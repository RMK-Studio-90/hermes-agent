"""Read models and explicit learning promotion, separate from workflow state."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def overview(record):
    state = record["state"]
    nodes = record["nodes"]
    return {"task_id": state["task_id"], "goal": state["goal"], "status": state["status"],
            "current_node": state["current_node"], "retries": state["repair_cycles"],
            "tokens": sum((n["result"] or {}).get("input_tokens", 0) + (n["result"] or {}).get("output_tokens", 0) for n in nodes),
            "duration_ms": sum(n["duration_ms"] or 0 for n in nodes),
            "nodes": [{"node": n["node"], "status": n["status"], "duration_ms": n["duration_ms"],
                       "model": (n["result"] or {}).get("model"), "reason": n["reason"]} for n in nodes]}


def metrics(store):
    ids = [r[0] for r in store.conn.execute("SELECT task_id FROM graph_runs")]
    records = [store.inspect(task_id) for task_id in ids]
    summaries = [overview(r) for r in records]
    success = [s for s in summaries if s["status"] == "succeeded"]
    reviews = [n for r in records for n in r["nodes"] if n["node"] == "reviewer"]
    model_runs = [n for r in records for n in r["nodes"] if (n["result"] or {}).get("model_calls", 0)]
    costs = [(n["result"] or {}).get("cost_usd") for n in model_runs]
    return {"tasks": len(summaries), "successful_tasks": len(success),
            "success_rate": len(success) / len(summaries) if summaries else None,
            "first_pass_success_rate": sum(s["retries"] == 0 for s in success) / len(summaries) if summaries else None,
            "review_failure_rate": sum(n["status"] == "FAIL" for n in reviews) / len(reviews) if reviews else None,
            "repair_rate": sum(s["retries"] > 0 for s in summaries) / len(summaries) if summaries else None,
            "tokens_per_successful_task": sum(s["tokens"] for s in summaries) / len(success) if success else None,
            "latency_ms_per_successful_task": sum(s["duration_ms"] for s in success) / len(success) if success else None,
            "cost_per_successful_task": sum(costs) / len(success) if success and costs and all(c is not None for c in costs) else None,
            "escalation_rate": sum(s["status"] in {"failed", "needs_attention", "budget_exhausted"} for s in summaries) / len(summaries) if summaries else None,
            "regression_rate": None,
            "runs": summaries}


def learning_candidate(store, task_id):
    record = store.inspect(task_id)
    state = record["state"]
    if state["status"] != "succeeded" or "gate" not in state["outputs"]:
        raise ValueError("Learning requires independently verified success")
    repair = state["outputs"].get("repair", {}).get("output", {})
    claim = repair.get("root_cause")
    if not claim:
        return None
    pattern = hashlib.sha256(claim.strip().casefold().encode()).hexdigest()
    candidate = {"pattern": pattern, "claim": claim, "source_task": task_id,
                 "evidence_revision": state["artifact_revision"], "status": "candidate"}
    store.conn.execute("CREATE TABLE IF NOT EXISTS learning_candidates (task_id TEXT PRIMARY KEY, pattern TEXT NOT NULL, payload TEXT NOT NULL)")
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO learning_candidates VALUES (?,?,?)", (task_id, pattern, json.dumps(candidate)))
    return candidate


def promote(store, task_id, destination, *, approved=False):
    if not approved:
        raise ValueError("Explicit approval is required for learning promotion")
    candidate = learning_candidate(store, task_id)
    if candidate is None:
        raise ValueError("No reusable repair learning was identified")
    rows = store.conn.execute("SELECT task_id FROM learning_candidates WHERE pattern=?", (candidate["pattern"],)).fetchall()
    sources = [r[0] for r in rows if store.inspect(r[0])["state"]["status"] == "succeeded"]
    if len(sources) < 2:
        raise ValueError("Promotion requires the pattern in two independently completed tasks")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = ("# Validated HGES learning\n\n" + candidate["claim"] + "\n\nSources: " + ", ".join(sources)
            + "\n\nApproved: " + datetime.now(timezone.utc).isoformat() + "\n")
    # Explicit destination, no overwrite and no automatic Memory/Skill registration.
    with target.open("x", encoding="utf-8") as handle:
        handle.write(text)
    candidate.update(status="promoted", destination=str(target), approved=True, source_tasks=sources)
    with store.conn:
        store.conn.execute("UPDATE learning_candidates SET payload=? WHERE task_id=?", (json.dumps(candidate), task_id))
    return str(target)
