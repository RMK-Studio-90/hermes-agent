#!/usr/bin/env python3
"""Fixed-context budget matrix across Hermes surfaces.

``hermes prompt-size`` answers "how big is one platform's fresh prompt?".
This script answers the comparison question: how much fixed context (system
prompt + tool schemas) does each *surface* pay per API call — the main CLI
agent, the CLI inside a repository (project context files), messaging
platforms, cron, and a leaf ``delegate_task`` subagent — so a config or
context-file change can be measured before/after on the same inputs.

Everything runs offline through the real construction paths (``AIAgent``
with dummy credentials, ``build_system_prompt``, the cron toolset resolver,
and ``delegate_tool._build_child_agent``). No API call is made.

Usage::

    # Against your real install (reads $HERMES_HOME / ~/.hermes):
    python scripts/context_budget_matrix.py --repo /path/to/project

    # Machine-readable, e.g. to diff two runs:
    python scripts/context_budget_matrix.py --repo . --json > before.json

    # Reproducible fixture home (bundled skills synced, default SOUL.md,
    # memory filled to a typical level) — used for repo-level before/after:
    python scripts/context_budget_matrix.py --fixture-home /tmp/hh --repo .

Token counts use the same chars/4 estimate as ``/context``
(``agent/context_breakdown.py``); they are estimates, not provider counts.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _tok(chars: int) -> int:
    return (chars + 3) // 4


def _prepare_fixture_home(home: Path) -> None:
    """Populate a throwaway HERMES_HOME the way a set-up install looks."""
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    from hermes_cli.config import ensure_hermes_home

    ensure_hermes_home()
    from tools.skills_sync import sync_skills

    sync_skills(quiet=True)
    mem_dir = home / "memories"
    mem_dir.mkdir(exist_ok=True)
    # ~70% of the default char limits (memory 2200, user 1375).
    (mem_dir / "MEMORY.md").write_text(
        "\n§\n".join(
            f"Project note {i}: prefers uv + scripts/run_tests.sh; repo at ~/src/app{i}."
            for i in range(20)
        )[:1540],
        encoding="utf-8",
    )
    (mem_dir / "USER.md").write_text(
        "\n§\n".join(
            f"User fact {i}: communicates in German, wants evidence-backed results."
            for i in range(14)
        )[:960],
        encoding="utf-8",
    )


def _measure_agent(
    agent: Any, extra_prompt: str = "", *, cwd: Optional[str] = None
) -> Dict[str, Any]:
    from agent.system_prompt import build_system_prompt, build_system_prompt_parts
    from hermes_cli.prompt_size import _SKILLS_BLOCK_RE

    # Project context files are resolved from the cwd at prompt-build time.
    prev = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with contextlib.redirect_stdout(sys.stderr):
            parts = build_system_prompt_parts(agent)
            system = build_system_prompt(agent)
    finally:
        os.chdir(prev)
    if extra_prompt:
        system = (system + "\n\n" + extra_prompt).strip()
    stable = parts.get("stable", "")
    volatile = parts.get("volatile", "")
    context = parts.get("context", "")
    m = _SKILLS_BLOCK_RE.search(volatile) or _SKILLS_BLOCK_RE.search(stable)
    skills_index = m.group(0) if m else ""
    memory = ""
    store = getattr(agent, "_memory_store", None)
    if store is not None:
        try:
            if getattr(agent, "_memory_enabled", True):
                memory += store.format_for_system_prompt("memory") or ""
            if getattr(agent, "_user_profile_enabled", True):
                memory += store.format_for_system_prompt("user") or ""
        except Exception:
            pass
    tools = getattr(agent, "tools", None) or []
    tools_json = json.dumps(tools, ensure_ascii=False)
    names = sorted(
        (t.get("function") or {}).get("name", "") for t in tools if isinstance(t, dict)
    )
    project_ctx = context
    if extra_prompt and "project context files" in extra_prompt:
        project_ctx = extra_prompt.split("project context files", 1)[1]
    return {
        "system_prompt_tokens": _tok(len(system)),
        "skills_index_tokens": _tok(len(skills_index)),
        "project_context_tokens": _tok(len(project_ctx)),
        "memory_tokens": _tok(len(memory)),
        "tool_schema_tokens": _tok(len(tools_json)),
        "tool_count": len(tools),
        "fixed_total_tokens": _tok(len(system)) + _tok(len(tools_json)),
        "tools": names,
    }


def _inspection_agent(
    platform: str, *, cwd: Optional[str], cron: bool = False, model: str = ""
) -> Any:
    from run_agent import AIAgent
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from agent.skill_utils import parse_config_string_list

    cfg = load_config()
    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    model = model or model_cfg.get("default") or model_cfg.get("model") or ""
    agent_cfg = cfg.get("agent") or {}
    if cron:
        from cron.scheduler import (
            _resolve_cron_disabled_toolsets,
            _resolve_cron_enabled_toolsets,
        )

        enabled = _resolve_cron_enabled_toolsets({}, cfg)
        disabled = _resolve_cron_disabled_toolsets(cfg)
    else:
        enabled = sorted(_get_platform_tools(cfg, platform))
        disabled = parse_config_string_list(agent_cfg.get("disabled_toolsets")) or None
    prev = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        kwargs: Dict[str, Any] = dict(
            model=model,
            api_key="inspect-only",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            save_trajectories=False,
            platform=platform,
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
        )
        if cron:
            # Mirrors cron/scheduler.py run_job(): no workdir → no cwd context.
            kwargs.update(skip_context_files=True, load_soul_identity=True)
        elif not cwd:
            kwargs.update(skip_context_files=True)
        with contextlib.redirect_stdout(sys.stderr):
            return AIAgent(**kwargs)
    finally:
        os.chdir(prev)


def _leaf_subagent(parent: Any, repo: Optional[str], toolsets: Optional[List[str]]) -> Any:
    from tools import delegate_tool

    if repo:
        os.environ["TERMINAL_CWD"] = repo
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return delegate_tool._build_child_agent(
            task_index=0,
                goal="Inspect the module and report findings.",
                context=None,
                toolsets=toolsets,
                model=None,
                max_iterations=10,
                task_count=1,
                parent_agent=parent,
            )
    finally:
        os.environ.pop("TERMINAL_CWD", None)


def compute_matrix(repo: Optional[str], model: str = "") -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    rows["main_agent"] = _measure_agent(_inspection_agent("cli", cwd=None, model=model))
    parent = None
    if repo:
        parent = _inspection_agent("cli", cwd=repo, model=model)
        rows["cli_repo"] = _measure_agent(parent, cwd=repo)
    rows["telegram"] = _measure_agent(_inspection_agent("telegram", cwd=None, model=model))
    rows["discord"] = _measure_agent(_inspection_agent("discord", cwd=None, model=model))
    rows["cron"] = _measure_agent(
        _inspection_agent("cron", cwd=None, cron=True, model=model)
    )
    parent = parent or _inspection_agent("cli", cwd=None, model=model)
    child = _leaf_subagent(parent, repo, None)
    rows["leaf_subagent_inherit"] = _measure_agent(
        child, getattr(child, "ephemeral_system_prompt", "") or ""
    )
    child = _leaf_subagent(parent, repo, ["terminal", "file"])
    rows["leaf_subagent_coding"] = _measure_agent(
        child, getattr(child, "ephemeral_system_prompt", "") or ""
    )
    return rows


_COLUMNS = [
    ("fixed_total_tokens", "fixed"),
    ("system_prompt_tokens", "sysprompt"),
    ("tool_schema_tokens", "tools"),
    ("tool_count", "#tools"),
    ("skills_index_tokens", "skills"),
    ("project_context_tokens", "project"),
    ("memory_tokens", "memory"),
]


def render(rows: Dict[str, Dict[str, Any]]) -> str:
    head = f"{'surface':24}" + "".join(f"{label:>11}" for _, label in _COLUMNS)
    lines = [head, "-" * len(head)]
    for name, row in rows.items():
        lines.append(f"{name:24}" + "".join(f"{row[k]:>11,}" for k, _ in _COLUMNS))
    lines.append("")
    lines.append("tokens ≈ chars/4; 'fixed' = system prompt + tool schemas per API call")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", help="Project directory for the CLI-in-repo and subagent rows")
    ap.add_argument("--fixture-home", help="Build a reproducible HERMES_HOME here and measure it")
    ap.add_argument(
        "--model",
        default="",
        help="Model to size against (drives the context-window-scaled caps); "
        "default: model.default from config",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    args = ap.parse_args(argv)

    if args.fixture_home:
        _prepare_fixture_home(Path(args.fixture_home).resolve())
    repo = str(Path(args.repo).resolve()) if args.repo else None
    # Keep the cwd neutral so the main-agent row never picks up a project file.
    neutral = tempfile.mkdtemp(prefix="hermes-ctx-matrix-")
    os.chdir(neutral)
    rows = compute_matrix(repo, args.model)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
