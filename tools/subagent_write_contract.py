"""Subagent write-contract preflight: know BEFORE dispatch whether a planned
output path is directly writable, or must go through an approved staging area
with a controlled parent-side final move.

Root cause this fixes (RMK Overnight run, 2026-09-16/17): three subagents did
several minutes of real work, then discovered at the final ``write_file`` call
that their target path was outside ``HERMES_WRITE_SAFE_ROOT`` and lost the
deliverable. HERMES_WRITE_SAFE_ROOT stays a hard security boundary (see
``agent.file_safety``); this module never bypasses it. Instead it lets the
orchestrator (or the child itself, first thing) classify every planned output
path up front and pick one of two lanes:

  DIRECT  -- path already inside a safe write root (or no root configured):
             the child writes there itself, as today.
  STAGED  -- path is outside every safe root: the child writes into an
             approved staging directory instead, and returns an
             ``ArtifactManifest`` describing what it produced and where it
             *wants* it to end up. The parent (which is not sandboxed the same
             way -- it drives ``terminal``/``mv`` directly) performs the final
             move after validating the manifest.

This module is intentionally decoupled from ``tools.delegate_tool``: it does
not change dispatch, toolsets, or the existing child lifecycle. It is meant to
be called (a) by the parent before ``delegate_task(...)``, to pre-resolve each
task's declared output paths and fold the routing decision into that task's
``context``, and (b) by a child/parent when it is time to commit staged
artifacts for real. Wiring it into ``delegate_tool.py`` is a separate,
reviewed step -- see the PHASE 2 report for the proposed call sites; this
module is safe to import and unit-test standalone today.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from agent.file_safety import get_safe_write_roots, is_write_denied


# ---------------------------------------------------------------------------
# Path classification (preflight, before any dispatch)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PathDecision:
    """One planned output path, classified before the child ever runs."""

    requested_path: str
    lane: str  # "direct" | "staged"
    write_target: str  # where the child should actually write
    final_target: Optional[str] = None  # where it should end up (staged lane only)


def classify_output_path(path: str, *, staging_root: Optional[str] = None) -> PathDecision:
    """Classify a single planned output path as DIRECT or STAGED.

    ``is_write_denied`` is the single source of truth (same check ``write_file``
    itself performs) so this can never be more permissive than the real guard --
    only more informative, and earlier.
    """
    resolved = os.path.realpath(os.path.expanduser(str(path)))
    if not is_write_denied(resolved):
        return PathDecision(requested_path=path, lane="direct", write_target=resolved)

    root = _resolve_staging_root(staging_root)
    staged_name = f"{uuid.uuid4().hex}_{os.path.basename(resolved) or 'artifact'}"
    staged_path = os.path.join(root, staged_name)
    return PathDecision(
        requested_path=path, lane="staged", write_target=staged_path, final_target=resolved,
    )


def classify_output_paths(
    paths: List[str], *, staging_root: Optional[str] = None,
) -> Dict[str, PathDecision]:
    """Classify every planned output path for one subagent task, in one pass."""
    root = _resolve_staging_root(staging_root)
    return {p: classify_output_path(p, staging_root=root) for p in paths}


def _resolve_staging_root(staging_root: Optional[str]) -> str:
    """Pick a real, existing staging directory: caller override, first
    configured safe-write root, or a durable fallback under HERMES_HOME."""
    if staging_root:
        resolved = os.path.realpath(os.path.expanduser(staging_root))
        os.makedirs(resolved, exist_ok=True)
        return resolved
    safe_roots = sorted(get_safe_write_roots())
    if safe_roots:
        # First configured root is the convention: deterministic, not "whichever
        # happened to be iterated first" -- callers needing a specific root pass
        # staging_root= explicitly.
        os.makedirs(safe_roots[0], exist_ok=True)
        return safe_roots[0]
    from hermes_constants import get_hermes_home

    fallback = os.path.join(get_hermes_home(), "cache", "delegation", "staging")
    os.makedirs(fallback, exist_ok=True)
    return fallback


# ---------------------------------------------------------------------------
# Preflight brief: what a child (or the parent, on its behalf) should be told
# ---------------------------------------------------------------------------

def build_preflight_brief(decisions: Dict[str, PathDecision]) -> str:
    """Render a short, explicit instruction block for the child's context so
    it never discovers the write-safety boundary only after finishing work."""
    if not decisions:
        return ""
    lines = ["WRITE CONTRACT (resolved before dispatch -- do not deviate):"]
    for requested, d in decisions.items():
        if d.lane == "direct":
            lines.append(f"  - {requested}: WRITE DIRECTLY to {d.write_target}")
        else:
            lines.append(
                f"  - {requested}: outside the write-safe root. Write to "
                f"{d.write_target} instead (staging area), then report it in your "
                f"final ArtifactManifest as produced for final path {d.final_target}. "
                "Do NOT attempt to write the final path yourself; the parent moves it."
            )
    lines.append(
        "End your final answer with a JSON ArtifactManifest block "
        "(see build_manifest_instructions()) listing every file you produced, "
        "even ones written directly."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Artifact manifest: what the child reports back
# ---------------------------------------------------------------------------

@dataclass
class ArtifactEntry:
    staged_path: str
    final_path: str
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None


@dataclass
class ArtifactManifest:
    entries: List[ArtifactEntry] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {"artifacts": [
                {"staged_path": e.staged_path, "final_path": e.final_path,
                 "size_bytes": e.size_bytes, "sha256": e.sha256}
                for e in self.entries
            ]},
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "ArtifactManifest":
        data = json.loads(text)
        entries = [
            ArtifactEntry(
                staged_path=e["staged_path"], final_path=e["final_path"],
                size_bytes=e.get("size_bytes"), sha256=e.get("sha256"),
            )
            for e in data.get("artifacts", [])
        ]
        return cls(entries=entries)


def build_manifest_instructions() -> str:
    return (
        "ArtifactManifest JSON shape:\n"
        '{"artifacts": [{"staged_path": "<where you wrote it>", '
        '"final_path": "<where it should end up>", '
        '"size_bytes": <int, optional>, "sha256": "<hex, optional>"}]}'
    )


# ---------------------------------------------------------------------------
# Parent-side controlled commit
# ---------------------------------------------------------------------------

@dataclass
class CommitResult:
    final_path: str
    ok: bool
    error: Optional[str] = None


def commit_manifest(manifest: ArtifactManifest, *, dry_run: bool = False) -> List[CommitResult]:
    """Move every staged artifact to its declared final path.

    Validates EACH final path against the real write-safety guard again right
    before moving (defence in depth -- a manifest is child-reported data, never
    trusted blindly) and refuses any move whose final path is itself denied
    for a reason other than being outside a *staging* root (i.e. a genuine
    credential-path attempt is refused even here).
    """
    return [_commit_one(entry, dry_run=dry_run) for entry in manifest.entries]


def _commit_one(entry: ArtifactEntry, *, dry_run: bool) -> CommitResult:
    staged = os.path.realpath(os.path.expanduser(entry.staged_path))
    final = os.path.realpath(os.path.expanduser(entry.final_path))

    if not os.path.isfile(staged):
        return CommitResult(final_path=final, ok=False,
                             error=f"staged artifact missing: {staged}")

    # Defence in depth: refuse a final path that is a hard-denied credential
    # path, even though the caller already believes it validated the manifest.
    from agent.file_safety import build_write_denied_paths, build_write_denied_prefixes
    home = os.path.realpath(os.path.expanduser("~"))
    if final in build_write_denied_paths(home) or any(
        final.startswith(prefix) for prefix in build_write_denied_prefixes(home)
    ):
        return CommitResult(final_path=final, ok=False,
                             error=f"final path is a protected credential path: {final}")

    if dry_run:
        return CommitResult(final_path=final, ok=True)

    try:
        os.makedirs(os.path.dirname(final) or ".", exist_ok=True)
        shutil.move(staged, final)
    except OSError as exc:
        return CommitResult(final_path=final, ok=False, error=str(exc))
    return CommitResult(final_path=final, ok=True)
