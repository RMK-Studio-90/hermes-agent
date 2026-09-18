# Audit: "RMK Dreaming idle catch-up" — End-to-End Verification

**Repository:** `E:\KI\Hermes\hermes-agent`
**Audited:** 2026-09-08, ~04:16 UTC / 06:16 Europe/Berlin
**Method:** Read-only inspection of source, per-profile cron stores, SQLite execution ledgers, Electron app state, and running-process ownership metadata on the linked Windows machine ("rmk"), via the device file bridge. Live Desktop UI / DevTools inspection was attempted but blocked (see §9).

---

## 1. EXECUTIVE VERDICT

## **FAIL**

The job **"RMK Dreaming idle catch-up" has no canonical definition in any cron store discoverable on this machine.** It does not exist in any of the 9 profiles under `E:\KI\Hermes\profiles`, nor in either of the two platform-fallback Hermes homes (`C:\Users\RMK\.hermes`, `C:\Users\RMK\AppData\Local\hermes`). No execution record, no output file, and no log line anywhere on disk references it. The Desktop UI is displaying a job that is not backed by any persisted backend state that I could locate.

This is a stronger finding than "misconfigured" — it is **PASS_WITH_UI_BUG territory at best, but I cannot rule out the job simply never having been persisted, which makes FAIL the honest call** given the checklist in §12 fails at the very first gate (canonical job exists). One residual path remains open and UNVERIFIED (see §9/§13): I could not open Desktop's DevTools to inspect the live `/api/cron/jobs` response, because the Windows session is currently screen-locked and I will not attempt to unlock it. That check would close the last gap.

Separately, and independent of whether this specific job exists: **"Dreaming" is not a concept implemented anywhere in this codebase**, and **"idle" does not mean user/machine idle detection** anywhere in the cron subsystem — it means (a) a per-execution inactivity watchdog that kills a hung job, and (b) an optimization that skips config/pool setup on ticks where nothing is due. There is no code path that measures desktop/user idle time and gates a "catch-up" run on it. Whatever this job's payload is (unknown — I couldn't find it), the name's semantics do not correspond to any implemented mechanism named that way. The only real "catch-up" mechanism in the codebase is the **generic misfire/catch-up sweep** (`cron/scheduler_provider.py`, `cron/jobs.py`) that applies to *every* cron job that falls behind schedule, regardless of "idle."

---

## 2. IMPLEMENTATION CALL GRAPH (generic cron subsystem — verified from source)

Since no job-specific "Dreaming" handler exists, this is the **generic** call graph every cron job in this codebase goes through. I could not extend it with a job-specific branch because there is no such job to trace.

```
cron/jobs.py:JOBS_FILE (per-profile jobs.json)
  → cron/jobs.py:2583 get_due_jobs()            # due-check: compares now vs next_run_at
  → cron/jobs.py:1089 compute_next_run()         # cron/interval schedule math (croniter-backed)
  → cron/scheduler.py:3726 tick(...)             # gateway ticker entry point, called every ~60s
      (comment at scheduler.py:3767-3772: "gateway ticker calls tick(verbose=False) every 60s")
  → cron/scheduler_provider.py:~239 misfire sweep # `cron.misfire_grace_minutes` (default 10 min,
                                                    DEFAULT_MISFIRE_GRACE_MINUTES = scheduler_provider.py:20)
      classifies a fire as on_time / late / catch_up  (cron/jobs.py:875-878)
  → cron/scheduler.py:2290 run_job(...)          # spawns the job's agent session or script
  → cron/scheduler.py:1789 _idle_seconds() / _POLL_INTERVAL=5.0 (scheduler.py:1747)
      → per-execution INACTIVITY watchdog (kills a hung run), NOT machine-idle detection
  → cron/executions.py + <profile>/cron/executions.db  # execution ledger (SQLite)
  → cron/scheduler_delivery.py:831 _resolve_delivery_target() / :494 cron_delivery_targets()
      → delivers output to the "local" target = "This desktop"
        (apps/desktop/src/i18n/en.ts:2146 `local: 'This desktop'`)
  → Desktop UI: apps/desktop/src/app/cron/index.tsx (list + detail panel)
      reads via apps/desktop/src/hermes.ts: getCronJobs() / getCronJob() / getCronJobRuns()
      — all profile/connection-scoped (apps/desktop/src/hermes-cron-scope.test.ts)
```

I could not produce the job-specific tail of this graph (dreaming/catch-up handler → execution → persistence for *this* job) because the job does not exist in any store I could find. See §5 for what "Dreaming" and "idle" actually resolve to in code.

---

## 3. NATURAL-RUN TIMELINE

**Not observable — UNVERIFIED.** I could not watch this job cross its `*/5 * * * *` boundary because it is not present in any jobs.json to begin with, so there is nothing for a scheduler to load or dispatch. I did, however, confirm that the *scheduler infrastructure itself* is alive and ticking in near-real-time for three other profiles at the moment of this audit (2026-09-08T04:16 UTC):

| Profile | `ticker_heartbeat` (epoch) | Age at check time | Verdict |
|---|---|---|---|
| rmk-intel | 1788840953.71 | ~17s old | ALIVE |
| rmk-knowledge | 1788840953.72 | ~17s old | ALIVE |
| rmk-research | 1788840953.73 | ~17s old | ALIVE (0 jobs configured) |
| `default` (AppData\Local\hermes fallback home) | 1788354800.37 | **~5.6 days old** | STALE / not ticking |

This proves the scheduler mechanism in this codebase genuinely works for jobs that do exist (see rmk-intel's `OpenRouter Catalog Watcher`, 23 completed runs, and rmk-knowledge's two jobs, both with real `last_run_at`/`last_status: ok` and real output files spanning Sep 2–8). It does **not** prove anything about "RMK Dreaming idle catch-up," which never appears in any of these stores.

Classification for this job specifically: closest fit is **JOB_NOT_LOADED** — except that's generous, since "not loaded" implies it exists somewhere and failed to load; I found no evidence it was ever persisted anywhere a loader could read it from.

---

## 4. DREAMING EVIDENCE

**None found.** Searched (ripgrep, case-insensitive, excluding `node_modules`/`venv*`) for `dreaming` across the entire repository. The only hit in ~text-wide search was an unrelated line in `optional-skills/health/neuroskill-bci/references/metrics.md:190` about REM sleep physiology — not Hermes code. `cron/blueprint_catalog.py` and `cron/suggestion_catalog.py` (the repo's built-in job templates/suggestions) contain no "dream" or "idle catch" terms either. There is no Dreaming module, handler, prompt template, or scheduled-job blueprint anywhere in this codebase.

I also searched every reachable execution ledger (`executions.db` `executions`/`cron_incidents` tables) and every cron output `.md` file across rmk-intel and rmk-knowledge (the only profiles with any job history) for the substring "dream" — zero matches in file names or file content.

**Conclusion:** there is no "Dreaming execution" to provide a provenance chain for. Whatever this job's actual prompt/payload was intended to be, it was never captured in any store I could reach.

---

## 5. RUN-HISTORY ANALYSIS ("No runs yet" vs. populated "Last")

I cannot trace this specific job's UI values to a backend row, because no backend row exists. But I can explain the **mechanism class** this bug belongs to, from source:

- `apps/desktop/src/hermes-cron-scope.test.ts` documents a **previously real, now-fixed** bug of exactly this shape (issue **#87882**): *"run history read a local state.db with zero cron rows and every job showed 'No runs yet'"* — caused by `getCronJobRuns()` not forwarding the active gateway **connection** alongside the **profile**, so the Run History call landed on the wrong backend/state.db while the job-list call (which shows "Last") happened to hit the right one, or vice versa.
- The fix (now in the test suite, presumably shipped) makes every cron helper — `getCronJobs`, `getCronJob`, `getCronJobRuns`, `createCronJob`, `updateCronJob`, `pauseCronJob`, `resumeCronJob`, `triggerCronJob`, `deleteCronJob` — carry both `profile` and `connectionId`.
- In this environment, `connections.json` shows only one registered connection (`"local"` / "This device") — there is no remote gateway connection active, so the *specific* #87882 mechanism (cross-connection routing) should not apply here.
- That leaves a **profile-scoping mismatch** as the more likely live candidate if this job existed at all: "Last" sourced from one profile's `jobs.json` (or a stale in-memory/react-query snapshot), "Run History" queried against a different profile's/backend's `executions.db` that legitimately has zero rows for that `job_id` — or the `job_id` behind this UI row was already deleted, so a Run History query for it correctly returns nothing while a stale list cache still shows the old "Last" value.

**Verdict for this specific job: UNVERIFIED / NOT_TESTABLE** — I cannot distinguish "real split-source bug re-occurring" from "orphaned/stale UI row for a job that was already deleted from every backend" without the live DevTools Network tab (blocked, see §9). Both explanations are consistent with every filesystem observation.

---

## 6. CATCH-UP ANALYSIS (generic mechanism, from code — Phase 5 questions)

Answered from implemented behavior, not intent:

1. **How is idle measured?** Two unrelated things called "idle" exist, neither is machine/user-idle: (a) `cron/scheduler.py:1789 _idle_seconds()` / `_POLL_INTERVAL=5.0` (line 1747) — polls how long a *running job's process* has produced no activity, to enforce an inactivity timeout (comment references issue #94285, "4118s-idle-on-a-600s-limit cron hang"); (b) "idle tick" (scheduler.py:3767-3772) — a scheduler tick where nothing is due, so config/pool setup is skipped for efficiency. **There is no OS/user-idle-time detection gating any job in this codebase.**
2. **What threshold?** For (a), `cron_inactivity_limit` (config-driven, passed as `limit_s`); no threshold exists for machine-idle because that concept isn't implemented.
3. **What resets idle?** For (a), any observed process activity. N/A for machine-idle.
4. **What qualifies as a missed run?** `cron/jobs.py:875-878` — classifies a fire as `on_time`, `late`, or `catch_up` based on how far past `next_run_at` the ticker's tick landed, using the misfire-grace window.
5. **Where is last successful run stored?** Per-job `last_run_at` / `last_status` fields in that profile's `cron/jobs.json` (verified structure in rmk-intel/rmk-knowledge's real jobs), plus a row in that profile's `cron/executions.db`.
6. **How many missed runs can be recovered?** One — `cron/jobs.py:860-861`: *"How late a job can be and still catch up rather than fast-forward: half the period, clamped to [120s, 2h]... so daily jobs catch up but frequent jobs fast-forward quickly."* Accumulated misses beyond the grace window are **fast-forwarded (skipped), not queued**.
7. **Once or repeatedly?** Once — the code computes a single next eligible fire and executes once; it does not replay every missed tick.
8. **Deduplication/idempotency?** Per-tick advisory locking on `jobs.json` (`fcntl`/`msvcrt` cross-process lock, `cron/jobs.py:17-25`), a `.tick.lock` file observed in every profile's `cron/` directory, and a `fire_claim`/`process_id`/`pid` column set in `executions.db`. This is real infrastructure, not a stub.
9. **Can the same missed interval run twice?** Not by design, given the lock + claim mechanism above — but I have **not** traced the crash-window scenario in Phase 10 for the actual completion→persist ordering (see §7); I did not want to force a crash test against a machine with four live production-ish backends.
10. **After Desktop/Hermes restart?** UNVERIFIED — would require a controlled restart, which risks the user's four currently-running profile backends (rmk-intel, rmk-knowledge, rmk-research, default) and their in-flight state. Not attempted.
11. **After PC sleep?** UNVERIFIED, same reasoning.
12. **After Hermes fully stopped?** UNVERIFIED, same reasoning.
13. **Nothing to "Dream" about?** N/A — no Dreaming handler exists to have a no-op branch.
14. **SUCCESS vs SKIPPED?** Generic jobs use `last_status: "ok"` / an `error` field in `executions.db`; I found no "skipped" status value in the two real jobs' history, so I cannot confirm a distinct SKIPPED state exists at all versus just "ok with no-op output" — **UNVERIFIED**.

---

## 7. IDEMPOTENCY ANALYSIS

From code (not tested live, to avoid disturbing the four running backends):

- **Locks:** `.jobs.lock` (jobs.json read-modify-write) and `.tick.lock` (single-ticker-at-a-time) exist as real files with real cross-process locking code (`fcntl`/`msvcrt`) in `cron/jobs.py`.
- **Claims:** `executions.db.executions` has `process_id`, `pid`, `claimed_at`, `handoff_pending`, `handoff_started_at` columns — this is a genuine claim/handoff design, not a stub schema.
- **Crash-between-completion-and-bookkeeping:** I did not find (nor did I have time within a read-only, non-disruptive audit to trace) the exact ordering of "mark execution row finished" vs. "advance `next_run_at` in jobs.json" to determine whether a crash between those two writes could cause a double-fire on restart. **This specific question (Phase 10's crash scenario) is UNVERIFIED** and would need a dedicated code read of the transaction boundary in `cron/scheduler.py` around `run_job` (line 2290) and wherever it calls back into `jobs.py` to persist `last_run_at`/`next_run_at`.

---

## 8. MODEL/USAGE EVIDENCE

Not applicable to the target job (it doesn't exist). For context, the real jobs I did find show real model routing recorded directly in `jobs.json`, e.g. rmk-knowledge's "RMK-KNOWLEDGE Daily Knowledge Audit": `model: "openrouter/nvidia/nemotron-3-super-120b-a12b:free"`, `provider: "custom:omniroute-free"`, `base_url: "http://127.0.0.1:20128/v1"` — i.e., routed through the user's local OmniRoute gateway rather than directly to OpenRouter. I did not verify token counts/cost against a Usage Dashboard telemetry store — out of scope without a concrete job to trace.

---

## 9. WHAT BLOCKED FULL CLOSURE

- The Windows session is currently **screen-locked** (confirmed indirectly: `computer_resolve_access` listed `lockapp.exe` among processes that would be hidden, and two screenshot attempts after switching to the correct monitor returned solid black). I did not attempt to unlock it — that would require the user's credentials, which is out of scope for this session.
- This blocked: opening Hermes Desktop's DevTools to inspect the live `/api/cron/jobs` and `/api/cron/jobs/<id>/runs` network responses (which would show, with certainty, which profile/connection this job claims to belong to, and its raw payload/fields); running `hermes cron status --profile <p>` for each of the 4 live backends (would give scheduler PID/heartbeat/loaded-job-count from the tool's own perspective rather than my filesystem inference); and any manual-trigger test (Phase 8) or restart/catch-up test (Phase 9), both of which explicitly require interacting with the running system.

---

## 10. DEFECTS FOUND

1. **[HIGH] Phantom scheduled job.** Desktop displays "RMK Dreaming idle catch-up" (schedule, Last, Next, delivery target) with no corresponding entry in any profile's `cron/jobs.json`, in either platform-fallback Hermes home, or in any execution ledger/output file on the machine. Either (a) the job was created and its persistence silently failed, (b) it was deleted from the backend but a stale row survives in the Desktop's in-memory/list state, or (c) it is a hardcoded/leftover UI artifact never wired to a real backend job. I could not distinguish between these without live DevTools access (§9).
2. **[MEDIUM] Misleading terminology.** "Idle" in the job's name implies machine/user-idle-gated execution. No such mechanism exists anywhere in `cron/`. If this job (once found) is meant to only run when the PC is idle, **that feature does not exist and would need to be built** — the only implemented "idle" concepts are an execution-hang watchdog and a scheduler tick-skip optimization.
3. **[LOW, informational] `default` profile fallback home is stale.** `C:\Users\RMK\AppData\Local\hermes\cron\ticker_heartbeat` is ~5.6 days old even though a backend process is running with `--profile default` (PID 19132 per `backend-ownership.json`). The `E:\KI\Hermes\profiles\default` directory that this backend *should* be using (per `HERMES_HOME` convention) has no `config.yaml`, `.env`, or `state.db` at all — it fails the code's own `_HERMES_HOME_MARKERS` check for "a real Hermes home." Worth checking that this backend is pointed at the intended profile home at all.

---

## 11. SEVERITY

Defect 1 is a **release-blocking correctness defect** for the claim "this job works end-to-end" — it doesn't, because it isn't there. Defect 2 is a **spec/naming defect** independent of whether the job exists. Defect 3 is **environmental hygiene**, not directly related to the target job, surfaced during the investigation.

---

## 12. PASS CHECKLIST

- [x] canonical job exists → **NO** (exhaustively searched, not found)
- [ ] scheduler loads it → N/A, nothing to load
- [ ] natural schedule fires → N/A
- [ ] correct handler executes → N/A
- [ ] Dreaming/catch-up semantics work → **"Dreaming" not implemented anywhere; "idle" ≠ machine-idle anywhere**
- [ ] meaningful result produced → N/A
- [ ] execution persisted → N/A
- [x] history queryable → yes, generically (real jobs' history is queryable and correct)
- [ ] restart/catch-up semantics sound → UNVERIFIED (not tested, to avoid disrupting live backends)
- [ ] duplicate execution prevented → real locking/claim infrastructure exists in code; not verified under a crash scenario
- [x] failures observable → yes, generically (`error`, `last_status`, `cron_incidents` table all exist and are populated for real jobs)

First gate fails → **FAIL**, not PARTIAL: nothing downstream of "does the job exist" can be evaluated for this specific job.

---

## 13. REPRODUCTION STEPS / HOW TO CLOSE THE REMAINING GAP

Once the machine is unlocked:

1. Open Hermes Desktop → Scheduled Jobs → open DevTools (usually `Ctrl+Shift+I` in this Electron build) → Network tab → filter `cron` → reload the panel. Capture the exact request URL (`?profile=...`) and the raw JSON for "RMK Dreaming idle catch-up," specifically its `id`.
2. With that `id`, from a terminal: `E:\KI\Hermes\hermes-agent\venv\Scripts\python.exe -m hermes_cli.main --profile <that-profile> cron status` — compare its `next_run_at`/`last_run_at`/loaded-job list against what I found on disk for that profile.
3. `grep -r "<job id>" E:\KI\Hermes\profiles\*\cron\jobs.json` (and the two fallback homes) to see if it exists somewhere I mis-scoped.
4. If genuinely orphaned in the UI only: restart Hermes Desktop (not the backends) and see if the row disappears — that would confirm it's a stale in-memory/list-cache artifact rather than a persistence failure.
5. Only after 1–4: consider a manual "Trigger now" (Phase 8) — but if step 3 finds no matching `id` anywhere, "Trigger now" will almost certainly 404 or error against a nonexistent job, which is itself useful confirming evidence.

---

## FACTS vs. ASSUMPTIONS vs. RECOMMENDATIONS

**Facts** (directly observed): every file path, job list, timestamp, and code excerpt cited above with a `FILE:LINE` or explicit file path.
**Assumptions**: that the Desktop UI's "Scheduled Jobs" panel for "RMK Dreaming idle catch-up" was showing the currently-active profile/connection at observation time — I could not confirm which profile was in view when you saw it, since the screen is now locked.
**Recommendation**: run steps 1–3 above once you're back at the machine; that will tell us definitively whether this is a persistence bug, a stale-UI bug, or a job that needs to be (re)created from scratch. I'd hold off building anything "idle-aware" until then, since that capability doesn't exist yet regardless of what's found.
