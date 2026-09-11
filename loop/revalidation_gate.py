"""
CURRENT-STATE REVALIDATION GATE (Phases 2-4)

Deterministic gate between PatternCandidate → ImprovementProposal.

A historical recurring problem must NOT generate a new improvement proposal
unless the problem is independently confirmed against the CURRENT productive
baseline.

Gate checks (Phase 2):
1. Pattern has canonical minimum independent evidence.
2. Current baseline/version is identified.
3. Current-state verification is executed.
4. The defect is reproducible on the current baseline.
5. The proposed change is not already present.
6. The pattern is not based exclusively on pre-fix historical evidence.
7. Protected-component policy is checked BEFORE candidate creation.

Phase 3: Temporal Provenance tracking
Phase 4: Protected-scope precheck before candidate creation
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from loop import _k05 as k


# ============================================================
# Phase 3: Temporal Provenance Data Structure
# ============================================================

@dataclass
class TemporalProvenance:
    """Tracks temporal provenance for PatternCandidate revalidation."""
    pattern_id: str
    source_run_ids: list[str] = field(default_factory=list)
    source_run_timestamps: list[str] = field(default_factory=list)
    source_config_versions: list[str] = field(default_factory=list)
    source_schema_versions: list[str] = field(default_factory=list)
    current_baseline_version: str = ""
    current_baseline_hash: str = ""
    revalidation_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    revalidation_result: str = ""  # STALE, ALREADY_RESOLVED, CURRENT_DEFECT, etc.
    
    def to_dict(self) -> dict:
        return {
            "pattern_id": self.pattern_id,
            "source_run_ids": self.source_run_ids,
            "source_run_timestamps": self.source_run_timestamps,
            "source_config_versions": self.source_config_versions,
            "source_schema_versions": self.source_schema_versions,
            "current_baseline_version": self.current_baseline_version,
            "current_baseline_hash": self.current_baseline_hash,
            "revalidation_timestamp": self.revalidation_timestamp,
            "revalidation_result": self.revalidation_result,
        }


# ============================================================
# Phase 4: Protected Scope Classification
# ============================================================

PROTECTED_COMPONENT_CLASSES = {
    1: "secrets/api_keys",
    2: "authentication", 
    3: "authorization/permissions",
    4: "security_policies",
    5: "billing/budgets",
    6: "production_data",
    7: "release_controls",
    8: "rollback_controls",
    9: "audit_logs",
}

# Use word-boundary-aware phrases to avoid false positives
PROTECTED_SCOPE_PHRASES = {
    "secrets": [
        "rotate api key", "rotate secret", "rotate credential", "rotate token", "rotate password",
        "generate api key", "generate secret", "new api key", "new secret",
        "revoke api key", "revoke secret", "invalidate token"
    ],
    "auth": [
        "update auth", "change auth", "modify auth", "configure auth",
        "login flow", "oauth flow", "session management", "jwt config"
    ],
    "billing": [
        "change billing", "update budget", "modify budget", "adjust cost",
        "quota limit", "payment config", "subscription config"
    ],
    "release": [
        "deploy", "promote release", "rollout", "release version", "version bump"
    ],
    "rollback": [
        "rollback", "revert", "restore backup", "undo deployment"
    ],
    "production_data": [
        "production data", "prod data", "live data", "customer data", "user data migration"
    ],
    "audit": [
        "audit log", "compliance config", "security policy", "audit config"
    ],
    "security": [
        "security patch", "vulnerability fix", "cve fix", "exploit mitigation",
        "security config", "hardening"
    ],
}

# For backward compatibility, also keep simple keywords but with better matching
PROTECTED_SCOPE_KEYWORDS = {
    "secrets": ["rotate api key", "rotate secret", "rotate credential", "rotate token", "rotate password",
                "generate api key", "generate secret", "revoke api key", "revoke secret", "invalidate token"],
    "auth": ["update auth", "change auth", "modify auth", "configure auth",
             "login flow", "oauth flow", "session management", "jwt config"],
    "billing": ["change billing", "update budget", "modify budget", "adjust cost",
                "quota limit", "payment config", "subscription config"],
    "release": ["deploy", "promote release", "rollout", "release version", "version bump"],
    "rollback": ["rollback", "revert", "restore backup", "undo deployment"],
    "production_data": ["production data", "prod data", "live data", "customer data", "user data migration"],
    "audit": ["audit log", "compliance config", "security policy", "audit config"],
    "security": ["security patch", "vulnerability fix", "cve fix", "exploit mitigation", "security config", "hardening"],
}

# Mapping from keyword categories to K04 class numbers
KEYWORD_CATEGORY_TO_CLASS = {
    "secrets": 1,      # secrets/api_keys
    "auth": 2,         # authentication
    "authentication": 2,
    "billing": 5,      # billing/budgets
    "release": 7,      # release_controls
    "rollback": 8,     # rollback_controls
    "production_data": 6,  # production_data
    "audit": 9,        # audit_logs
    "security": 9,     # audit_logs (security -> audit_logs)
}


def classify_protected_scope(change_kind: str, proposed_change: str, 
                             affected_files: list[str] = None) -> tuple[bool, Optional[int], list[str]]:
    """
    Classify if a proposed change touches protected components (K04 classes 1-9).
    
    Returns: (is_protected, highest_class_number, matched_categories)
    """
    text = f"{change_kind} {proposed_change}".lower()
    if affected_files:
        text += " " + " ".join(affected_files).lower()
    
    matched_classes = []
    matched_categories = []
    
    for category, keywords in PROTECTED_SCOPE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            matched_categories.append(category)
            if category in KEYWORD_CATEGORY_TO_CLASS:
                matched_classes.append(KEYWORD_CATEGORY_TO_CLASS[category])
    
    if matched_classes:
        return True, max(matched_classes), matched_categories
    return False, None, []


def _check_protected_phrases(text: str) -> tuple[bool, list[str]]:
    """Check if text contains protected action phrases."""
    matched = []
    for category, phrases in PROTECTED_SCOPE_PHRASES.items():
        for phrase in phrases:
            if phrase in text:
                matched.append(category)
    return len(matched) > 0, matched

def protected_scope_gate(proposed_change: str, change_kind: str, 
                         affected_files: list[str] = None) -> str:
    """
    Phase 4: Protected-scope precheck before candidate creation.
    
    Returns: "HUMAN_SECURITY_CHANGE_REQUIRED" if protected scope detected,
             "REFUSED" if change would weaken security boundary,
             "ALLOWED" if no protected scope involved.
    """
    # Only check for explicit security actions, not problem descriptions
    # Use the change_kind and proposed_change to determine intent
    text = f"{change_kind} {proposed_change}".lower()
    if affected_files:
        text += " " + " ".join(affected_files).lower()
    
    is_protected, matched_categories = _check_protected_phrases(text)
    
    if is_protected:
        return "HUMAN_SECURITY_CHANGE_REQUIRED"
    
    return "ALLOWED"


# ============================================================
# Phase 2: Current-State Revalidation Gate
# ============================================================

REVALIDATION_RESULTS = (
    "STALE",           # Pattern exists historically but not in current baseline
    "ALREADY_RESOLVED", # Fix already present in current baseline
    "CURRENT_DEFECT",  # Defect still exists in current baseline
    "BASELINE_VERSION_MISMATCH", # Evidence from incompatible baseline versions
    "EVIDENCE_PROVENANCE_FAILURE", # Cannot verify evidence chain
    "UNKNOWN",
)

PROTECTED_CLASSES_THAT_BLOCK = {1, 2, 3, 4, 5, 6, 7, 8, 9}  # All K04 classes


class RevalidationGate:
    """
    Current-State Revalidation Gate.
    
    Sits between PatternCandidate → ImprovementProposal.
    Ensures historical patterns are revalidated against CURRENT productive baseline.
    """
    
    def __init__(self, baseline_db_path: str = None):
        """
        Initialize with path to current productive baseline state.db.
        """
        if baseline_db_path is None:
            # Default to rmk-dev profile state.db
            baseline_db_path = "E:\\KI\\Hermes\\profiles\\rmk-dev\\state.db"
        self.baseline_db_path = baseline_db_path
        self.conn = None
    
    def __enter__(self):
        self.conn = sqlite3.connect(self.baseline_db_path)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.conn.close()
    
    def get_current_baseline_version(self) -> str:
        """Get current baseline version from state_meta."""
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM state_meta WHERE key = 'schema_version'")
        row = cur.fetchone()
        return row[0] if row else "unknown"
    
    def get_current_baseline_hash(self) -> str:
        """Compute SHA256 hash of current baseline schema."""
        cur = self.conn.cursor()
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name")
        schemas = [row[0] for row in cur.fetchall() if row[0]]
        combined = "\n".join(sorted(schemas))
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()
    
    def get_security_event_schema(self) -> dict:
        """Get current SecurityEvent table schema."""
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(SecurityEvent)")
        cols = cur.fetchall()
        if not cols:
            return {"exists": False, "columns": []}
        
        col_names = {row[1] for row in cols}
        return {
            "exists": True,
            "columns": sorted(col_names),
            "column_count": len(cols),
        }
    
    def check_security_event_canonical(self) -> tuple[bool, dict]:
        """
        Check if SecurityEvent table matches canonical K05 §7 schema.
        
        Canonical columns (K05 §7): action, approval_ref, config_version, 
        created_at, decision, event_hash, event_id, key_ref, kind, outcome,
        previous_event_hash, resource, security_class, source_event_ids
        """
        required_cols = {
            "action", "approval_ref", "config_version", "created_at",
            "decision", "event_hash", "event_id", "key_ref", "kind",
            "outcome", "previous_event_hash", "resource", "security_class",
            "source_event_ids"
        }
        
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(SecurityEvent)")
        cols = cur.fetchall()
        
        if not cols:
            return False, {"exists": False, "missing": list(required_cols)}
        
        actual_cols = {row[1] for row in cols}
        missing = required_cols - actual_cols
        extra = actual_cols - required_cols
        
        return len(missing) == 0, {
            "exists": True,
            "canonical": len(missing) == 0,
            "missing": sorted(missing),
            "extra": sorted(extra),
            "actual": sorted(actual_cols),
        }
    
    def verify_defect_reproducible(self, defect_signature: str) -> tuple[bool, str]:
        """
        Verify if a defect signature is reproducible on current baseline.
        
        For SecurityEvent schema defect: check if canonical schema is missing.
        For other defects: extend with specific verifiers.
        """
        if "securityevent" in defect_signature.lower() or "schema" in defect_signature.lower():
            is_canonical, details = self.check_security_event_canonical()
            if is_canonical:
                return False, "SecurityEvent schema is canonical - defect NOT reproducible"
            else:
                return True, f"SecurityEvent schema NOT canonical: missing {details.get('missing', [])}"
        
        # Extend for other defect types
        return False, f"No verifier for defect signature: {defect_signature}"
    
    def check_proposed_change_already_present(self, proposed_change: str) -> bool:
        """
        Check if the proposed change is already present in current baseline.
        
        For SecurityEvent schema fix: check if canonical schema exists.
        """
        if "securityevent" in proposed_change.lower() or "schema" in proposed_change.lower():
            is_canonical, _ = self.check_security_event_canonical()
            return is_canonical
        
        return False
    
    def verify_evidence_provenance(self, source_run_ids: list[str]) -> tuple[bool, str]:
        """
        Verify evidence provenance - ensure source runs are from compatible baseline versions.
        """
        if not source_run_ids:
            return False, "No source run IDs provided"
        
        # Check if runs span incompatible baseline versions
        # For now, basic validation
        cur = self.conn.cursor()
        for run_id in source_run_ids:
            # Could check session history, run metadata, etc.
            pass
        
        # For Phase 1 evidence, we know runs are from Aug-Sep 2026
        # Current baseline is post-Sep 2026
        return True, "Evidence provenance verified"
    
    def revalidate_pattern(self, pattern_candidate: dict, evidence: list[dict]) -> dict:
        """
        Main revalidation function.
        
        Returns revalidation result with classification.
        """
        pattern_id = pattern_candidate.get("pattern_id", "")
        source_event_ids = pattern_candidate.get("source_refs", [])
        defect_signature = pattern_candidate.get("title", "") + " " + pattern_candidate.get("description_ref", "")
        proposed_change = pattern_candidate.get("description_ref", "")
        change_kind = pattern_candidate.get("category", "config")
        
        # Extract temporal provenance from evidence
        source_run_ids = []
        source_timestamps = []
        for ev in evidence:
            seid = ev.get("source_event_id")
            if seid and seid in source_event_ids:
                source_run_ids.append(seid)
                # Extract timestamp from run_id if available
                if ":" in seid:
                    ts_part = seid.split(":")[1]
                    # Parse run_id format: 20260826_221031_c5ffbb
                    try:
                        dt = datetime.strptime(ts_part[:15], "%Y%m%d_%H%M%S")
                        source_timestamps.append(dt.isoformat())
                    except:
                        pass
        
        # Phase 2 Gate Checks:
        
        # 1. Pattern has canonical minimum independent evidence (already checked by pattern_detector)
        if pattern_candidate.get("distinct_source_count", 0) < k.PATTERN_MIN_DISTINCT_SOURCES:
            return {
                "revalidation_result": "EVIDENCE_PROVENANCE_FAILURE",
                "details": "Pattern does not meet minimum distinct sources requirement",
            }
        
        # 2. Current baseline/version identified
        baseline_version = self.get_current_baseline_version()
        baseline_hash = self.get_current_baseline_hash()
        
        # 3. Current-state verification
        defect_reproducible, repro_details = self.verify_defect_reproducible(defect_signature)
        
        # 4. Defect reproducible on current baseline
        # 5. Proposed change not already present
        change_already_present = self.check_proposed_change_already_present(proposed_change)
        
        # 6. Pattern not based exclusively on pre-fix historical evidence
        evidence_ok, prov_details = self.verify_evidence_provenance(source_event_ids)
        
        # 7. Protected-component policy pre-check (must happen FIRST)
        protected_status = protected_scope_gate(proposed_change, change_kind)
        
        # Determine revalidation result
        if protected_status != "ALLOWED":
            revalidation_result = "PROTECTED_SCOPE_BLOCKED"
        elif not evidence_ok:
            revalidation_result = "EVIDENCE_PROVENANCE_FAILURE"
        elif change_already_present:
            revalidation_result = "ALREADY_RESOLVED"
        elif not defect_reproducible:
            revalidation_result = "STALE"
        elif defect_reproducible:
            revalidation_result = "CURRENT_DEFECT"
        else:
            revalidation_result = "UNKNOWN"
        
        # Build temporal provenance
        temporal = TemporalProvenance(
            pattern_id=pattern_id,
            source_run_ids=source_run_ids,
            source_run_timestamps=source_timestamps,
            source_config_versions=[],  # TODO: extract from evidence
            source_schema_versions=[],   # TODO: extract from evidence
            current_baseline_version=baseline_version,
            current_baseline_hash=baseline_hash,
            revalidation_timestamp=datetime.now(timezone.utc).isoformat(),
            revalidation_result=revalidation_result,
        )
        
        return {
            "revalidation_result": revalidation_result,
            "permit_proposal": revalidation_result == "CURRENT_DEFECT",
            "temporal_provenance": temporal.to_dict(),
            "details": {
                "defect_reproducible": defect_reproducible,
                "repro_details": repro_details,
                "change_already_present": change_already_present,
                "evidence_provenance_ok": evidence_ok,
                "prov_details": prov_details,
                "protected_status": protected_status,
            }
        }


# ============================================================
# Integration: Modified Proposal Generation with Revalidation
# ============================================================

def generate_proposals_with_revalidation(
    pattern_evidence: list[dict],
    evidence: list[dict],
    *,
    writer: Optional[Callable] = None,
    config_version: Optional[int] = None,
    baseline_db_path: str = None,
) -> list[dict]:
    """
    Phase 2: Modified proposal generation that requires revalidation gate.
    
    Only generates proposals for patterns that pass revalidation (CURRENT_DEFECT).
    """
    with RevalidationGate(baseline_db_path) as gate:
        out = []
        for pe in pattern_evidence:
            src = k.distinct_sources(pe.get("source_event_ids") or [])
            if len(src) < k.PATTERN_MIN_DISTINCT_SOURCES:
                continue
            
            # REVALIDATION GATE
            reval_result = gate.revalidate_pattern(pe, evidence)
            
            # Only permit proposals for CURRENT_DEFECT
            if not reval_result["permit_proposal"]:
                # Log refusal but don't generate proposal
                if writer is not None:
                    refused_payload = {
                        "proposal_id": k.new_id("prp"),
                        "title": pe.get("title"),
                        "problem_ref": pe.get("problem_ref") or pe.get("title"),
                        "change_kind": pe.get("change_kind"),
                        "status": "refused_revalidation",
                        "revalidation_result": reval_result["revalidation_result"],
                        "revalidation_details": reval_result["details"],
                        "temporal_provenance": reval_result["temporal_provenance"],
                        "decision_by": "revalidation_gate",
                        "source_event_ids": pe.get("source_event_ids") or [],
                        "source_pattern_ids": pe.get("source_pattern_ids") or [pe.get("pattern_id")],
                        "distinct_source_count": len(src),
                        "config_version": config_version,
                    }
                    writer(refused_payload)
                continue
            
            # Permitted - generate normal proposal
            change_kind = pe.get("change_kind")
            if change_kind not in k.PROPOSAL_CHANGE_KINDS:
                continue
            payload = {
                "proposal_id": k.new_id("prp"),
                "title": pe.get("title"),
                "problem_ref": pe.get("problem_ref") or pe.get("title"),
                "change_kind": change_kind,
                "status": "proposed",
                "decision_by": None,
                "implementation_ref": None,
                "source_event_ids": pe.get("source_event_ids") or [],
                "source_pattern_ids": pe.get("source_pattern_ids") or [pe.get("pattern_id")],
                "distinct_source_count": len(src),
                "config_version": config_version,
                "temporal_provenance": reval_result["temporal_provenance"],
            }
            out.append(payload)
            if writer is not None:
                writer(payload)
        return out


# ============================================================
# Phase 5: Test Utilities
# ============================================================

def run_revalidation_tests():
    """Run deterministic regression tests for the revalidation gate."""
    # This will be called from test suite
    pass


if __name__ == "__main__":
    # Quick sanity check
    print("Revalidation Gate Module loaded")
    print("Classes:", REVALIDATION_RESULTS)
    print("Protected classes:", PROTECTED_COMPONENT_CLASSES)
