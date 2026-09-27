# update-safe — RMK Safe Update / Update Guardian

Guardian, der den PRODUKTIVEN Checkout (`E:\KI\Hermes\hermes-agent`) vor
Upstream-Updates schützt. Ein Update verändert die Produktion NIEMALS direkt.

## Workflow

`CHECK -> ISOLATED UPDATE -> MERGE -> VERIFY -> CERTIFY -> PROMOTE`

- **CHECK**: `--check` ist read-only (Update verfügbar? Contract-Gate grün?).
- **ISOLATED UPDATE**: Merge läuft in einem temporären git worktree
  (`<worktrees>/update-<ts>`), Produktion bleibt unangetastet.
- **MERGE**: BASE/OURS/THEIRS-Merge ohne globales ours/theirs; Konflikt oder
  nicht-certifizierbares Ergebnis → `UPDATE_NEEDS_REVIEW`, Prod unverändert.
- **VERIFY/CERTIFY**: Certification-Gate = Git-Hygiene (UU=0, keine Marker,
  diff --check, keine untracked) + Source-Compile + Regression-Suiten +
  RMK Contract Registry (16 Behaviour-Contracts) + Baseline-Differential
  (KNOWN_BASELINE_FAILURE vs REGRESSION).
- **PROMOTE**: nur nach `UPDATE_CERTIFIED`; `promote_certified()` aktualisiert
  Prod atomar, smoket danach, und bei Post-Smoke-Fehler → `AUTO_ROLLBACK`
  zum Last-Known-Good (Safestate-HEAD). Lokale uncommitted RMK-Änderungen
  werden vor Promotion gesnapshottet und nach Rollback wiederhergestellt.

## Bedienung

```bash
hermes update-safe --check      # nur prüfen, nichts installieren
hermes update-safe              # voller safe Update-Lauf
hermes update-safe --no-promote # bis UPDATE_CERTIFIED, Prod unangetastet
hermes update-safe --branch main
```

Statustexte: `UPDATE_AVAILABLE / UPDATE_TESTING / UPDATE_NEEDS_REVIEW /
UPDATE_CERTIFIED / UPDATE_INSTALLED / UPDATE_ROLLED_BACK / UPDATE_ABORTED`.

## Reproduzierbarkeit

Jeder Lauf schreibt `.hermes/update-runs/<timestamp>/`:
`safestate.json` (HEAD, branch, upstream, status, config, runtime),
`baseline.json` (Test-/Contract-Fingerabdruck) und `report.json`
(old HEAD → neue HEAD, Merge-Entscheidungen, Gate-Steps, Baseline-Diff,
Promotion-Ergebnis, Rollback-Target).

## Bausteine (Erweiterung, keine zweite Engine)

Orchestriert Bestehendes: `backup.create_quick_snapshot` / `rmk_safestate`
(Safestate), git worktree (Isolation), `update_cmd_*`-Familie (Update/Check/
Autostash/Backup), `update_receipt` (Receipt-Konvention). Neu: Contract
Registry (`update_safe_contracts.py`), Baseline (`update_safe_baseline.py`),
Guardian (`update_safe.py`), CLI (`subcommands/update_safe.py`).

## Sicherheitsregel

Ein fehlgeschlagenes oder nicht eindeutig zertifiziertes Update ersetzt den
laufenden produktiven Stand unter keinen Umständen (fail-closed).
