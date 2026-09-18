# PHASE 0 — RMK KNOWLEDGE INGESTION SPECIFICATION V4

**Status:** AUTHORITATIVE CONTRACT  
**Version:** 4.0  
**Date:** 2026-09-15  
**Owner:** rmk-dev / rmk-knowledge / rmk-review  
**Location:** E:\KI\Hermes\profiles\rmk-knowledge\reports\PHASE_0_FINAL_OBJECTIVE_V4.md

---

## 1. GOAL / SCOPE

Build a deterministic, evidence-backed ingestion pipeline that:

- Discovers Markdown/MDX files under the canonical Obsidian vault (`E:\KI\Obsidian\RMK Knowledge`)
- Extracts front-matter, body, links, tags, and embeddings
- Validates every record against the **Schema v33** contract
- Persists to the **Processing Ledger** (SQLite, WAL mode) with full provenance
- **The Processing Ledger is a TABLE (`processing_ledger`) inside the canonical profile's `state.db`**
- Projects validated knowledge to the **RMK Knowledge** lifecycle store (canonical)
- Projects to **OpenViking** retrieval index (separate boundary, read-only for ingestion)
- Emits structured telemetry for every decision point
- Guarantees idempotency: re-running on the same corpus produces zero new ledger rows
- Enforces secret boundary: no secrets, tokens, or credentials ever enter the ledger or projections
- Maximum 3 attempts per file (transient failures only); permanent failures are recorded and not retried

**Out of scope:**
- UI/CLI for manual ingestion triggers
- Real-time file-watcher (Phase 1)
- LLM-based enrichment, summarization, or classification
- Cross-vault synchronization
- OpenViking write path (ingestion is read-only for OpenViking)

---

## 2. SECRET BOUNDARY & PATTERNS

### 2.1 Hard Secret Classes (never allowed in ledger or projections)

| Class | Pattern (regex) | Action |
|-------|----------------|--------|
| API Key (generic) | `[a-zA-Z0-9_-]{20,}` in `key=`, `api_key=`, `token=`, `Authorization:*** contexts | REDACT → `[REDACTED]` |
| Bearer Token | `Bearer\s+[A-Za-z0-9._-]{10,}` | REDACT → `[REDACTED]` |
| OpenRouter Key | `sk-or-[a-zA-Z0-9]{20,}` | REDACT → `[REDACTED]` |
| OpenAI Key | `sk-[a-zA-Z0-9]{20,}` | REDACT → `[REDACTED]` |
| Anthropic Key | `sk-ant-[a-zA-Z0-9_-]{20,}` | REDACT → `[REDACTED]` |
| GitHub Token | `gh[pousr]_[A-Za-z0-9]{36}` | REDACT → `[REDACTED]` |
| NVIDIA Key | `nvapi-[a-zA-Z0-9_-]{20,}` | REDACT → `[REDACTED]` |
| Generic Secret | `password\s*[:=]\s*[^\s]{8,}`, `secret\s*[:=]\s*[^\s]{8,}` | REDACT → `[REDACTED]` |
| Private Key | `-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----` | BLOCK file |
| AWS Key | `AKIA[0-9A-Z]{16}` | REDACT → `[REDACTED]` |
| GCP Key | `ya29\.[A-Za-z0-9_-]+` | REDACT → `[REDACTED]` |
| Discord Token | `[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27}` | REDACT → `[REDACTED]` |
| Slack Token | `xox[baprs]-[0-9]{12}-[0-9]{12}-[0-9]{12}-[a-f0-9]{32}` | REDACT → `[REDACTED]` |

### 2.2 Scanner Failure Semantics

- **BLOCK**: File contains a private key block or unredactable credential structure → `ledger.status = BLOCKED_SECRET`, no body/embedding extracted
- **REDACT**: Pattern match found → replace match with `[REDACTED]` in extracted body; original file untouched
- **PASS**: No patterns matched → normal processing
- **ERROR**: Scanner exception (encoding, permission, path traversal) → `ledger.status = SCANNER_ERROR`, record exception message

### 2.3 Secret Redaction Contract

- Redaction applies **only to extracted text** written to ledger/projections
- Source files are **never modified**
- Redaction log entry: `{file_id, pattern_class, match_count, positions[]}`
- Telemetry event: `secret_redaction` with `{file_id, class, count}`

---

## 3. PROCESSING LEDGER SCHEMA (SQLite, WAL mode)

**Location:** Table `processing_ledger` inside the canonical profile's `state.db`  
**Canonical profile:** `rmk-knowledge` → `E:\KI\Hermes\profiles\rmk-knowledge\state.db`

```sql
CREATE TABLE IF NOT EXISTS processing_ledger (
    -- Identity
    file_id          TEXT PRIMARY KEY,           -- SHA256(relative_path + mtime_ns + size)
    source_path      TEXT NOT NULL,              -- vault-relative path (POSIX)
    source_abs_path  TEXT NOT NULL,              -- absolute path at ingestion time
    canonical_dir_hash TEXT NOT NULL,            -- SHA256 of canonical directory structure

    -- File metadata
    size_bytes       INTEGER NOT NULL,
    mtime_ns         INTEGER NOT NULL,           -- source file mtime (nanoseconds)
    encoding         TEXT NOT NULL,              -- e.g. "utf-8"
    front_matter     TEXT,                       -- JSON string of parsed front-matter
    body_sha256      TEXT NOT NULL,              -- SHA256 of extracted body (post-redaction)

    -- Schema validation
    schema_version   INTEGER NOT NULL DEFAULT 33,
    schema_valid     INTEGER NOT NULL DEFAULT 0, -- 1=valid, 0=invalid
    schema_errors    TEXT,                       -- JSON array of validation errors

    -- Content
    title            TEXT,
    tags             TEXT,                       -- JSON array
    links            TEXT,                       -- JSON array of [[wikilinks]] + [markdown](url)
    embeddings       TEXT,                       -- JSON array of {model, vector_sha256, dim}
    word_count       INTEGER,
    char_count       INTEGER,

    -- Processing state
    status           TEXT NOT NULL,              -- DISCOVERED, SCANNING, SCANNED, VALIDATING, VALIDATED, PROJECTING, PROJECTED, BLOCKED_SECRET, SCANNER_ERROR, SCHEMA_INVALID, PROJECTION_FAILED, COMPLETE
    attempt          INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    last_error       TEXT,
    last_attempt_at  INTEGER,                    -- unix nanoseconds
    completed_at     INTEGER,                    -- unix nanoseconds

    -- Provenance
    ingested_at      INTEGER NOT NULL,           -- unix nanoseconds
    ingested_by      TEXT NOT NULL,              -- "rmk-knowledge-ingest/v4"
    run_id           TEXT NOT NULL,              -- UUIDv4 for this ingestion run
    source_identity  TEXT NOT NULL,              -- "obsidian:vault:RMK Knowledge"

    -- Idempotency
    previous_file_id TEXT,                       -- if this file_id supersedes an earlier one
    superseded       INTEGER NOT NULL DEFAULT 0  -- 1 if a newer ingestion superseded this row
);

CREATE INDEX IF NOT EXISTS idx_ledger_state ON processing_ledger(status);
CREATE INDEX IF NOT EXISTS idx_ledger_adapter ON processing_ledger(source_identity);
CREATE INDEX IF NOT EXISTS idx_ledger_knowledge ON processing_ledger(canonical_dir_hash);
CREATE INDEX IF NOT EXISTS idx_ledger_retry ON processing_ledger(attempt, status);
```

---

## 4. SCHEMA 32 → 33 MIGRATION

### 4.1 Schema 32 (Legacy)
- Fields: `file_id, source_path, size_bytes, mtime, front_matter, body_sha256, schema_version, schema_valid, title, tags, links, status, attempt, last_error, ingested_at, ingested_by`
- No canonical directory hash, no provenance chain, no run_id, no secret redaction log

### 4.2 Schema 33 (Current)
- **Added fields:** `source_abs_path, canonical_dir_hash, encoding, embeddings, word_count, char_count, schema_errors, max_attempts, last_attempt_at, completed_at, run_id, source_identity, previous_file_id, superseded`
- **Changed:** `mtime` → `mtime_ns` (nanosecond precision)
- **Removed:** implicit single-attempt assumption; explicit `attempt`/`max_attempts`
- **Migration:** On Hermes `state.db` open, `SCHEMA_VERSION` checked; if < 33, run migration SQL (add columns, backfill `canonical_dir_hash` from vault root hash, backfill `run_id` from latest ingestion run, set `max_attempts=3`)

### 4.3 Migration Procedure
```sql
-- Run once per state.db lifetime when Hermes SCHEMA_VERSION advances to 33
-- Migration is executed by Hermes SessionDB schema init (hermes_state_schema.py)
ALTER TABLE processing_ledger ADD COLUMN source_abs_path TEXT;
ALTER TABLE processing_ledger ADD COLUMN canonical_dir_hash TEXT;
ALTER TABLE processing_ledger ADD COLUMN encoding TEXT DEFAULT 'utf-8';
ALTER TABLE processing_ledger ADD COLUMN embeddings TEXT;
ALTER TABLE processing_ledger ADD COLUMN word_count INTEGER;
ALTER TABLE processing_ledger ADD COLUMN char_count INTEGER;
ALTER TABLE processing_ledger ADD COLUMN schema_errors TEXT;
ALTER TABLE processing_ledger ADD COLUMN max_attempts INTEGER DEFAULT 3;
ALTER TABLE processing_ledger ADD COLUMN last_attempt_at INTEGER;
ALTER TABLE processing_ledger ADD COLUMN completed_at INTEGER;
ALTER TABLE processing_ledger ADD COLUMN run_id TEXT;
ALTER TABLE processing_ledger ADD COLUMN source_identity TEXT DEFAULT 'obsidian:vault:RMK Knowledge';
ALTER TABLE processing_ledger ADD COLUMN previous_file_id TEXT;
ALTER TABLE processing_ledger ADD COLUMN superseded INTEGER DEFAULT 0;
-- mtime → mtime_ns: create new column, backfill (mtime * 1_000_000), drop old, rename
-- Backfill canonical_dir_hash from vault root
-- Backfill run_id from most recent run_id in table or new UUIDv4
-- Create indexes
CREATE INDEX IF NOT EXISTS idx_ledger_state ON processing_ledger(status);
CREATE INDEX IF NOT EXISTS idx_ledger_adapter ON processing_ledger(source_identity);
CREATE INDEX IF NOT EXISTS idx_ledger_knowledge ON processing_ledger(canonical_dir_hash);
CREATE INDEX IF NOT EXISTS idx_ledger_retry ON processing_ledger(attempt, status);
```

---

## 5. STATE-TRANSITION RULES

```
DISCOVERED
  └─► SCANNING (attempt += 1)
        ├─► SCANNED (secret scan PASS/REDACT)
        │     └─► VALIDATING
        │           ├─► VALIDATED (schema_valid=1)
        │           │     └─► PROJECTING
        │           │           ├─► PROJECTED (RMK Knowledge + OpenViking)
        │           │           │     └─► COMPLETE
        │           │           └─► PROJECTION_FAILED (attempt < max_attempts → SCANNING)
        │           │                 (attempt >= max_attempts → terminal)
        │           └─► SCHEMA_INVALID (terminal, no retry)
        └─► BLOCKED_SECRET (terminal, no retry)
        └─► SCANNER_ERROR (attempt < max_attempts → SCANNING)
              (attempt >= max_attempts → terminal)
```

### 5.1 Terminal States (no further transitions)
- `BLOCKED_SECRET`
- `SCHEMA_INVALID`
- `SCANNER_ERROR` (after max_attempts)
- `PROJECTION_FAILED` (after max_attempts)
- `COMPLETE`

### 5.2 Retryable States
- `SCANNING` (on `SCANNER_ERROR`)
- `PROJECTING` (on `PROJECTION_FAILED`)

### 5.3 Maximum 3 Attempts Total
- Each file gets at most 3 transitions into `SCANNING` or `PROJECTING`
- Attempt counter persists across process restarts
- After 3 failures, state becomes terminal with `last_error` populated

---

## 6. DUPLICATE / IDEMPOTENCY SEMANTICS

### 6.1 File Identity
```
file_id = SHA256(relative_path + "|" + mtime_ns + "|" + size_bytes)
```
- Two files with same path, mtime, and size → same `file_id` → **idempotent skip**
- Modified file (mtime or size changed) → new `file_id` → **re-process**
- `previous_file_id` links old → new for audit trail
- `superseded=1` set on old row when new `file_id` ingested

### 6.2 Corpus Idempotency
- Re-running ingestion on unchanged corpus:
  - Discovers same `file_id`s
  - Finds existing `COMPLETE` rows
  - **Zero new ledger rows inserted**
  - Telemetry: `idempotent_skip={count}`

### 6.3 Run Identity
- Each ingestion run gets a UUIDv4 `run_id`
- All rows in a run share the same `run_id`
- Enables run-level audit, rollback, and telemetry correlation

---

## 7. SOURCE IDENTITY & CANONICAL DIRECTORY HASHING

### 7.1 Source Identity
```
source_identity = "obsidian:vault:RMK Knowledge"
```
- Fixed string for this ingestion source
- Future sources: `obsidian:vault:<name>`, `git:repo:<url>`, `api:confluence:<space>`

### 7.2 Canonical Directory Hash
- Computed once per ingestion run
- Walk vault root (`E:\KI\Obsidian\RMK Knowledge`) in deterministic order (sorted POSIX paths)
- For each file: `SHA256(relative_path + "|" + mtime_ns + "|" + size_bytes)`
- `canonical_dir_hash = SHA256(concat(sorted(file_hashes)))`
- Stored in every ledger row for that run
- Enables detection of vault structural changes across runs
- Telemetry: `canonical_dir_hash` per run

---

## 8. TELEMETRY CONTRACT

Every state transition emits a structured event to `E:\KI\Hermes\profiles\rmk-knowledge\telemetry.jsonl` (JSON Lines):

```json
{
  "ts": "2026-09-15T12:00:00.123456Z",
  "run_id": "uuidv4",
  "file_id": "sha256...",
  "source_path": "folder/note.md",
  "event": "state_transition",
  "from": "SCANNING",
  "to": "SCANNED",
  "details": {"secret_redactions": 2, "patterns": ["OpenRouter Key", "Generic Secret"]}
}
```

**Event Types:**
- `run_start`, `run_complete`, `run_failed`
- `state_transition` (with `from`, `to`, `details`)
- `secret_redaction` (with `class`, `count`, `positions[]`)
- `schema_validation` (with `valid`, `errors[]`)
- `projection` (with `target`, `status`, `details`)
- `idempotent_skip` (with `count`)
- `canonical_dir_hash` (with `hash`, `file_count`)

---

## 9. RMK KNOWLEDGE LIFECYCLE INTEGRATION

### 9.1 Projection Target (Canonical)
- **Path:** `E:\KI\Obsidian\RMK Knowledge\_RMK_GENERATED\Hermes\Knowledge\`
- **Format:** One `.md` per validated file, front-matter enriched with:
  - `rmk_ingest_file_id`
  - `rmk_ingest_run_id`
  - `rmk_ingest_at`
  - `rmk_schema_version: 33`
  - `rmk_canonical_dir_hash`
- **Naming:** Preserve relative path structure; `.md` extension
- **Conflict:** Overwrite if `body_sha256` differs; skip if identical

### 9.2 Projection Rules
- Only `status = COMPLETE` rows projected
- Only `schema_valid = 1` rows projected
- Secret-redacted body used (original vault file unchanged)
- Front-matter merged: ingest metadata takes precedence on `rmk_*` keys
- Telemetry: `projection` event per file with `target="rmk_knowledge"`

---

## 10. OPENViking PROJECTION — BOUNDARY RULE

### 10.1 Read-Only for Ingestion
- Ingestion pipeline **writes to OpenViking retrieval index** (e.g., `E:\KI\OpenViking\index\`)
- Ingestion pipeline **NEVER writes to OpenViking canonical store**
- OpenViking canonical store is managed exclusively by `rmk-knowledge` agent
- Ingestion provides: `file_id`, `body_sha256`, `embeddings`, `metadata` (title, tags, links)
- OpenViking decides chunking, vector indexing, and retrieval schema

### 10.2 Projection Payload
```json
{
  "file_id": "...",
  "source_identity": "obsidian:vault:RMK Knowledge",
  "title": "...",
  "body_sha256": "...",
  "tags": [...],
  "links": [...],
  "embeddings": [{"model": "nomic-embed-text", "vector_sha256": "...", "dim": 768}],
  "ingested_at": 1234567890123456789,
  "run_id": "..."
}
```

### 10.3 Boundary Enforcement
- Ingestion code **must not** import or call OpenViking write APIs
- Projection via file drop / message queue / HTTP POST to OpenViking ingestion endpoint only
- Telemetry: `projection` event with `target="openviking"`

---

## 11. UUIDv4 DECISION

- **All run IDs, file IDs (where not content-addressed), and correlation IDs use UUIDv4 (RFC 4122)**
- `run_id`: UUIDv4 per ingestion run
- `file_id`: Content-addressed (SHA256), NOT UUID — ensures idempotency
- Correlation IDs in telemetry: UUIDv4
- Rationale: Content-addressing for file identity; UUIDv4 for run/correlation where no natural key exists

---

## 12. MIGRATION & ROLLBACK PROCEDURE

### 12.1 Forward Migration (Schema < 33 → 33)
- Automatic on Hermes `state.db` open when `SCHEMA_VERSION` advances to 33
- Executed by `hermes_state_schema.py` data migration chain
- Idempotent: safe to re-run
- Verification: `SCHEMA_VERSION = 33` + column existence check + indexes exist

### 12.2 Rollback (Schema 33 → 32)
**Not supported.** Schema 33 is a strict superset; rollback would lose provenance data.
- If rollback required: restore `state.db` from pre-migration backup
- Backup taken automatically before migration: `state.db.pre_v33_<timestamp>`

### 12.3 Corpus Rollback
- Delete ledger rows for a specific `run_id`
- Set `superseded=0` on `previous_file_id` rows
- Re-run ingestion

---

## 13. COMPLETE TEST MATRIX

| Test ID | Description | Expected Result |
|---------|-------------|-----------------|
| T01 | Fresh ledger, fresh corpus → all files COMPLETE | PASS |
| T02 | Re-run on unchanged corpus → zero new rows | PASS (idempotent_skip = file count) |
| T03 | Modify one file → only that file re-processed | PASS (new file_id, previous linked) |
| T04 | File with OpenRouter key → REDACT, not BLOCK | PASS (status=COMPLETE, redaction log) |
| T05 | File with private key block → BLOCKED_SECRET | PASS (terminal, no projection) |
| T06 | File with invalid front-matter YAML → SCHEMA_INVALID | PASS (terminal, errors recorded) |
| T07 | File missing required schema field → SCHEMA_INVALID | PASS |
| T08 | Schema 32 state.db opened → auto-migrates to 33 | PASS (SCHEMA_VERSION=33, columns exist, indexes exist) |
| T09 | Transient scanner error (permission) → retry up to 3x | PASS (attempt=3, then terminal) |
| T10 | Transient projection error → retry up to 3x | PASS |
| T11 | Permanent projection error (disk full) → terminal after 3 | PASS |
| T12 | canonical_dir_hash computed and stable per run | PASS |
| T13 | run_id UUIDv4, unique per run | PASS |
| T14 | RMK Knowledge projection writes enriched .md files | PASS |
| T15 | OpenViking projection payload emitted, no write API called | PASS |
| T16 | allow_paid equivalent: no paid model/API used in pipeline | PASS |
| T17 | Telemetry JSONL contains all event types | PASS |
| T18 | Concurrent ingestion runs (different run_id) → no conflict | PASS |
| T19 | Vault symlink traversal → blocked (path stays in vault) | PASS |
| T20 | Large file (>100MB) → streamed, not memory-exhausted | PASS |

---

## 14. ACCEPTANCE CRITERIA

**PASS if ALL of the following are TRUE:**

1. `PHASE_0_FINAL_OBJECTIVE_V4.md` persisted at exact path with verified SHA256
2. Processing ledger table created in `rmk-knowledge` profile's `state.db` with Schema 33
3. Canonical directory hash computed and stored per run
4. Fresh ingestion of test corpus (50+ files, mixed secrets/valid/invalid) produces:
   - 100% files in terminal state (`COMPLETE` or `BLOCKED_SECRET` or `SCHEMA_INVALID`)
   - Zero `SCANNER_ERROR` or `PROJECTION_FAILED` terminal states (retries exhausted)
   - Idempotent re-run produces zero new ledger rows
5. Secret redaction:
   - No secret pattern appears in any ledger `body_sha256` body
   - Redaction log present for each redacted file
   - Private key files → `BLOCKED_SECRET`
6. Schema validation:
   - Valid files → `schema_valid=1`, projected
   - Invalid files → `schema_valid=0`, `SCHEMA_INVALID`, not projected
7. Projections:
   - RMK Knowledge: enriched `.md` files under `_RMK_GENERATED\Hermes\Knowledge\`
   - OpenViking: payload emitted, no write API called
8. Telemetry:
   - `run_start`, `run_complete` events present
   - Every file has state-transition chain
   - `canonical_dir_hash` event per run
9. Migration:
   - Schema 32 `state.db` auto-migrates to 33 on Hermes open
   - Backup created before migration
10. UUIDv4 used for `run_id` and correlation IDs
11. Maximum 3 attempts enforced (no file exceeds attempt=3)
12. `allow_paid: false` equivalent — no paid API credentials used

**FAIL if ANY criterion is not met.**

---

## 15. OUT OF SCOPE (EXPLICIT)

- Real-time file system watcher (Phase 1)
- LLM-based content enrichment (Phase 2)
- Cross-vault synchronization (Phase 3)
- OpenViking canonical store write path
- UI/CLI for manual ingestion control
- Distributed ingestion workers
- Incremental embedding model updates
- Natural language query interface

---

## 16. FINAL BUILD READINESS DECISION

**BUILD_READINESS = PASS** only when:
- This specification is persisted and independently verified (§17)
- All acceptance criteria (§14) demonstrated on representative corpus
- SafeState backup of `E:\KI\Hermes\hermes-agent` created
- `rmk-review` independent verification complete
- No open `BLOCKED` items in this contract

---

## 17. SPEC FILE PERSISTENCE VERIFICATION

**Required commands (to be executed by rmk-dev, then independently by rmk-review):**

```powershell
$Repo = "E:\KI\Hermes\hermes-agent"
$Spec = Join-Path $Repo "docs\PHASE_0_FINAL_OBJECTIVE_V4.md"

Test-Path $Spec
Get-Item $Spec | Select-Object FullName,Length,LastWriteTime
Get-FileHash $Spec -Algorithm SHA256
git -C $Repo status --short -- $Spec
Get-Content $Spec -TotalCount 15
Get-Content $Spec | Select-Object -Last 15
```

**Expected output:**
- `Test-Path = True`
- `FullName = E:\KI\Hermes\hermes-agent\docs\PHASE_0_FINAL_OBJECTIVE_V4.md`
- `Length > 10000` (bytes)
- `SHA256 = <calculated hash>`
- `git status` shows the file (staged or untracked)
- First 15 lines contain header + goal/scope
- Last 15 lines contain verification section

---


## 10. Enforced Production Rules (User-Mandated)

### OVERSIZED_SOURCE
- Immediately permanent — any source classified as OVERSIZED_SOURCE triggers permanent scanner failure, no retry possible
- This is a hard constraint; sources flagged as OVERSIZED_SOURCE must never be re-attempted in Phase 0

### QUALITY_GATE
- non-retryable — the Quality Gate is a one-pass check; if failed, the entire Phase 0 ingestion cycle must be rolled back and remediated before any further attempt
- No retries or soft-fallbacks are permitted under the Quality Gate constraint

### Zero Raw-Secret Leakage Requirement
- Zero raw-secret leakage: no secret values, API keys, tokens, passwords, or credentials may appear in any output, log, state, or telemetry data
- This is enforced at the code level via fail-closed scanning and at the protocol level via redaction rules
- Any detection of raw secrets in any context (state.db, ingestion.jsonl, logs, notifications) triggers immediate scanner failure (OVERSIZED_SOURCE-class behavior)

**END OF PHASE 0 FINAL OBJECTIVE V4**