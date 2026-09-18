# HERMES DREAMING V1 — RUNTIME VERIFICATION & HARDENING

Scope: profile `rmk-knowledge`, job id `f10cb30ded6d` ("RMK-KNOWLEDGE Dreaming — Session Reflection"), SOUL.md point 10. Read-only investigation per instructions; no implementation changes made in this pass. Repository `E:\KI\Hermes\hermes-agent`, machine `rmk`.

---

## 1. Executive Verdict

**FAIL**

Not because nothing works — the job is correctly *registered* and the scheduler is demonstrably alive — but because two of the mechanisms the design explicitly relied on are provably non-functional or unenforced *as configured right now*, independent of whether a live trigger could be observed:

1. The "only re-process sessions since the last dream" safeguard has **no enforcement in code** — it is pure prompt instruction. `NO_INCREMENTAL_SESSION_CURSOR` applies.
2. The job's own prompt authorizes direct `memory`-tool writes for "confirmed" facts, but **this profile has the underlying memory store disabled** (`memory_enabled: false`, `user_profile_enabled: false` in `config.yaml`). Every such call will resolve to `agent._memory_store is None` and fail with a tool error — the promotion path in the design is dead on arrival, not merely "unreviewed."

On top of these two proven defects, the controlled runtime test (Phase 5) and the idempotency test (Phase 6) could **not** be executed this session because the Windows machine's foreground surface is a UIPI-protected, non-interactive process consistent with a locked/secure desktop (evidence in §4). Per the instruction "if any acceptance gates are unproven, the final verdict cannot be PASS," this alone would already block a PASS; combined with the two proven defects above, the correct verdict is FAIL rather than the softer BLOCKED — BLOCKED would understate that specific things are already known to be broken, not merely untested.

What *is* proven and correct: the job exists with valid config, the SOUL.md exception is in place and scoped correctly, the scheduler ticker for this profile is alive and ticking in real time, the job has never misfired (it hasn't had a first scheduled occurrence yet), and a `write_file` core tool exists so the report half of the design (the Markdown proposal ledger) has a real write path — the report-writing mechanism is not affected by the memory-store defect.

---

## 2. Current Architecture (as installed, not as designed)

| Component | File | State |
|---|---|---|
| Job definition | `profiles/rmk-knowledge/cron/jobs.json`, id `f10cb30ded6d` | Present, `enabled: true`, `schedule: "30 4 * * *"`, `next_run_at: 2026-09-09T04:30:00+02:00`, `last_run_at: null`, `failure_streak: 0` |
| Governance | `profiles/rmk-knowledge/SOUL.md`, point 10 | Present — scopes the job to session-history reflection only, explicitly forbids `knowledge_store` access during dreaming runs |
| Session read path | `session_search` tool (`agent/inline_tool_executors.py`, `tools/session_search_tool.py`) | Available (core tool, `_HERMES_CORE_TOOLS`), but has **no timestamp/since parameter** |
| Bookmark | `cron/dream_bookmark.json` (referenced by prompt) | Not a real cursor — a file the LLM is *instructed* to read/write; nothing in code enforces or even validates it |
| Direct-write path | `memory` tool (`tools/memory_tool.py`) | Available as a core tool schema, but **backed by no store** for this profile (`store=None`) |
| Report path | `write_file` (core tool, `toolsets.py`) → `reports/dream_proposals/<date>.md` | Available, functional, unaffected by the memory defect |
| Custom toolset | `rmk_knowledge` → `knowledge_store` (plugin `rmk-knowledge-guard/__init__.py`) | Available to the profile in general, but **explicitly forbidden by this job's own prompt** ("Kein knowledge_store-Zugriff in diesem Lauf") |
| Scheduler | `cron/scheduler.py` tick loop | Alive — `ticker_heartbeat` / `ticker_last_success` both stamped within the same minute as this check (see §5) |

---

## 3. Evidence Table

| # | Claim | Evidence | Verdict |
|---|---|---|---|
| E1 | Job is registered and enabled | `cron/jobs.json`, id `f10cb30ded6d`, `"enabled": true`, `"state": "scheduled"` | PROVEN |
| E2 | Scheduler process for this profile is alive right now | `profiles/rmk-knowledge/cron/ticker_heartbeat` = `1788846540.965…`, `ticker_last_success` = `1788846540.969…`, checked against `date -u` = `Tue Sep 8 05:49:01 UTC 2026` — both within ~20s of the read | PROVEN |
| E3 | Job has not misfired / has no failure history | `last_run_at: null`, `last_status: null`, `failure_streak: 0`, `created_at: 2026-09-08T06:37:25+02:00` (this morning), `next_run_at: 2026-09-09T04:30:00+02:00` | PROVEN — job is simply too new to have run; not evidence it *will* succeed |
| E4 | No incremental session cursor exists at the tool level | `tools/session_search_tool.py:533` — full signature of `session_search()` has no `since`/timestamp parameter of any kind; the "last dream bookmark" logic exists only as prose in the job prompt | **`NO_INCREMENTAL_SESSION_CURSOR`** confirmed |
| E5 | Cron-sourced sessions (including the job's own prior runs) are not excluded from BROWSE-mode recall | `tools/session_search_tool.py:400` `_list_recent_sessions()` calls `list_recent_sessions_bounded(exclude_sources=_HIDDEN_SESSION_SOURCES, …)` where `_HIDDEN_SESSION_SOURCES = ("kanban","subagent","tool")` (line 20) — `"cron"` is only in `_DEMOTED_SESSION_SOURCES` (line 29, sort-order demotion, not exclusion), applied in the `_discover` path (line 275), not in BROWSE | `DREAM_REPROCESS_LOOP` risk confirmed as real and unmitigated |
| E6 | Memory tool has no live store for this profile | `profiles/rmk-knowledge/config.yaml`: `memory: memory_enabled: false, user_profile_enabled: false`; `agent/agent_init.py:1259` only constructs `agent._memory_store` `if agent._memory_enabled or agent._user_profile_enabled`; `tools/memory_tool.py:135` `if store is None: return tool_error("Memory is not available…")` | PROVEN — every `memory` call from this job will fail cleanly, not silently succeed |
| E7 | `write_approval` gate for `memory` subsystem is off | `tools/write_approval.py:43` `write_approval_enabled()` reads `<subsystem>.write_approval`, default `False`; not set in `config.yaml` | Confirmed — but secondary to E6 (nothing to gate; the store doesn't exist) |
| E8 | No cron-specific misfire override for this job | `profiles/rmk-knowledge/config.yaml` has no `cron:` section and no `misfire` key | Global default applies: `DEFAULT_MISFIRE_GRACE_MINUTES = 10` (`cron/scheduler_provider.py:20`) plus jobs.py's half-period-clamped-to-[120s,2h] due-classification |
| E9 | Report-writing mechanism exists independent of the memory defect | `toolsets.py:11-26`, `_HERMES_CORE_TOOLS` includes `write_file`, `read_file`, `patch`, `search_files`, `execute_code`, `terminal` | PROVEN — the report half of the design is not blocked by E6 |
| E10 | The one custom tool this profile has for structured writes is explicitly forbidden to this job | `plugins/rmk-knowledge-guard/__init__.py:294-304`, `SCHEMA = {'name': 'knowledge_store', … 'No web, shell or arbitrary writes.'}`, registered to toolset `rmk_knowledge`; job prompt: "Kein knowledge_store-Zugriff in diesem Lauf" | Confirmed by design (intentional exclusion) — but confirms the *only* write path available to this job is `write_file`/`memory`, not `knowledge_store` |
| E11 | Machine is not currently reachable via UI for a live trigger | See §4 | BLOCKED |

---

## 4. Runtime Test (Phase 5) — BLOCKED

No CLI trigger subcommand exists (`hermes_cli/cron.py`, `cli.py` — confirmed absent, this and prior session). The only real dispatch path is the Desktop "Trigger now" UI action, which requires the screen to be interactively reachable.

Diagnostic sequence this session:
1. `computer_screenshot()` on both attached monitors ("LG ULTRAFINE", "2460G5") returned a uniform flat frame with **zero visible windows**, even after granting both `Hermes` and `Lockapp` into the session allowlist (i.e., neither app is the reason content is hidden — nothing is rendering because nothing in the allowlist owns the visible surface).
2. A single, inert `Escape` keypress (chosen specifically because it enters no credentials, dismisses nothing, and is a no-op on a Windows lock screen) was rejected by the automation layer itself with: *"'(name withheld)' is not in the allowed applications and is currently in front… If this is an elevated process (Task Manager, a UAC prompt, or an installer running as administrator), it cannot be controlled — Windows UIPI blocks input from lower-integrity processes."*

This is materially stronger evidence than the first audit's plain "screen looks dark" observation: a UIPI-blocked, name-withheld foreground process is the same integrity-boundary behavior Windows applies to its own secure desktop (the winlogon/lock-screen surface), which is deliberately designed to reject exactly this kind of external input and to resist screen-content capture by remote-automation tools. Two independent signals (capture returns a flat placeholder; keyboard input is rejected at the OS integrity boundary) now agree.

**No unlock was attempted**, consistent with the standing instruction from the first audit not to bypass a locked screen. Phase 5 and, by direct dependency, Phase 6 (idempotency) are classified **NOT_TESTABLE** this session. Reproduction for the user: unlock the machine, open Hermes Desktop → the `rmk-knowledge` cron list → "RMK-KNOWLEDGE Dreaming — Session Reflection" → Trigger now, then re-run this audit's Phase 5/6 checklist below.

**Phase 5/6 checklist for the user's own manual run** (unchanged from the original protocol, listed here so the eventual manual test is complete):
- Before triggering: record `cron/executions.db` row count, `sessions/` file count, `reports/dream_proposals/` contents, and `cron/dream_bookmark.json` (if it exists yet — it doesn't, this is a first run).
- Trigger exactly once via the real UI path.
- After: confirm exactly one new `executions.db` row with `status=success` or a clearly classified failure; confirm the job's own resulting session is tagged `source=cron`; confirm `reports/dream_proposals/<today>.md` was created with the sessions-read count and category breakdown the prompt specifies; confirm any `memory` tool call in the transcript returned the `"Memory is not available…"` error (this is the expected, not the anomalous, outcome given E6) and that the LLM's final report does not claim a successful memory write it did not make; confirm `dream_bookmark.json` was created/updated.
- Run it a second time immediately with no new sessions in between and confirm `[SILENT]` — this is the idempotency test (Phase 6), and it is the one place the *lack* of a hard cursor (E4/E5) is most likely to surface as a visible bug: watch specifically for the job re-reading and re-summarizing its own prior dream-run sessions.

---

## 5. Idempotency Result — NOT_TESTABLE (blocked by §4)

Cannot be executed this session for the reason above. Code-level risk assessment stands independent of the live test: because `session_search`/BROWSE mode does not exclude `cron`-sourced sessions (E5) and there is no enforced cursor (E4), a second run with zero new *human* sessions could still see and re-summarize its own first run's session as if it were new input, unless the LLM's own prompt-level judgment ("seit dem letzten Dream-Bookmark") correctly self-filters — which is exactly the kind of LLM-judgment-as-safety-control the audit instructions say not to accept.

---

## 6. Memory Safety Result

**Classification: `RUNTIME_BLOCKER_TOOL_UNAVAILABLE` for the direct-write path (not `UNSAFE_FOR_AUTOPROMOTION` as originally hypothesized before this profile's `config.yaml` was checked).**

The original concern was that `write_approval` defaults to off (E7), meaning any `memory` call would go through un-gated. That is still true as a general Hermes fact, but for *this specific profile* it is superseded by a more fundamental problem: `memory_enabled: false` and `user_profile_enabled: false` mean `agent._memory_store` is never constructed (`agent/agent_init.py:1259`), so `memory_tool()` always hits its `store is None` branch (`tools/memory_tool.py:135`) and returns a clean error, never a write.

Net effect: no unsafe autopromotion can currently happen — but not because a safety control is holding it back. It's because the mechanism the design's "Kategorie 2 und 3 → direkt speichern" tier depends on is switched off at the config level for this profile. The job prompt's promise of a fast-path for well-confirmed facts is currently unfulfillable; every attempt will consume a tool round-trip and return an error. **Recommended promotion mode: `PROPOSAL_ONLY_RECOMMENDED`** — matching the audit's own default — achieved by either (a) enabling `memory_enabled` for `rmk-knowledge` and *then* turning on `write_approval` for the `memory` subsystem so the fast-path is real and reviewed, or (b) removing the direct-memory-write authorization from the prompt/SOUL.md entirely and making the design strictly proposal-only (write_file → ledger only). Neither is implemented yet.

---

## 7. Scheduler Catch-up Result

No job-level or profile-level override exists (E8); the global defaults from the first audit apply unchanged: `DEFAULT_MISFIRE_GRACE_MINUTES = 10` (`cron/scheduler_provider.py:20`) governs the misfire-grace sweep, and `cron/jobs.py`'s due-classification (on_time/late/catch_up, half-period clamped to [120s, 2h]) governs how a missed daily 04:30 run would be classified if the machine were asleep/off at that time. Since the job has not yet had its first scheduled occurrence (`last_run_at: null`), this is a code-level projection, not an observed catch-up event — flagged as such, not asserted as tested. Native catch-up exists and should be used as-is; no second scheduler or polling mechanism is warranted.

---

## 8. Required Fixes (only after evidence above — no changes made this session)

1. Replace the prose-only "bookmark" with an enforced cursor: either (a) extend `session_search` with a real `since_ts`/timestamp filter, or (b) — lower-effort and reusing existing infrastructure — add a deterministic pre-filter step (a small script step before the LLM turn, or a `no_agent: true` companion job like the existing `OpenRouter Catalog Watcher` pattern) that reads `dream_bookmark.json`, queries only sessions strictly newer than it, and hands the LLM an already-bounded list. Either closes `NO_INCREMENTAL_SESSION_CURSOR` and the `DREAM_REPROCESS_LOOP` risk without inventing a new subsystem.
2. Exclude the job's own past cron-sourced sessions from what it can see of itself, or accept (and explicitly instruct for) the fact that BROWSE-mode recall does not filter `source=cron` — right now the prompt assumes a filtering behavior the tool does not provide.
3. Resolve the memory-write defect one of two ways (§6) — do not leave the prompt authorizing a call path that is currently guaranteed to fail.
4. Given `write_approval` is off system-wide by default and no profile currently overrides it, if option (a) in §6 is chosen, `write_approval` for `memory` must be turned on for `rmk-knowledge` specifically, or the fast-path authorization should be removed — "mehrfach bestätigt" in the prompt is not, on its own, an enforced control.
5. Move the proposal ledger to a machine-readable format (JSON/JSONL) alongside the Markdown report, per the preferred V1 architecture's Stage C — the Markdown report alone cannot be deterministically deduplicated by a future promotion step.

None of these require a new scheduler, new polling loop, or new subsystem — all are extensions of tools/config that already exist.

---

## 9. V2 Recommendation (idle detection) — informational only, not implemented

No machine-idle detection exists anywhere in the codebase (confirmed again this session; no `GetLastInputInfo`/polling/idle code found). What does exist, and is a legitimate internal-signal candidate for V2: `agent/activity_tracking.py` and `agent/session_activity.py` maintain a real `last_activity_at`/`last_activity_ts` per session (already used by `agent/curator.py` for staleness checks and by the stall-monitor/watchdog). A V2 idle-triggered variant could compute "time since the most recent *non-cron* session's last_activity_at across this profile's session DB" as a native, no-polling proxy for "the human hasn't used this profile in N minutes," rather than reading Windows input state. This is a recommendation for later work, not something to build now — consistent with the instruction to keep the time-based 04:30 schedule as the working V1 baseline and to only add idle detection once a reliable source of truth exists.

---

## Acceptance Gates

| Gate | Status |
|---|---|
| `CONFIG_LOADED` | PROVEN |
| `CORRECT_PROFILE` | PROVEN — job's `workdir` is `E:\KI\Hermes\profiles\rmk-knowledge`, config/heartbeat read from that profile's own directory |
| `SCHEDULER_RECOGNIZES_JOB` | PROVEN — job present in `jobs.json`, `next_run_at` computed, ticker heartbeat fresh |
| `SESSION_SEARCH_AVAILABLE` | PROVEN — core tool, not on the cron denylist (`messaging`/`clarify`/`cronjob`) |
| `SESSION_SCOPE_UNDERSTOOD` | PROVEN (via code trace) — but scope includes cron/self sessions in BROWSE mode |
| `INCREMENTAL_CURSOR_WORKS` | **FALSIFIED** — `NO_INCREMENTAL_SESSION_CURSOR` |
| `REPORT_WRITTEN` | UNPROVEN this session (mechanism exists — `write_file` — but no live run occurred) |
| `RUN_RECORDED` | UNPROVEN — no run has occurred yet (`last_run_at: null`) |
| `SECOND_RUN_IDEMPOTENT` | **NOT_TESTABLE** — blocked by locked machine |
| `FAILED_RUN_DOES_NOT_SKIP_INPUT` | UNPROVEN — no failure semantics test performed (no code path found that advances the bookmark before report persistence, but this is not directly observed) |
| `NO_SELF_REFLECTION_LOOP` | **FALSIFIED** — cron/self sessions not excluded from recall |
| `NO_UNCONTROLLED_MEMORY_MUTATION` | PROVEN true only because the store is disabled (E6), not because a control exists |

Per the standing instruction, any unproven/falsified gate forecloses a PASS verdict. **Final verdict: FAIL.** The fix list in §8 is config/prompt-level only — no scope creep beyond what already exists in the repo.
