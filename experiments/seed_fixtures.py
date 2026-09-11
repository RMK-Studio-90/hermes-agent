"""GRAPH-5 V2 — Seeded Fixture Module (K09 §15/P2).

Deterministic precondition seeding for the 10 excluded K06 golden-set cases.
Each seed(case_id, case_dir) writes fixture state (board, tasks, state.db rows,
artifact files) into an isolated case scratch dir; verify() reads back the fixture;
cleanup() removes the dir. All timestamps are fixed in the past for determinism.

Module is self-contained: imports kanban_db, kanban_db_connect, sqlite3.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

FIXED_TS = 1725000000  # deterministic "past" Unix timestamp
TEMPLATE_HOME = Path(r"E:/KI/.k09-sandbox/home-template")
PLUGIN_SRC = Path(r"E:/KI/Hermes/plugins/rmk-task-transformer")

FIXTURE_GO_BRIEF_TRF01 = (
    "Delegation: Erstelle eine RMK-Karte aus dem deutschen GO-Brief "
    "f\u00fcr das K09 Transfer-Experiment.\n\n"
    "GOAL: Erstelle eine Kanban-Karte 'k09-trf01-go' auf dem rmk-system Board "
    "basierend auf dem beigef\u00fcgten Auftrag. "
    "Verwende idempotency_key='k09-trf01-idem' und created_by='k09-seed'.\n\n"
    "SCOPE: Fertig, wenn eine Karte mit dem idempotency_key existiert.\n"
    "DEPENDS: Karten k09-trf01-p-k05, k09-trf01-p-k08 (bereits erledigt).\n"
    "ACCEPTANCE: Genau 1 Karte mit idempotency_key='k09-trf01-idem'.\n"
    "OUTPUT: Ergebnis des Kanban-Creates.\n"
    "CURRENT GATE: Bereit."
)

__all__ = ["seed", "verify", "cleanup", "FIXTURE_GO_BRIEF_TRF01"]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _db_path(case_dir: Path) -> Path:
    return case_dir / "kanban_home" / "kanban" / "boards" / "rmk-system" / "kanban.db"


def _ensure_kanban_env(case_dir: Path) -> None:
    kh = str(case_dir / "kanban_home")
    kb = str(_db_path(case_dir))
    # ASSIGN (not setdefault): a fresh case_dir must always win. setdefault
    # leaked the first case's path into every later seed of the same process
    # (cross-case and cross-arm contamination after rmtree).
    os.environ["HERMES_KANBAN_HOME"] = kh
    os.environ["HERMES_KANBAN_DB"] = kb
    os.environ["HERMES_KANBAN_BOARD"] = "rmk-system"


def _init_board(case_dir: Path):
    from hermes_cli.kanban_db import create_board
    from hermes_cli.kanban_db_connect import connect

    _ensure_kanban_env(case_dir)
    create_board("rmk-system", name="K09 RMK System Board")
    return connect(board="rmk-system")


def _state_db(case_dir: Path):
    p = case_dir / "home" / "state.db"
    con = sqlite3.connect(str(p))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def copy_template(case_dir: Path) -> None:
    src = TEMPLATE_HOME
    dst_home = case_dir / "home"
    if dst_home.exists():
        shutil.rmtree(dst_home)
    shutil.copytree(src, dst_home, dirs_exist_ok=True)
    for sub in ["kanban_home", "workspace", "logs"]:
        (case_dir / sub).mkdir(parents=True, exist_ok=True)


def _write_artifact(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# ORC-01
# ---------------------------------------------------------------------------


def seed_ORC_01(case_dir: Path) -> dict:
    from hermes_cli.kanban_db import create_task
    from hermes_cli.kanban_db_connect import connect

    copy_template(case_dir)
    conn = _init_board(case_dir)
    body_go = (
        "GO: Read SPEC-K09-01.md and write byte-identical canonical+replica copies.\n"
        "SCOPE: Read from canonical/SPEC-K09-01.md, write to canonical/SPEC-K09-01.md\n"
        "and replica/SPEC-K09-01.md; verify byte identity and report SHA256.\n"
        f"CANONICAL: {case_dir}/workspace/canonical/SPEC-K09-01.md\n"
        f"REPLICA: {case_dir}/workspace/replica/SPEC-K09-01.md\n"
        "ACCEPTANCE: Two files byte-identical.\n"
    )
    task_id = create_task(conn, title="K09 ORC-01 Spec Card", body=body_go,
                          created_by="k09-seed", initial_status="running")
    conn.execute("UPDATE tasks SET id=?, status='ready' WHERE id=?",
                 ("k09-orc01-spec", task_id))
    conn.commit()
    canonical = case_dir / "workspace" / "canonical" / "SPEC-K09-01.md"
    replica = case_dir / "workspace" / "replica" / "SPEC-K09-01.md"
    seedsrc = case_dir / "workspace" / "seed-src" / "SPEC-draft.md"
    content = "SPEC K09-01: deterministic seed content v1\nsha256 placeholder\n"
    _write_artifact(canonical, content)
    _write_artifact(replica, content)
    _write_artifact(seedsrc, "source draft for SPEC-K09-01\n")
    conn.close()
    return {
        "case_id": "ORC-01",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {"canonical": _sha256_file(canonical),
                            "replica": _sha256_file(replica)},
        "card_ids": ["k09-orc01-spec"],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
    }


def verify_ORC_01(case_dir: Path) -> list[str]:
    p = _db_path(case_dir)
    if not p.exists():
        return [f"kanban DB missing at {p}"]
    conn = sqlite3.connect(str(p))
    row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?",
                       ("k09-orc01-spec",)).fetchone()
    conn.close()
    problems = []
    if not row:
        problems.append("task k09-orc01-spec not found")
    elif row[0] != "ready":
        problems.append(f"expected status 'ready', got {row[0]!r}")
    for art in ["canonical", "replica"]:
        ap = case_dir / "workspace" / art / "SPEC-K09-01.md"
        if not ap.exists():
            problems.append(f"artifact missing: {ap}")
    return problems


# ---------------------------------------------------------------------------
# ORC-02
# ---------------------------------------------------------------------------


def seed_ORC_02(case_dir: Path) -> dict:
    from hermes_cli.kanban_db import create_task, link_tasks
    from hermes_cli.kanban_db_connect import connect

    copy_template(case_dir)
    conn = _init_board(case_dir)
    parent = create_task(conn, title="ORC02 Parent", body="Parent task",
                         created_by="k09-seed", initial_status="running")
    sibling = create_task(conn, title="ORC02 Sibling", body="Sibling ready",
                          created_by="k09-seed", initial_status="running")
    child = create_task(conn, title="ORC02 Child", body="Gated child",
                        created_by="k09-seed",
                        parents=[parent, sibling], initial_status="running")
    for old_id, new_id in [(parent, "k09-orc02-parent"),
                           (sibling, "k09-orc02-sibling-parent"),
                           (child, "k09-orc02-child")]:
        conn.execute("UPDATE tasks SET id=?, status='ready' WHERE id=?", (new_id, old_id))
    conn.execute("UPDATE tasks SET status='todo' WHERE id='k09-orc02-child'")
    # Links created by create_task pointed at the temp ids and were orphaned by
    # the id UPDATE; re-insert with the fixed ids (kanban.db has no FKs).
    conn.execute("DELETE FROM task_links WHERE child_id='k09-orc02-child'")
    conn.execute("INSERT INTO task_links (parent_id, child_id) "
                 "VALUES ('k09-orc02-parent', 'k09-orc02-child')")
    conn.execute("INSERT INTO task_links (parent_id, child_id) "
                 "VALUES ('k09-orc02-sibling-parent', 'k09-orc02-child')")
    conn.commit()
    conn.close()
    return {
        "case_id": "ORC-02",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {},
        "card_ids": ["k09-orc02-parent", "k09-orc02-sibling-parent",
                     "k09-orc02-child"],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
    }


def verify_ORC_02(case_dir: Path) -> list[str]:
    conn = sqlite3.connect(str(_db_path(case_dir)))
    rows = {r[0]: r[1]
            for r in conn.execute("SELECT id, status FROM tasks").fetchall()}
    links = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id='k09-orc02-child'"
    ).fetchall()
    conn.close()
    problems = []
    if rows.get("k09-orc02-parent") != "ready":
        problems.append("parent not ready")
    if rows.get("k09-orc02-sibling-parent") != "ready":
        problems.append("sibling not ready")
    if rows.get("k09-orc02-child") != "todo":
        problems.append(f"child not todo (got {rows.get('k09-orc02-child')})")
    if len(links) != 2:
        problems.append(f"expected 2 parent links, got {len(links)}")
    return problems


# ---------------------------------------------------------------------------
# REV-01
# ---------------------------------------------------------------------------


def seed_REV_01(case_dir: Path) -> dict:
    from hermes_cli.kanban_db import create_task
    from hermes_cli.kanban_db_connect import connect

    copy_template(case_dir)
    conn = _init_board(case_dir)
    artifact_path = case_dir / "workspace" / "canonical" / "artifact-rev01.md"
    _write_artifact(artifact_path, "REV-01 artifact: deterministic review target.\n")
    body = (
        "Review the artifact below and verify it matches the reference SHA256.\n"
        f"Path: {artifact_path}\n"
        f"Reference SHA256: {_sha256_file(artifact_path)}\n"
        "Spot-check: 1) SHA256 match; 2) Content format; 3) Verdict.\n"
    )
    task_id = create_task(conn, title="REV-01 Artifact Review", body=body,
                          created_by="k09-seed", assignee="author-k09",
                          initial_status="running")
    conn.execute("UPDATE tasks SET id=?, status='review' WHERE id=?",
                 ("k09-rev01-artifact", task_id))
    conn.commit()
    conn.close()
    return {
        "case_id": "REV-01",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {"artifact-rev01": _sha256_file(artifact_path)},
        "card_ids": ["k09-rev01-artifact"],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
    }


def verify_REV_01(case_dir: Path) -> list[str]:
    conn = sqlite3.connect(str(_db_path(case_dir)))
    row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?",
                       ("k09-rev01-artifact",)).fetchone()
    conn.close()
    problems = []
    if not row:
        return ["task k09-rev01-artifact not found"]
    if row[0] != "review":
        problems.append(f"expected status 'review', got {row[0]!r}")
    if row[1] != "author-k09":
        problems.append(f"expected assignee 'author-k09', got {row[1]!r}")
    ap = case_dir / "workspace" / "canonical" / "artifact-rev01.md"
    if not ap.exists():
        problems.append(f"artifact missing at {ap}")
    return problems


# ---------------------------------------------------------------------------
# CTX-01
# ---------------------------------------------------------------------------


def seed_CTX_01(case_dir: Path) -> dict:
    from hermes_cli.kanban_db import create_task
    from hermes_cli.kanban_db_connect import connect

    copy_template(case_dir)
    conn = _init_board(case_dir)
    sdb = _state_db(case_dir)
    parent_id = "k09seed-ctx01-parent"
    sdb.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at, end_reason, "
        "message_count, profile_name) "
        "VALUES (?, 'cli', ?, ?, 'compression', 5, 'rmk-knowledge')",
        (parent_id, FIXED_TS, FIXED_TS + 60))
    child_id = "k09seed-ctx01-child"
    sdb.execute(
        "INSERT INTO sessions (id, source, parent_session_id, started_at, "
        "ended_at, end_reason, message_count, profile_name) "
        "VALUES (?, 'subagent', ?, ?, ?, 'completed', 3, 'rmk-knowledge')",
        (child_id, parent_id, FIXED_TS + 70, FIXED_TS + 120))
    sdb.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'user', 'Task k09-ctx01-task: status blocked', ?)",
                (parent_id, FIXED_TS + 1))
    sdb.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'assistant', 'Found task k09-ctx01-task blocked', ?)",
                (parent_id, FIXED_TS + 2))
    sdb.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'user', 'Continue: report 1-3 sentence status', ?)",
                (parent_id, FIXED_TS + 3))
    sdb.execute("INSERT INTO messages (session_id, role, content, "
                "_compressed_summary, timestamp) "
                "VALUES (?, 'assistant', 'summary: task k09-ctx01-task blocked', 1, ?)",
                (child_id, FIXED_TS + 80))
    sdb.commit()
    sdb.close()

    art_path = case_dir / "workspace" / "artifacts" / "ctx01-evidence.md"
    _write_artifact(art_path, "CTX-01 evidence: prior work output\n")
    body = f"Resume task k09-ctx01-task. Artifact: {art_path}\n"
    task_id = create_task(conn, title="CTX-01 Task Resume", body=body,
                          created_by="k09-seed", initial_status="running")
    conn.execute("UPDATE tasks SET id=?, status='blocked' WHERE id=?",
                 ("k09-ctx01-task", task_id))
    conn.commit()
    conn.close()
    return {
        "case_id": "CTX-01",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {"ctx01-evidence": _sha256_file(art_path)},
        "card_ids": ["k09-ctx01-task"],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
        "state_db_sha256": _sha256_file(case_dir / "home" / "state.db"),
    }


def verify_CTX_01(case_dir: Path) -> list[str]:
    problems = []
    sdb = _state_db(case_dir)
    row = sdb.execute(
        "SELECT end_reason, message_count FROM sessions WHERE id=?",
        ("k09seed-ctx01-parent",)).fetchone()
    if not row:
        problems.append("parent session not found")
    elif row[0] != "compression":
        problems.append(f"expected end_reason 'compression', got {row[0]!r}")
    mc = sdb.execute(
        "SELECT count(*) FROM messages WHERE session_id='k09seed-ctx01-parent'"
    ).fetchone()[0]
    if mc < 3:
        problems.append(f"expected >=3 messages in parent, got {mc}")
    sdb.close()
    conn = sqlite3.connect(str(_db_path(case_dir)))
    row2 = conn.execute("SELECT status FROM tasks WHERE id=?",
                        ("k09-ctx01-task",)).fetchone()
    conn.close()
    if not row2:
        problems.append("task k09-ctx01-task not found")
    elif row2[0] != "blocked":
        problems.append(f"expected status 'blocked', got {row2[0]!r}")
    return problems


# ---------------------------------------------------------------------------
# TIM-01
# ---------------------------------------------------------------------------


def seed_TIM_01(case_dir: Path) -> dict:
    copy_template(case_dir)
    conn = _init_board(case_dir)
    conn.execute("INSERT INTO tasks (id, title, body, status, created_by, "
                 "created_at, workspace_kind, consecutive_failures) "
                 "VALUES (?, 'TIM-01 Timeout Card', 'Timeout fixture', "
                 "'todo', 'k09-seed', ?, 'scratch', 1)",
                 ("k09-tim01-task", FIXED_TS))
    conn.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, "
                 "ended_at, error, max_runtime_seconds) "
                 "VALUES (?, 'timed_out', 'timed_out', ?, ?, "
                 "'timeout after max_runtime_seconds', 30)",
                 ("k09-tim01-task", FIXED_TS, FIXED_TS + 35))
    conn.commit()
    conn.close()
    sdb = _state_db(case_dir)
    sdb.execute("INSERT INTO sessions (id, source, started_at, ended_at, "
                "end_reason, message_count, profile_name) "
                "VALUES (?, 'cli', ?, ?, 'timeout', 2, 'rmk-knowledge')",
                ("k09seed-tim01-timeout", FIXED_TS, FIXED_TS + 40))
    sdb.commit()
    sdb.close()
    log_path = case_dir / "home" / "logs" / "errors.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"[{FIXED_TS}] TIMEOUT: task k09-tim01-task timed out after 30s\n",
        encoding="utf-8")
    return {
        "case_id": "TIM-01",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {},
        "card_ids": ["k09-tim01-task"],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
        "state_db_sha256": _sha256_file(case_dir / "home" / "state.db"),
    }


def verify_TIM_01(case_dir: Path) -> list[str]:
    problems = []
    conn = sqlite3.connect(str(_db_path(case_dir)))
    run = conn.execute("SELECT status, outcome FROM task_runs WHERE task_id=?",
                       ("k09-tim01-task",)).fetchone()
    if not run:
        problems.append("task_run not found")
    elif not (run[0] == "timed_out" and run[1] == "timed_out"):
        problems.append(f"expected timed_out/timed_out, got {run}")
    row = conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id=?",
                       ("k09-tim01-task",)).fetchone()
    if row and row[1] != 1:
        problems.append(f"expected consecutive_failures=1, got {row[1]}")
    conn.close()
    sdb = _state_db(case_dir)
    srow = sdb.execute("SELECT end_reason FROM sessions WHERE id=?",
                       ("k09seed-tim01-timeout",)).fetchone()
    sdb.close()
    if not srow:
        problems.append("timeout session not found")
    elif srow[0] != "timeout":
        problems.append(f"expected end_reason 'timeout', got {srow[0]!r}")
    logp = case_dir / "home" / "logs" / "errors.log"
    if not logp.exists():
        problems.append("errors.log not found")
    elif "timeout" not in logp.read_text(encoding="utf-8").lower():
        problems.append("errors.log missing timeout line")
    return problems


# ---------------------------------------------------------------------------
# TOL-02
# ---------------------------------------------------------------------------


def seed_TOL_02(case_dir: Path) -> dict:
    copy_template(case_dir)
    conn = _init_board(case_dir)
    conn.close()
    sdb = _state_db(case_dir)
    sid = "k09seed-tol02-probe"
    sdb.execute("INSERT INTO sessions (id, source, started_at, ended_at, "
                "end_reason, message_count, profile_name) "
                "VALUES (?, 'cli', ?, ?, 'completed', 3, 'rmk-knowledge')",
                (sid, FIXED_TS, FIXED_TS + 10))
    sdb.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'user', 'Run ls and report', ?)", (sid, FIXED_TS))
    sdb.execute("INSERT INTO messages (session_id, role, tool_calls, timestamp) "
                "VALUES (?, 'assistant', "
                "'[{\"type\":\"function\",\"function\":{\"name\":\"terminal\","
                "\"arguments\":\"{\\\"command\\\":\\\"ls\\\"}\"}}]', ?)",
                (sid, FIXED_TS + 1))
    sdb.execute("INSERT INTO messages (session_id, role, tool_name, tool_call_id, "
                "content, timestamp) "
                "VALUES (?, 'tool', 'terminal', 'call_k09seed_1', 'file1\\nfile2', ?)",
                (sid, FIXED_TS + 2))
    sdb.commit()
    sdb.close()
    return {
        "case_id": "TOL-02",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {},
        "card_ids": [],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
        "state_db_sha256": _sha256_file(case_dir / "home" / "state.db"),
    }


def verify_TOL_02(case_dir: Path) -> list[str]:
    problems = []
    sdb = _state_db(case_dir)
    tc = sdb.execute(
        "SELECT count(*) FROM messages WHERE session_id='k09seed-tol02-probe' "
        "AND role='tool' AND tool_name='terminal' AND tool_call_id='call_k09seed_1'"
    ).fetchone()[0]
    if tc != 1:
        problems.append(f"expected 1 tool message, got {tc}")
    tcc = sdb.execute(
        "SELECT count(*) FROM messages WHERE session_id='k09seed-tol02-probe' "
        "AND tool_calls IS NOT NULL"
    ).fetchone()[0]
    if tcc != 1:
        problems.append(f"expected 1 tool_calls message, got {tcc}")
    sdb.close()
    return problems


# ---------------------------------------------------------------------------
# RTE-01
# ---------------------------------------------------------------------------


def seed_RTE_01(case_dir: Path) -> dict:
    copy_template(case_dir)
    conn = _init_board(case_dir)
    conn.close()
    sdb = _state_db(case_dir)
    s1 = "k09seed-rte-session-1"
    s2 = "k09seed-rte-session-2"
    sdb.execute("INSERT INTO sessions (id, source, started_at, ended_at, "
                "end_reason, message_count, profile_name) "
                "VALUES (?, 'cli', ?, ?, 'completed', 1, 'rmk-knowledge')",
                (s1, FIXED_TS, FIXED_TS + 5))
    sdb.execute("INSERT INTO sessions (id, source, started_at, ended_at, "
                "end_reason, message_count, profile_name) "
                "VALUES (?, 'cli', ?, ?, 'completed', 1, 'rmk-knowledge')",
                (s2, FIXED_TS + 10, FIXED_TS + 15))
    for sid, mod in [(s1, "nemotron-3-ultra-550b:free"),
                     (s2, "qwen-3.5-9b:free")]:
        sdb.execute(
            "INSERT OR IGNORE INTO session_model_usage "
            "(session_id, model, billing_provider, billing_base_url, billing_mode, "
            "task, api_call_count, input_tokens, output_tokens, estimated_cost_usd) "
            "VALUES (?, ?, 'openrouter', 'https://openrouter.ai/api/v1', "
            "'free', 'default', 1, 100, 50, 0.0)", (sid, mod))
    sdb.commit()
    sdb.close()
    return {
        "case_id": "RTE-01",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {},
        "card_ids": [],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
        "state_db_sha256": _sha256_file(case_dir / "home" / "state.db"),
    }


def verify_RTE_01(case_dir: Path) -> list[str]:
    problems = []
    sdb = _state_db(case_dir)
    rows = sdb.execute(
        "SELECT model, billing_provider FROM session_model_usage").fetchall()
    sdb.close()
    if not rows:
        return ["no session_model_usage rows"]
    for model, prov in rows:
        if prov != "openrouter":
            problems.append(f"expected provider openrouter, got {prov}")
    return problems


# ---------------------------------------------------------------------------
# AMB-02
# ---------------------------------------------------------------------------


def seed_AMB_02(case_dir: Path) -> dict:
    copy_template(case_dir)
    conn = _init_board(case_dir)
    conn.close()
    return {
        "case_id": "AMB-02",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {},
        "card_ids": [],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
    }


def verify_AMB_02(case_dir: Path) -> list[str]:
    conn = sqlite3.connect(str(_db_path(case_dir)))
    count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    conn.close()
    if count != 0:
        return [f"expected 0 tasks on empty board, got {count}"]
    return []


# ---------------------------------------------------------------------------
# SPC-02
# ---------------------------------------------------------------------------


def seed_SPC_02(case_dir: Path) -> dict:
    from hermes_cli.kanban_db import create_task
    from hermes_cli.kanban_db_connect import connect

    copy_template(case_dir)
    conn = _init_board(case_dir)
    body = (
        "GO: Create a delegate child that attempts delegation/kanban mutations.\n"
        "Child toolset must NOT contain delegate_task or kanban.\n"
        "Child must fail with PermissionError on kanban write via terminal.\n"
    )
    task_id = create_task(conn, title="SPC-02 Forbidden Delegation", body=body,
                          created_by="k09-seed", initial_status="running")
    conn.execute("UPDATE tasks SET id=?, status='ready' WHERE id=?",
                 ("k09-spc02-parent", task_id))
    conn.commit()
    conn.close()
    return {
        "case_id": "SPC-02",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {},
        "card_ids": ["k09-spc02-parent"],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
    }


def verify_SPC_02(case_dir: Path) -> list[str]:
    conn = sqlite3.connect(str(_db_path(case_dir)))
    row = conn.execute("SELECT status FROM tasks WHERE id=?",
                       ("k09-spc02-parent",)).fetchone()
    conn.close()
    if not row:
        return ["task k09-spc02-parent not found"]
    if row[0] != "ready":
        return [f"expected 'ready', got {row[0]!r}"]
    return []


# ---------------------------------------------------------------------------
# TRF-01
# ---------------------------------------------------------------------------


def seed_TRF_01(case_dir: Path) -> dict:
    from hermes_cli.kanban_db import create_task
    from hermes_cli.kanban_db_connect import connect

    copy_template(case_dir)
    plugin_dst = case_dir / "home" / "plugins" / "rmk-task-transformer"
    shutil.copytree(str(PLUGIN_SRC), str(plugin_dst), dirs_exist_ok=True)
    cfg_path = case_dir / "home" / "config.yaml"
    cfg_text = cfg_path.read_text(encoding="utf-8")
    cfg_text += "\nplugins:\n  enabled:\n    - rmk-task-transformer\n"
    cfg_path.write_text(cfg_text, encoding="utf-8")

    conn = _init_board(case_dir)
    p1 = create_task(conn, title="TRF-01 K05 Parent", body="Parent from K05",
                     created_by="k09-seed", initial_status="running")
    conn.execute("UPDATE tasks SET id=?, status='done' WHERE id=?",
                 ("k09-trf01-p-k05", p1))
    p2 = create_task(conn, title="TRF-01 K08 Parent", body="Parent from K08",
                     created_by="k09-seed", initial_status="running")
    conn.execute("UPDATE tasks SET id=?, status='done' WHERE id=?",
                 ("k09-trf01-p-k08", p2))
    conn.commit()
    conn.close()
    return {
        "case_id": "TRF-01",
        "board_db_sha256": _sha256_file(_db_path(case_dir)),
        "artifact_hashes": {},
        "card_ids": ["k09-trf01-p-k05", "k09-trf01-p-k08"],
        "template_config_sha256": _sha256_file(case_dir / "home" / "config.yaml"),
        "input_override": True,
        "fixture_go_brief": FIXTURE_GO_BRIEF_TRF01,
    }


def verify_TRF_01(case_dir: Path) -> list[str]:
    problems = []
    conn = sqlite3.connect(str(_db_path(case_dir)))
    p1 = conn.execute("SELECT status FROM tasks WHERE id=?",
                      ("k09-trf01-p-k05",)).fetchone()
    p2 = conn.execute("SELECT status FROM tasks WHERE id=?",
                      ("k09-trf01-p-k08",)).fetchone()
    conn.close()
    if not p1:
        problems.append("parent k09-trf01-p-k05 not found")
    elif p1[0] != "done":
        problems.append(f"parent k05 expected 'done', got {p1[0]!r}")
    if not p2:
        problems.append("parent k09-trf01-p-k08 not found")
    elif p2[0] != "done":
        problems.append(f"parent k08 expected 'done', got {p2[0]!r}")
    cfg = case_dir / "home" / "config.yaml"
    if "rmk-task-transformer" not in cfg.read_text(encoding="utf-8"):
        problems.append("plugin not enabled in config")
    pdir = case_dir / "home" / "plugins" / "rmk-task-transformer"
    if not pdir.exists():
        problems.append("plugin dir missing")
    return problems


# ---------------------------------------------------------------------------
# Dispatcher: seed(), verify(), cleanup()
# ---------------------------------------------------------------------------

_SEED_MAP = {
    "ORC-01": (seed_ORC_01, verify_ORC_01),
    "ORC-02": (seed_ORC_02, verify_ORC_02),
    "REV-01": (seed_REV_01, verify_REV_01),
    "CTX-01": (seed_CTX_01, verify_CTX_01),
    "TIM-01": (seed_TIM_01, verify_TIM_01),
    "TOL-02": (seed_TOL_02, verify_TOL_02),
    "RTE-01": (seed_RTE_01, verify_RTE_01),
    "AMB-02": (seed_AMB_02, verify_AMB_02),
    "SPC-02": (seed_SPC_02, verify_SPC_02),
    "TRF-01": (seed_TRF_01, verify_TRF_01),
}


def seed(case_id: str, case_dir: Path) -> dict:
    if case_id not in _SEED_MAP:
        raise ValueError(f"unknown case {case_id!r}")
    fn, _ = _SEED_MAP[case_id]
    return fn(case_dir)


def verify(case_id: str, case_dir: Path) -> list[str]:
    if case_id not in _SEED_MAP:
        raise ValueError(f"unknown case {case_id!r}")
    _, fn = _SEED_MAP[case_id]
    return fn(case_dir)


def cleanup(case_dir: Path) -> None:
    if case_dir.exists():
        shutil.rmtree(str(case_dir))


if __name__ == "__main__":  # pragma: no cover
    import sys
    if len(sys.argv) < 3:
        print("Usage: seed_fixtures.py <case_id> <case_dir> [--verify] [--cleanup]")
        sys.exit(1)
    case_id = sys.argv[1]
    case_dir = Path(sys.argv[2])
    if "--cleanup" in sys.argv:
        cleanup(case_dir)
        print(f"Cleaned up {case_dir}")
        sys.exit(0)
    man = seed(case_id, case_dir)
    print(json.dumps(man, indent=2, ensure_ascii=False))
    if "--verify" in sys.argv:
        problems = verify(case_id, case_dir)
        if problems:
            for p in problems:
                print(f"PROBLEM: {p}")
            sys.exit(1)
        print("verify: OK")