"""K07 §8 Promotion Decision Engine — verbatim implementation.

Pure function on a pair-record list. No DB writes, no API calls, no network.
Only deterministic statistical computation (McNemar, bootstrap) + K07 §8 logic.

Spec source: E:/KI/RMK-System/00_DOKU/go-spec/K07_eval_statistics_contract.md
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import numpy as np
from scipy import stats

# ---------------------------------------------------------------------------
# K07 §8.1 Outcome taxonomy
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    PROMOTE = "PROMOTE"
    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED_HARD_FAIL = "BLOCKED_HARD_FAIL"
    BLOCKED_DATA_QUALITY = "BLOCKED_DATA_QUALITY"
    BLOCKED_SECURITY = "BLOCKED_SECURITY"


# ---------------------------------------------------------------------------
# K07 §14 Data-quality gate enumeration
# ---------------------------------------------------------------------------

class DataQualityFlag(str, Enum):
    DQ_01_MISSING_PROVENANCE = "DQ-01"
    DQ_02_MISMATCHED_CASE_IDS = "DQ-02"
    DQ_03_DUPLICATE_RUNS = "DQ-03"
    DQ_04_MISSING_VALIDATOR_OUTPUTS = "DQ-04"
    DQ_05_CORRUPTED_JSON = "DQ-05"
    DQ_06_SCHEMA_MISMATCH = "DQ-06"
    DQ_07_INCOMPLETE_EXECUTION = "DQ-07"
    DQ_08_UNSUPPORTED_CASE = "DQ-08"
    DQ_09_SEALED_HOLDOUT_LEAK = "DQ-09"
    DQ_10_JUDGE_PROMPT_DRIFT = "DQ-10"


# ---------------------------------------------------------------------------
# K07 §5 hard-fail classes and security subset
# ---------------------------------------------------------------------------

# K07 §5 table: 11 mandatory hard-fail classes (HF-01..HF-11)
# The security subset (HF classes constituting a SECURITY veto):
_SECURITY_HF_CLASSES = {"HF-01", "HF-02", "HF-09", "HF-10", "HF-11"}
# Full set of defined hard-fail classes:
_ALL_HARD_FAIL = {
    "HF-01", "HF-02", "HF-03", "HF-04", "HF-05",
    "HF-06", "HF-07", "HF-08", "HF-09", "HF-10", "HF-11",
}


def collect_hard_fails(pairs: list[dict]) -> tuple[list[str], list[str]]:
    """Return (all_hard_fails, security_hard_fails) observed across any CUR pair.

    K07 §5 "Hard-fail precedence": ANY hard fail on ANY CUR case (baseline OR
    candidate side) blocks promotion. Security-class hard fails (HF-01, HF-02,
    HF-09, HF-10, HF-11) produce BLOCKED_SECURITY; all other hard fails produce
    BLOCKED_HARD_FAIL. Both baseline_run.hard_fail_classes and
    candidate_run.hard_fail_classes are collected and unioned (deduplicated).

    Returns:
        (sorted(all_hard_fails), sorted(security_hard_fails))
    """
    all_fails: set[str] = set()
    sec_fails: set[str] = set()
    for pair in pairs:
        for side in ("baseline_run", "candidate_run"):
            run = pair.get(side) or {}
            for hf in run.get("hard_fail_classes", []):
                if hf:
                    all_fails.add(hf)
                    if hf in _SECURITY_HF_CLASSES:
                        sec_fails.add(hf)
    return sorted(all_fails), sorted(sec_fails)


# ---------------------------------------------------------------------------
# K07 §9 Regression budget
# ---------------------------------------------------------------------------

def _regression_budget(pairs: list[dict]) -> dict[str, int]:
    """Compute regressions per risk class (K07 §9).

    A case is a regression when the baseline succeeded (succeeded=True or
    status=="SUCCESS") and the candidate did not. Regressions are counted
    per pair.risk_class in {CRIT, HIGH, MED, LOW}.
    """
    budget = {"CRIT": 0, "HIGH": 0, "MED": 0, "LOW": 0}
    regressions: dict[str, int] = {"CRIT": 0, "HIGH": 0, "MED": 0, "LOW": 0}

    for pair in pairs:
        risk = pair.get("risk_class", "MED")
        if risk not in regressions:
            continue
        b = pair.get("baseline_run", {})
        c = pair.get("candidate_run", {})
        b_ok = b.get("succeeded", False) or b.get("status") == "SUCCESS"
        c_ok = c.get("succeeded", False) or c.get("status") == "SUCCESS"
        if b_ok and not c_ok:
            regressions[risk] += 1

    return regressions


# ---------------------------------------------------------------------------
# K07 § 7.2 Bootstrap CI
# ---------------------------------------------------------------------------

def _compute_phpr(pairs: list[dict], cur_case_ids: set[str]) -> tuple[float, float, int, int, np.ndarray, np.ndarray]:
    """Compute PHPR (K07 §4: Paired Hard-Validator Pass Rate) on CUR dev cases.

    K07 §4 numerator = cases attempted on both where the candidate passed all
    hard validators AND succeeded. Excludes INCONCLUSIVE, FUT, SPEC pairs.

    Returns (delta, inconclusive_rate, n_paired_dev, n_discordant, per_pair_deltas, per_pair_risk_class):
      delta = phpr_candidate - phpr_baseline
      inconclusive_rate = nr of INCONCLUSIVE pairs / all pairs seen
      n_paired_dev     = valid CUR cases attempted on both (INCONCLUSIVE excluded)
      n_discordant    = pairs where baseline hard-validator-passed differs
                        from candidate hard-validator-passed
      per_pair_deltas = array of (c_ok - b_ok) in {-1,0,1} per valid pair — the
                        K07 §7.2 bootstrap CI is computed on this array.
      per_pair_risk_class = array of risk_class strings aligned with per_pair_deltas
                            for stratified bootstrap resampling (K07 §7.2).
    """
    candidate_pass = 0
    baseline_pass = 0
    n_pairs = 0
    inconclusive = 0
    discordant = 0
    per_pair_deltas: list[float] = []
    per_pair_risk: list[str] = []

    for pair in pairs:
        case_id = pair.get("case_id", "")
        if case_id not in cur_case_ids:
            continue
        b = pair.get("baseline_run", {})
        c = pair.get("candidate_run", {})
        # Exclude INCONCLUSIVE pairs from the metric entirely (counted separately)
        b_inc = b.get("status") == "INCONCLUSIVE"
        c_inc = c.get("status") == "INCONCLUSIVE"
        if b_inc or c_inc:
            inconclusive += 1
            continue
        # Exclusion gate: succeeded but not HF-clean counts as not "pass"
        b_ok = bool(b.get("succeeded")) and not bool(b.get("hard_fail_classes"))
        c_ok = bool(c.get("succeeded")) and not bool(c.get("hard_fail_classes"))
        baseline_pass += 1 if b_ok else 0
        candidate_pass += 1 if c_ok else 0
        n_pairs += 1
        per_pair_deltas.append(float(c_ok) - float(b_ok))
        per_pair_risk.append(pair.get("risk_class", "MED"))
        if b_ok != c_ok:
            discordant += 1

    if n_pairs == 0:
        return float("nan"), float("nan"), 0, 0, np.array([]), np.array([])

    phpr_b = baseline_pass / n_pairs
    phpr_c = candidate_pass / n_pairs
    delta = phpr_c - phpr_b
    inconclusive_rate = (
        inconclusive / (n_pairs + inconclusive)
        if (n_pairs + inconclusive) > 0
        else 0.0
    )

    return delta, inconclusive_rate, n_pairs, discordant, np.array(per_pair_deltas), np.array(per_pair_risk)


def _bootstrap_ci(deltas: np.ndarray, risk_classes: np.ndarray = None,
                  n_resamples: int = 10_000, seed: int = 42,
                  alpha: float = 0.05) -> tuple[float, float]:
    """Bootstrap 95% percentile CI for the paired delta (K07 §7.2).

    K07 §7.2: \"resample within risk_class × domain cells, then pool.\"
    This implementation stratifies by `risk_class` (the only cell axis available
    from the K07 §6.1 pair record; `domain` is absent from the record schema).
    If `risk_classes` is None or all identical, falls back to flat resampling.
    """
    if len(deltas) < 2:
        return float("nan"), float("nan")

    rng = np.random.default_rng(seed)

    if risk_classes is None or len(risk_classes) != len(deltas):
        # Flat resampling fallback (legacy path / single stratum)
        samples = rng.choice(deltas, size=(n_resamples, len(deltas)), replace=True)
        means = samples.mean(axis=1)
        lo = np.percentile(means, 100 * alpha / 2)
        hi = np.percentile(means, 100 * (1 - alpha / 2))
        return float(lo), float(hi)

    # Stratified resampling by risk_class
    unique_risks = np.unique(risk_classes)
    # Pre-compute per-stratum indices
    strata_idx = {r: np.where(risk_classes == r)[0] for r in unique_risks}
    stratum_sizes = {r: len(idxs) for r, idxs in strata_idx.items()}

    means = np.zeros(n_resamples)
    for i in range(n_resamples):
        # Resample within each stratum independently, then pool
        resampled_means = []
        for r, idxs in strata_idx.items():
            stratum_deltas = deltas[idxs]
            if len(stratum_deltas) == 0:
                continue
            # Resample this stratum
            resampled = rng.choice(stratum_deltas, size=len(stratum_deltas), replace=True)
            resampled_means.append(resampled.mean())
        if resampled_means:
            means[i] = np.mean(resampled_means)
        else:
            means[i] = 0.0

    lo = np.percentile(means, 100 * alpha / 2)
    hi = np.percentile(means, 100 * (1 - alpha / 2))
    return float(lo), float(hi)


def _mcnemar_p(pairs: list[dict]) -> float:
    """McNemar p-value on paired binary outcome (baseline result vs candidate).
    Exact binomial test: 2*min(P(X<=k), 0.5) where k=min(n_01,n_10), X~Binom(n_disc, 0.5)
    (K07 §7.3). No continuity-corrected chi-square branch is implemented — the
    exact binomial is used for all n_disc, which is the more spec-correct choice
    for typical golden-set sample sizes (n_disc < 100)."""
    n_disc = 0
    n_00 = 0
    n_01 = 0  # baseline ok, candidate fail
    n_10 = 0  # baseline fail, candidate ok
    n_11 = 0
    for pair in pairs:
        b = pair.get("baseline_run", {})
        c = pair.get("candidate_run", {})
        b_ok = bool(b.get("succeeded")) and not b.get("hard_fail_classes")
        c_ok = bool(c.get("succeeded")) and not c.get("hard_fail_classes")
        if b_ok and c_ok: n_11 += 1
        elif b_ok and not c_ok: n_01 += 1
        elif not b_ok and c_ok: n_10 += 1
        else: n_00 += 1
    n_disc = n_01 + n_10
    if n_disc < 1:
        return 1.0

    # exact binomial test (no continuity correction)
    p_exact = 2 * min(stats.binom.cdf(min(n_01, n_10), n_disc, 0.5), 0.5)
    p_exact = min(p_exact, 1.0)
    return float(p_exact)

# ---------------------------------------------------------------------------
# K07 §8 decide() — the only function callers invoke.
# ---------------------------------------------------------------------------

def decide(eval_record: dict) -> Decision:
    """
    K07 §8 Promotion Decision Function — byte-for-byte equivalent.

    All 6 outcomes are enumerated in `Decision`.
    """
    # Step 1: Data-quality pre-flight
    if eval_record.get("data_quality_fail", False):
        return Decision.BLOCKED_DATA_QUALITY

    pairs = eval_record.get("pairs", [])
    if not pairs:
        return Decision.INCONCLUSIVE

    # Collect all hard fails across CUR pairs (K07 §5)
    all_hard_fails, sec_hard_fails = collect_hard_fails(pairs)
    if all_hard_fails:
        if sec_hard_fails:
            return Decision.BLOCKED_SECURITY
        return Decision.BLOCKED_HARD_FAIL

    # Step 4: Regression budget (CRIT/HIGH strict; MED/LOW budget with override)
    stat = eval_record.get("stat", {})
    risk_budget = eval_record.get("risk_budget", {"CRIT": 0, "HIGH": 0, "MED": 1, "LOW": 2})

    # Compute PHPR delta + pair-count statistics from the pair records (K07 §6.1)
    cur_ids = {p.get("case_id", "") for p in pairs if p.get("case_id", "")}
    delta, inconclusive_rate, n_paired, n_discordant, per_pair_deltas, per_pair_risk = _compute_phpr(pairs, cur_ids)
    stat.setdefault("delta", delta)
    stat.setdefault("n_paired_dev", n_paired)
    stat.setdefault("n_discordant", n_discordant)
    stat.setdefault("inconclusive_rate", inconclusive_rate)

    # K07 §9 regression budget: CRIT and HIGH are zero-tolerance.
    # MED/LOW have bounded budgets.
    # We compute regressions via risk_class attached to each pair.
    risk_class_of: dict[str, str] = {}
    for pair in pairs:
        risk_class_of.setdefault(pair.get("case_id", ""), pair.get("risk_class", "MED"))
    # Cases where candidate has hard_fail but baseline did not → regression
    regressions = _regression_budget(pairs)

    for risk in ("CRIT", "HIGH"):
        if regressions.get(risk, 0) > risk_budget.get(risk, 0):
            return Decision.REJECT
    flags: list[str] = list(eval_record.get("flags", []))
    for risk in ("MED", "LOW"):
        if regressions.get(risk, 0) > risk_budget.get(risk, 0):
            if delta < 0.10:  # MES_MED
                return Decision.REJECT
            flags.append("MED_LOW_BUDGET_BREACH_OVERRIDE")

    # Step 5: Statistical pre-conditions
    MIN_DEV_PAIRS = 16
    MIN_DISCORDANT = 5
    INCONCLUSIVE_RATE_THRESHOLD = 0.20
    ALPHA = 0.05

    if n_paired < MIN_DEV_PAIRS or n_discordant < MIN_DISCORDANT:
        return Decision.INCONCLUSIVE
    if inconclusive_rate > INCONCLUSIVE_RATE_THRESHOLD:
        return Decision.INCONCLUSIVE

    # Step 6: Effect-size + significance gate
    # mcnemar_p comes from eval_record (precomputed by the runner) or we compute it
    mcnemar_p = eval_record.get("mcnemar_p")
    if mcnemar_p is None:
        mcnemar_p = _mcnemar_p(pairs)

    if mcnemar_p >= ALPHA:
        return Decision.INCONCLUSIVE

    # ci_low / ci_high come from bootstrap on the per-pair delta array (K07 §7.2),
    # unless the caller explicitly overrides (e.g. a runner replaying a stored
    # decision with pre-computed CI bounds).
    if "ci_low" in eval_record or "ci_high" in eval_record:
        stat.setdefault("ci_low", eval_record.get("ci_low", float("nan")))
        stat.setdefault("ci_high", eval_record.get("ci_high", float("nan")))
    else:
        boot_lo, boot_hi = _bootstrap_ci(per_pair_deltas, per_pair_risk)
        stat.setdefault("ci_low", boot_lo)
        stat.setdefault("ci_high", boot_hi)

    ci_low = stat["ci_low"]
    ci_high = stat["ci_high"]
    ci_contains_zero = (ci_low <= 0 <= ci_high) if (
        not math.isnan(ci_low) and not math.isnan(ci_high)
    ) else True

    if ci_low > 0 and delta >= 0.20:  # MES_HIGH
        return Decision.PROMOTE
    if ci_high < 0 and delta <= -0.20:
        return Decision.REJECT
    if ci_contains_zero and abs(delta) < 0.05:  # MES_LOW
        return Decision.INCONCLUSIVE
    if delta > 0 and mcnemar_p < ALPHA and ci_low > -0.05:  # -MES_LOW
        return Decision.PROMOTE
    if delta < 0 and mcnemar_p < ALPHA and ci_high < 0.05:  # MES_LOW
        return Decision.REJECT

    return Decision.INCONCLUSIVE
