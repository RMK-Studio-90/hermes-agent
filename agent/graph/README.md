# Hermes Graph Engineering System

HGES is an explicit, bounded execution path using Hermes' existing provider and
credential resolution. `hermes graph run task.json` performs intake, routing,
isolated role calls, controller-owned execution, tests, review, targeted repair,
acceptance and finalization. Ordinary chat retains its existing fast path.

## Run

```json
{
  "goal": "Explain the difference between an API and a user interface",
  "acceptance_criteria": ["Include one concrete example"],
  "task_type": "text",
  "complexity": 2,
  "risk": "low",
  "mode": "auto"
}
```

```sh
hermes graph run task.json
hermes graph inspect GRAPH_ID
hermes graph metrics
```

`--db PATH` selects a separate store; otherwise it lives below the active profile's
`get_hermes_home()/graph/`. IDs are new for every run; `--task-id` never overwrites a
previous run. Non-successful executions return exit 1.

For coding set `task_type: software_change`, an existing absolute `workspace`, exact
relative `files`, and `test_commands` as argv arrays. For example:

```json
{
  "goal": "Correct calculator.py",
  "acceptance_criteria": ["The approved calculator regression tests pass"],
  "task_type": "software_change",
  "complexity": 6,
  "risk": "medium",
  "workspace": "/absolute/path/to/project",
  "files": ["calculator.py"],
  "test_commands": [["python", "-m", "unittest", "tests.test_calculator"]]
}
```

Choose the project's own test wrapper (`scripts/run_tests.sh` for Hermes). Commands
are user-owned capabilities, never model-generated shell strings. They are trusted
programs with their own filesystem/network effects; HGES is not an OS sandbox for
arbitrary test scripts. The executor supplies full replacement text only for scoped
files. The controller rejects traversal, symlinks/junctions, oversized content and
concurrent edits. Models cannot delete files or issue arbitrary shell commands.
Multi-file writes are not a filesystem transaction: interrupted writes require
reconciliation. Large contexts fail visibly rather than silently dropping source.

## Modes

| Mode | Execution |
|---|---|
| DIRECT | One tool-free answer for simple low-risk text |
| REFLECT | Executor → validator → independent critic → gate |
| PLAN | Planner → executor → validator → reviewer → gate |
| GRAPH | Architect → tech lead → executor → validator → reviewer → gate |

Auto-routing uses complexity, risk and scope. `researchers: true` adds a read-only
researcher for explicitly supplied source files; it does not claim web browsing.
FAIL triggers a root-cause/repair-scope contract, then execution and review again.
Finalization returns the accepted answer without another model call.

Each role gets a separate JSON context, not the parent chat. Reviewer input is the
specification, original/current artifacts and controller test evidence, excluding
planning/repair conversations. Roles expose no shell, memory, skill, delegation or
browser tools. File writes and tests belong to the controller; background memory
review is not started. This reuses provider resolution without introducing another
general-purpose tool loop.

## Configuration and limits

`config.yaml` → `graph` holds `node_tokens`, `output_tokens`, `node_timeout`, and
`budget`: `max_nodes`, `max_tokens`, `max_model_calls`, `max_runtime_seconds`,
`max_repair_cycles`. Task values override defaults. Configure existing Hermes routes
under `auxiliary.graph_direct`, `graph_architect`, `graph_tech_lead`, `graph_planner`,
`graph_executor`, `graph_researcher`, `graph_reviewer`, and `graph_repair`. Reviewer
context is independent even when its model is shared; it can be pinned separately.
Request files do not contain credentials.

Node allowances are reserved before dispatch and not refunded. Input uses UTF-8
byte bounds plus framing/output reserves. Provider usage is checked before effects;
missing usage, malformed/truncated JSON and provider errors fail without blind
retries. `max_model_calls` counts role invocations. OpenAI SDK transport retries are
disabled; other native adapters can retain internal network retries. This is not an
exact invoice cap for opaque provider-side processing.

Every shipped model/test adapter runs in a bounded subprocess. Windows Job Objects
close the process tree; POSIX uses a dedicated process group. Output capture is
bounded. Lifetime survives restarts, with monotonic deadlines inside an invocation.
Terminating local work cannot undo a request already received by a remote provider.
Custom GraphNode implementations must cooperate with async cancellation or use the
provided process adapter.

## Acceptance, Kanban and Desktop

Coding success requires actual tests to exit successfully, unchanged scoped content
between tests/review/gate/finalizer, every criterion approved, reviewer PASS and no
blocking findings. New content or a new review invalidates the old gate. Semantic
criteria remain model-reviewed; this is not a mathematical correctness proof.

Set `kanban_task_id` and optionally `board` in the contract to claim a ready card.
HGES uses its existing task/run identity and a board-local graph store. Other
surfaces cannot complete an enrolled card before accepted graph success. Legacy
cards retain their behavior. Success completes the owned run; failure blocks it.
Detailed node stages are separate from existing Kanban columns. There is no second
dispatcher. Cross-database operations fail closed and may require reconciliation.

Task-detail REST includes `graph`; the Desktop task drawer renders status, attempts,
models, tokens, duration and failure reasons through normal polling. UI changes
require the updated renderer build. Inspect board runs using
`hermes graph inspect GRAPH_ID --board BOARD`.

## Recovery and learning

```sh
hermes graph resume GRAPH_ID
hermes graph recover GRAPH_ID --expected-version VERSION
```

Resume continues only clean checkpoints. Open nodes are never automatically replayed
or stolen. Confirm the old owner stopped before recovery; recovery closes the run
as `needs_attention` and reconciles its owned Kanban card. Inspect external effects
and create a new scoped task to continue uncertain work.

Successful repaired tasks can create learning candidates in a separate table. No
workflow artifact is automatically written to Memory, OpenViking, Obsidian or skills.
A matching root cause needs two successful source tasks and explicit approval:

```sh
hermes graph candidate GRAPH_ID
hermes graph promote GRAPH_ID /chosen/knowledge/note.md --approve
```

Promotion creates a new Markdown knowledge artifact with source references and
records approval. Existing files are not overwritten. Memory ingestion and skill
registration remain explicit actions beyond this gate.

Metrics report success, first-pass success, review failure, repair, escalation,
tokens and latency. Cost per success is available only when all contributing model
runs include reported costs. Missing cost/regression measurements are null, not zero.

## Validation

```sh
scripts/run_tests.sh tests/agent/test_graph_execution.py tests/agent/test_graph_workflow.py tests/agent/test_graph_kanban.py tests/agent/test_graph_process_windows.py
python -m agent.graph demo --scenario pass
```

Diagnostics also include `repair`, `fail`, `timeout`, `cancel`. Tests use real
temporary files/SQLite, Kanban/REST, process-tree termination and real Hermes provider
resolution against a local HTTP endpoint without paid external model requests.

## Guided /graph entry

Desktop/TUI and CLI route `/graph <idea>` into a normal, leased conversation turn.
The graph intake intercepts pending draft replies before the tool loop. Messaging
uses the same profile/session-scoped intake. It asks requirements first, then uses
the tool-free `auxiliary.graph_intake` role to resolve remaining questions or propose
a validated GraphRequest. No execution occurs while collecting or previewing.

`/graph start <current token>` approves the displayed goal, criteria, workspace,
exact files, test argv and budget. Edits revoke the token; CAS prevents duplicate
execution. A new Kanban card is parked until HGES atomically owns its run, avoiding
a race with the generic dispatcher. The normal chat streams node progress and the
final answer. `/graph status` reads the durable state; `/graph cancel` ends an idle
draft. The Desktop Stop control cancels an active turn and its child processes.

Intake has a separate maximum of 12 model calls. Its provider child explicitly
inherits the active profile home. Drafts survive restarts; interrupted graph work
is never implicitly replayed. The existing inspect/recover commands reconcile
stopped checkpoints. No bot deployment or credential provisioning is implied.

Run `scripts/run_tests.sh tests/agent/test_graph_intake.py` for the dialogue,
public agent facade, Desktop dispatch, real local provider, cancellation, and
approved file-writing integration tests.
