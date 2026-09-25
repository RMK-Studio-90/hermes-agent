# Context & capability budget — measurement and local runbook

Goal: **minimum necessary context and capability** per agent/surface, not
minimum possible context. Every token of fixed context (system prompt + tool
schemas) is paid on every API call; every tool a surface doesn't need is also
a capability it shouldn't have.

## 1. Measure

`scripts/context_budget_matrix.py` builds the real agents offline (dummy
credentials, no API call) and reports fixed context per surface: main agent,
CLI inside a repo (project context files), Telegram, Discord, cron, and a
leaf `delegate_task` subagent (implicit inheritance and a coding-only
`terminal`+`file` child).

```bash
# Against your real install (reads $HERMES_HOME / ~/.hermes):
python scripts/context_budget_matrix.py --repo ~/src/your-project
python scripts/context_budget_matrix.py --repo ~/src/your-project --json > before.json
# Per-profile:
HERMES_HOME=~/.hermes/profiles/<name> python scripts/context_budget_matrix.py --repo .
# Per-platform detail (skills by size, toolsets by size):
hermes prompt-size --platform telegram
```

Tokens are the chars/4 estimate `/context` uses. Pass `--model <name>` to size
the context-window-scaled caps against a specific model.

## 2. Repo-level changes and their measured effect

Fixture: `--fixture-home` (bundled skills synced, default SOUL.md, memory
~70% full), `--repo` = this repository, identical inputs before and after.

| Surface (fixed tokens/call) | Before | After | Δ |
|---|---:|---:|---:|
| Main agent (CLI, no project) | 13,369 | 13,390 | +21 |
| CLI in this repo, 256K-window model | 28,077 | 17,212 | **−10,865 (−39%)** |
| CLI in this repo, 1M-window model | 38,062 | 17,280 | **−20,782 (−55%)** |
| Telegram | 13,308 | 13,329 | +21 |
| Discord | 13,275 | 13,296 | +21 |
| Cron | 12,831 | 12,852 | +21 |
| Leaf subagent, inherited toolsets | 15,018 | 12,124 | **−2,894 (−19%)** |
| Leaf subagent, `terminal`+`file` | 8,171 | 6,605 | **−1,566 (−19%)** |
| Project context (256K / 1M model) | 14,369 / 24,286 | 3,483 | **−76% / −86%** |
| Project context in subagents | 4,782 (truncated) | 3,216 (complete) | −33% |
| Skills index | 1,908 | 1,908 | 0 |
| Memory | 684 | 684 | 0 |

The +21 is one added `delegate_task` guidance line (sequential work stays
with the parent). Before, `AGENTS.md` (96K chars) was head/tail **truncated**
in every session (cap 61K chars at 256K windows, 20K in subagents) — the
middle rules were silently lost. After, Hermes loads the complete 12K-char
`.hermes.md`.

Old tool output: with `compression.proactive_prune_tokens` now defaulting to
48000, a synthetic coding session with 40 tool calls (every third a 24K-char
dump) goes from 97,542 to 32,691 history tokens (−66%) on every subsequent
API call; at the old default of 0 nothing was reclaimed. Full outputs stay in
the session store.

What changed:

- **`.hermes.md`** — condensed working rules + an index into `AGENTS.md`
  sections. `.hermes.md` wins context-file priority, so Hermes sessions (and
  subagents working in the repo) load it instead of `AGENTS.md`; other tools
  keep reading `AGENTS.md`, which stays authoritative and upstream-mergeable.
- **Subdirectory hints** — when an ancestor `.hermes.md` won startup
  priority, the cwd's own `AGENTS.md` (e.g. `apps/desktop/AGENTS.md`) is now
  still discovered lazily on first touch instead of being dropped.
- **Write protection** — `.hermes.md`, `HERMES.md` and `AGENTS.override.md`
  joined the protected instruction files (always require human approval,
  even under yolo), with an invariant test that every context filename the
  loaders read is protected.
- **Subagent least privilege** — children that inherit toolsets implicitly
  no longer get `tts` and `session_search` (`delegation.inherit_exclude_toolsets`,
  `[]` restores full inheritance).
- **Proactive prune on by default** — `compression.proactive_prune_tokens:
  48000`, resolved identically at agent construction and TUI/desktop live
  reload (`DEFAULT_PROACTIVE_PRUNE_TOKENS`). Windows too small to reach it are
  handled by `threshold` alone. Each committed prune costs one prompt-cache
  break; the `proactive_prune_min_reclaim_tokens` gate plus the full-runway
  re-arm keep those episodic. Set `0` on a profile where you have measured
  that cache reads outweigh the reclaimed tokens.
- **Doc drift fixed** — cron memory is enabled on purpose (ef04d846e);
  delegation depth is flat by default (`max_spawn_depth: 1`); concurrency
  default is 10, not 3; cron has a 600s inactivity timeout, not a 3-minute
  hard interrupt.

## 3. Apply to your local system

These are per-install decisions (which bots need which tools), so they live
in each profile's `config.yaml`, not in repo defaults. Run the matrix before
and after each step.

### 3.0 Pick up the new defaults

A key written explicitly in `config.yaml` beats a changed default. For every
profile:

```bash
hermes config get compression.proactive_prune_tokens    # expect 48000
hermes config get delegation.inherit_exclude_toolsets   # expect [tts, session_search]
# if an old explicit value shadows the default:
hermes config set compression.proactive_prune_tokens 48000
hermes gateway restart                                   # running gateways re-read config
```

### 3.1 Least-privilege toolsets per surface

Start from `hermes tools` (curses UI) or edit `platform_toolsets`. Keep what
each surface actually uses; examples:

```yaml
platform_toolsets:
  cli: [terminal, file, web, browser, vision, skills, todo, memory,
        session_search, clarify, code_execution, delegation, cronjob]
  telegram: [web, vision, tts, skills, todo, memory, session_search,
             clarify, cronjob, terminal, file]     # drop browser/tts if unused
  discord: [discord, web, vision, skills, memory, session_search, clarify]
  cron: [terminal, file, web, skills, memory]       # per job: enabled_toolsets
```

- Keep platform-native toolsets (`discord`, `discord_admin`, `yuanbao`, …)
  on the platforms whose bots use them.
- `kanban` only on profiles that work boards (workers get it automatically
  inside a dispatched task).
- `computer_use`, `homeassistant`, `spotify`, `image_gen`, `browser`: enable
  only where used; each adds schema tokens to every call of that surface.
- Cron jobs that need less than the cron default: set the job's
  `enabled_toolsets` (via the `cronjob` tool) — per-job lists win.

### 3.2 Skills index

Disable skills a surface never uses: `skills.disabled: [...]` globally or
`skills.platform_disabled.<platform>: [...]`. `hermes prompt-size --platform
<p>` lists skills by size. Skill bodies already load lazily (`skill_view`).

### 3.3 Project context

Keep repo context files lean: core rules in `.hermes.md` / `AGENTS.md`,
details in on-demand files or skills. `context_file_max_chars` pins a fixed
cap if a local model's window is small (the dynamic cap is 6% of the window,
floor 20K chars).

### 3.4 Free-first model routing and reasoning effort

```yaml
delegation:
  provider: <free-or-local provider>    # routine subagent work
  model: <free-or-local model>
  reasoning_effort: low                 # raise per task class, not globally
agent:
  reasoning_effort: medium
  reasoning_overrides:                  # premium models: only where they earn it
    <premium-model>: high
auxiliary:                               # side tasks (titles, search, vision…)
  <task>:
    provider: <free-or-local provider>
    model: <free-or-local model>
```

Premium models for orchestration, hard debugging, architecture and final
review; routine parsing/discovery/simple edits on free/local. Context budgets
come from the model's window (compression `threshold` ratio,
`model_thresholds` per model, `threshold_tokens` cap) — don't hardcode a
product-specific token limit.

### 3.5 SOUL.md per profile

Identity, mission, scope, non-goals, reporting/handoff, approval boundaries.
Don't restate skills, project rules or tool docs there. Small SOULs are fine
as they are — don't trim identity for a few tokens.

### 3.6 Hard gates (a SOUL.md rule is not a gate)

Already enforced in code, keep them on:

- `approvals.mode: smart|manual` (not `off`), `approvals.cron_mode: deny`,
  `delegation.subagent_auto_approve: false`.
- `approvals.deny: ["git push --force*", "<your deploy command>*", ...]`
  blocks unconditionally, before yolo.
- `security.protected_instruction_files: true` (instruction-file writes).
- `memory.write_approval` / `skills.write_approval: true` to stage writes to
  shared memory/skills for review.
- Subagents can't write memory, send messages, schedule cron or ask the user.

## 4. Delegation gate

Delegate only independent, parallelizable or context-flooding work; do
sequential, dependent or trivial steps directly (encoded in the
`delegate_task` description). Depth stays flat (`max_spawn_depth: 1`) so
agent-to-agent loops cannot form; `delegation.max_iterations` caps each
child. Child summaries are self-reports — verify evidence (paths, IDs, test
output) before accepting or persisting them; persistence happens in the
parent (children have no `memory`).

## 5. Long tasks

Start independent tasks in a fresh session. For continuation, prefer the
compact persisted state (`todo`, kanban task state, compression summaries,
`session_search`) over dragging the full old conversation along.
