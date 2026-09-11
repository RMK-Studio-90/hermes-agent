"""Feature flag plumbing (K08 §17.1/§17.2/§17.3). BUILD-04 already
implements delegation.operating_brain.enabled via config_enabled() in
hermes_cli/operating_brain.py. This module adds per-profile semantics.

Per-profile (K08 §17.2): config.yaml may declare
  delegation:
    operating_brain:
      enabled: rmk.ob.v1        # default for all profiles
      profiles:
        rmk-experiment: rmk.ob.v1
        rmk-control: false
If no per-profile entry, falls back to `delegation.operating_brain.enabled`.
"""

from __future__ import annotations
from typing import Optional

def effective_mode(profile_name: Optional[str] = None, *, cfg: Optional[dict] = None) -> Optional[str]:
    """Resolve the Operating-Brain mode for a named profile (K08 §17.2).
    Returns None (disabled), a treatment id string, or raises ValueError
    for unknown modes (caller must refuse spawn per §17.3)."""
    from hermes_cli.operating_brain import config_enabled, OPERATING_BRAIN_ID, OperatingBrainModeError
    if cfg is None:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly().get("delegation") or {}
    ob = cfg.get("operating_brain") or {}
    # Per-profile override
    if profile_name:
        profiles = ob.get("profiles") or {}
        if profile_name in profiles:
            mode = config_enabled({"operating_brain": {"enabled": profiles[profile_name]}})
            if mode and mode not in (None, OPERATING_BRAIN_ID):
                raise OperatingBrainModeError(
                    f"profile {profile_name!r}: unknown operating_brain.enabled={mode!r}"
                )
            return mode
    # Global default
    mode = config_enabled({"operating_brain": ob})
    if mode and mode not in (None, OPERATING_BRAIN_ID):
        raise OperatingBrainModeError(
            f"global: unknown operating_brain.enabled={mode!r}"
        )
    return mode


def immediate_disable(profile_name: Optional[str] = None, *, cfg: Optional[dict] = None) -> bool:
    """Test whether §17.3 hard kill switch is engaged for a profile.
    Returns True if the effective mode is disabled after the override."""
    return effective_mode(profile_name, cfg=cfg) is None
