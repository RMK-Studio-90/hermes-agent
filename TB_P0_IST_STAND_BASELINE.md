# TB-P0 Ist-Stand & Testbaseline — t_e8b126a5

> **Provenance:** Dieses Dokument konsolidiert die Ergebnisse von **Run 216**
> (Kanban-Task `t_e8b126a5`, Profil rmk-dev, 2026-08-25 17:09–18:13,
> endete mit Budget-Erschöpfung 150/150 *nach* Abschluss der fachlichen Arbeit).
> Die Zahlen wurden bei der Recovery am 2026-08-25 stichprobenartig re-verifiziert
> (siehe Abschnitt „Recovery-Verifikation"). Keine Full-Suite-Reruns.

## 1. Repository-Ist-Stand

| Item | Wert |
|---|---|
| Workspace | `C:/Users/RMK/AppData/Local/hermes/hermes-agent` |
| Branch | `main` |
| HEAD | `1bbb6e5bc` — docs(skill): troubleshoot stale web_extract pages |
| Working Tree | einzig `package-lock.json` modifiziert (fremde Vormutation: entfernte `"peer": true`-Einträge, npm-Version-Skew-Artefakt; vor Run 216 vorhanden) |
| Code-Änderungen durch Run 216 | **keine** |

**Umgebungsänderung durch Run 216 (dokumentiert, beabsichtigt, reversibel):**
pytest war im Release-venv nicht installiert → ohne ihn bricht der kanonische
Runner (`scripts/run_tests.sh`) mit „no virtualenv with pytest" ab. Installiert in
`venv/`: pytest 9.1.1, pytest-xdist 3.8.0, pytest-timeout 2.4.0,
pytest-asyncio 1.4.0, pytest-cov 7.1.0 (+ pluggy-Upgrade, execnet, iniconfig, coverage).
Rückholbar via pip uninstall. Dies ist Teil der „Testbaseline fixieren".

## 2. Test-Infrastruktur

- **Kanonischer Einstieg:** `scripts/run_tests.sh` → `run_tests_parallel.py`
  (Subprozess-Isolation pro Datei, überspringt integration/e2e/docker).
- **Sammlung:** ~48.200 Testitems in ~3.264 Dateien
  (pytest `--collect-only -q`, Run 216).
- **Runner-Schätzung:** ~33.340 Tests (statisches Zählen von `def test_`;
  unterschätzt parametrisierte Tests — beide Zahlen hier dokumentiert).

## 3. Baseline-Ergebnisse (aus Run 216)

| Slice | Ergebnis | Quelle |
|---|---|---|
| `tests/test_hermes_constants.py` | **55 passed / 10 skipped / 3 FAILED** | Run 216 + re-verifiziert |
| `tests/verify` + `tests/ci` | **194 passed / 1 FAILED** | Run 216 |
| `tests/test_hermes_state.py` + `tests/state` | **324 passed / 6 skipped / 4 FAILED** | Run 216 |

Vollsuite wurde in Run 216 bewusst NICHT gefahren (~33k Tests, Budget);
Rest per Runner-`--slice` als separate Karte vorgesehen (Kindkarte TB-P1 bleibt
gemäß SERIAL_GATE bis unabhängigem PASS gesperrt).

## 4. Fehlerklassifikation (alle prä-existierend, keine Code-Regressions)

1. **WinError 1314 (Symlink-Privileg)** — Host ohne Admin/Developer-Mode:
   - `tests/test_hermes_constants.py:739` (dangling_legacy_symlink),
     `:753` (symlink_to_populated_dir), `tests/state/test_fts_runtime_rebuild.py:204`.
   - `tests/conftest.py:1221` bietet `@pytest.mark.require_symlinks`; die drei
     Tests tragen den Marker nicht. Umgebungsabhängig, kein Produktionsfehler.
2. **python3 = Microsoft-Store-Stub** —
   `tests/verify/test_environment_and_runner.py:135` startet `python3 -m http.server`;
   auf diesem Host löst `python3` zum Store-Stub („Python wurde nicht gefunden").
3. **chmod-0o000-Verzeichnis auf Windows listbar** —
   `tests/state/test_fts_runtime_rebuild.py::…cmdline_fallback`: verifiziert per Probe;
   `os.listdir()` auf chmod-0o000 liefert auf Windows `[]` statt PermissionError →
   Cmdline-Fallback greift nie → `holders == []`. Linux-Verhalten ohne Plattform-Gating.
4. **Read-Pool-Trace-Designlücke** —
   `tests/test_hermes_state.py:846 test_search_projection_skips_context_enrichment_queries`
   (Root Cause in Run 216 durch 9 Probes eingekreist): bei aktivem WAL
   (SQLite 3.53.1) läuft die Context-Query auf einer gepoolten Read-Connection
   (`_READ_POOL_MAX=8`), die weder `db._conn` noch die getracete Connection ist;
   Trace-Hooks decken den Read-Pool nicht ab. Deterministisch rot auf diesem Host.

## 5. Offene Punkte (außerhalb des P0-Scope, bedürfen GO/eigener Karten)

- (a) `require_symlinks`-Marker auf 3 Tests ergänzen
- (b) Plattform-Gating für `/proc`-Tests
- (c) Entscheidung zum Read-Pool-Trace-Design im Projection-Test
- (d) Rest-Suite per `--slice` (TB-P1)

## 6. Recovery-Verifikation (2026-08-25, nach Run 216)

Stichprobe ohne Full-Rerun:
- `venv/Scripts/python -m pytest tests/test_hermes_constants.py -q`
  → `3 failed, 55 passed, 10 skipped` ✅ identisch zu Run 216
- `venv/Scripts/python -m pytest tests/state/test_fts_runtime_rebuild.py tests/test_hermes_state.py -q`
  → `3 failed, 262 passed` — dieselben drei Failure-Klassen (Symlink ×2 bzw. ×1,
  cmdline_fallback, Projection-Trace); Teilslice von Run 216s Kombination, konsistent
- `git status --porcelain` → nur `package-lock.json` ✅
- HEAD `1bbb6e5bc` ✅
