"""Rewind checkpoint default-on toggle (K04, RECOMMENDED, not gating release).

Checks config.yaml for:
  rewind:
    checkpoints_enabled: true/false (default: false until K09 GO)
The toggle is operator-toggleable; tests verify the config read path only.
"""

from __future__ import annotations
from typing import Optional

def rewind_checkpoints_enabled(cfg: Optional[dict] = None) -> bool:
    """Read the Rewind checkpoint default-on toggle (K04 RECOMMENDED).
    Returns True only if the config explicitly permits it. Default: False."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly()
        except Exception:
            return False
    rewind = (cfg or {}).get("rewind") or {}
    return bool(rewind.get("checkpoints_enabled", False))
