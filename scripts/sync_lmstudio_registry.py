"""Reconcile registry.json's `lmstudio` rows against LM Studio's live catalog.

Queries LM Studio's native ``/api/v1/models`` (the same discovery path the
runtime uses for admission/loading) and disables any registry row whose
``model_id`` is not currently installed in LM Studio. Never hardcodes model
names; the live catalog is the only source of truth. Newly installed models
that are not yet registered are reported but not auto-added (capability/
routing/coding scores need an operator's classification, not a guess).

Usage: <venv python> scripts/sync_lmstudio_registry.py [--apply]
Without --apply, prints the plan only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.models_local import probe_lmstudio_models  # noqa: E402

REGISTRY_PATH = Path(r"E:\KI\Hermes\routing\registry.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes to registry.json")
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    args = parser.parse_args()

    registry_path = Path(args.registry)
    document = json.loads(registry_path.read_text(encoding="utf-8"))

    try:
        live = probe_lmstudio_models(base_url="http://127.0.0.1:1234/v1", timeout=5.0)
    except Exception as exc:
        print(f"LM Studio probe failed: {exc}", file=sys.stderr)
        return 1
    if live is None:
        print("LM Studio unreachable at http://127.0.0.1:1234/v1 - no changes made.", file=sys.stderr)
        return 1

    live_set = set(live)
    changed = False
    for row in document["models"]:
        if row.get("provider") != "lmstudio":
            continue
        model_id = row.get("model_id", "")
        stale = model_id not in live_set
        if stale and row.get("available", True):
            print(f"DISABLE stale: {model_id} (not in live LM Studio catalog)")
            row["available"] = False
            changed = True
        elif not stale and not row.get("available", True):
            print(f"RE-ENABLE: {model_id} (present again in live LM Studio catalog)")
            row["available"] = True
            changed = True
        else:
            print(f"OK: {model_id} (available={row.get('available')})")

    registered_ids = {r["model_id"] for r in document["models"] if r.get("provider") == "lmstudio"}
    for key in live_set - registered_ids:
        print(f"UNREGISTERED (installed, not in registry - needs operator classification): {key}")

    if not changed:
        print("No changes needed.")
        return 0

    if args.apply:
        from agent.routing.admission import parse_registry

        parse_registry(document)  # validate before writing
        registry_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(f"Applied. Wrote {registry_path}")
    else:
        print("Dry run only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
