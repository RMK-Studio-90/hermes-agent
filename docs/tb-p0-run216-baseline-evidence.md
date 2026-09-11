# TB-P0 Baseline-/Evidenzartefakt — Konsolidierung Run 216

**Task:** t_e8b126a5 ([TB-P0] Belegter Ist-Stand und Testbaseline fixieren)
**Recovery-Karte:** t_567194da
**Authoritative Quelle:** Run 216 (Log: `C:/Users/RMK/AppData/Local/hermes/cache/terminal-output/out-1787678319-31300-ac50.log`)
**Erstellt:** 2026-08-25, durch Recovery-Run t_567194da / rmk-dev

> **Scope dieses Artefakts:** Reine Konsolidierung der in Run 216 bereits erzeugten
> Evidenz. Es wurde KEINE neue Testbaseline ausgeführt, keine Tests wiederholt,
> keine technische Untersuchung erneut durchgeführt. Jede fachliche Tatsache unten
> ist mit „Quelle: Run 216" markiert. Neue, während dieser Recovery erhobene
> Metadaten sind ausschließlich im Abschnitt „Observed during Recovery" getrennt
> gekennzeichnet.

---

## 1. Repository State zum Zeitpunkt von Run 216

*Quelle: Run 216*

- Snapshot-HEAD: `1bbb6e5bc`
- Branch: `main`
- Vorbestehende, fremde Änderung: `package-lock.json` (modifiziert).
  Diese Änderung liegt außerhalb des Scope dieser Recovery: sie wird weder
  geändert, noch zurückgesetzt, noch einbezogen oder attribuiert.

## 2. Run-216-Umgebungsergänzungen (Test-Umgebung)

*Quelle: Run 216*

Folgende Pakete wurden in Run 216 in das Repo-venv
(`C:/Users/RMK/AppData/Local/hermes/hermes-agent/venv`) installiert — vorher
enthielt das venv kein pytest, sodass keine Testbaseline ausführbar war
(`scripts/run_tests.sh` brach mit „no virtualenv with pytest" ab):

| Paket | Version |
|---|---|
| pytest | 9.1.1 |
| pytest-xdist | 3.8.0 |
| pytest-timeout | 2.4.0 |
| pytest-asyncio | 1.4.0 |
| pytest-cov | 7.1.0 |

## 3. Collection-Observationen (Näherungen)

*Quelle: Run 216*

- Ungefähr **48.200 sammelbare Testitems** in **3.264 Testdateien**
  (`venv/Scripts/python -m pytest --collect-only -q`).
- Diese Zahlen sind **methodenabhängige Näherungen**: kein Vollsuite-Lauf,
  kein verifiziertes vollständiges Inventar, **kein PASS**.
  (Hinweis: die Run-216-Zusammenfassung erwähnte an anderer Stelle ~33k Tests;
  die konsistentere Sammlungsbeobachtung sind ~48.200 Items. Beide Werte werden
  unverändert übernommen und nicht harmonisiert.)

## 4. Tatsächlich begrenzt ausgeführte Läufe in Run 216

*Quelle: Run 216*

Kanonischer Runner: `scripts/run_tests.sh` → `run_tests_parallel.py`
(pro-Datei-Subprozess-Isolation, überspringt integration/e2e/docker).

| Slice | Ergebnis |
|---|---|
| `tests/test_hermes_constants.py` | 55 passed / 10 skipped / 3 failed |
| `tests/verify` + `tests/ci` | 194 passed / 1 failed |
| `tests/test_hermes_state.py` + `tests/state` | 324 passed / 6 skipped / 4 failed |

Dies sind begrenzte Teilläufe — **keine Vollsuite**, kein Gesamtpass.

## 5. Baseline Findings (Fehlerklassifikation)

*Quelle: Run 216*

Alle ohne Quellcode-Regression; keine weitere Ursachenanalyse darüber hinaus:

1. **WinError 1314** („Dem Client fehlt ein erforderliches Recht") bei
   Symlink-Erstellung auf Host ohne Admin-/Developer-Mode — betroffen u.a.
   `tests/test_hermes_constants.py:739` (dangling_legacy_symlink),
   `:753` (symlink_to_populated_dir), `tests/state/test_fts_runtime_rebuild.py:204`.
   Die drei Tests tragen den Skip-Marker `@pytest.mark.require_symlinks`
   (`tests/conftest.py:1221`) nicht. Umgebungsabhängig, kein Produktionsfehler.
2. Fehlender/ungeeigneter **Windows-python3-Store-Alias** (Host-Problem, nicht Code).
3. Windows-Verhaltensabweichung bei **chmod 0o000**: `os.listdir()` auf einem
   0o000-Verzeichnis liefert auf Windows `[]` statt `PermissionError` →
   Cmdline-Fallback greift nie → `holders == []`. Linux-Verhalten ohne
   Plattform-Gating.
4. Mindestens ein kombinierter Lauf deutete **potenziell reihenfolgeabhängiges
   Verhalten** an (Details nicht weiter untersucht).

## 6. Suite-Status

*Quelle: Run 216*

- Die **Vollsuite wurde nicht abgeschlossen** (Iterationsbudget 150/150
  erschöpft; Terminal-/Zeitbudget).
- Run 216 meldete **STATUS: PARTIAL** und **REVIEW_READY**.
- **REVIEW_READY ist eine Selbstmeldung des ausführenden Runs und kein
  unabhängiger Review-PASS.**

## 7. Observed during Recovery (neue Metadaten, NICHT aus Run 216)

Diese Angaben wurden während der Konsolidierung am 2026-08-25 ausschließlich
per Lesezugriff (`git rev-parse`, `git status`, `git rev-list`) erhoben. Beide
Repositories wurden **unverändert gelassen**:

- Haupt-Repository `C:/Users/RMK/AppData/Local/hermes/hermes-agent`:
  - HEAD: `1bbb6e5bc`, Branch: `main`.
  - Sichtbare Änderung: `package-lock.json` (modifiziert) — unverändert lassen,
    gehört nicht zur Recovery.
  - `main...origin/main`: beobachtet **behind 3**. (Abweichung vom letzten
    bekannten Stand „behind 1" laut Kartentext; hier wird ausschließlich die
    frische Beobachtung dokumentiert.)
- Staging-Worktree `C:/Users/RMK/AppData/Local/hermes/hermes-agent-turn-budget`:
  - Branch: `staging/turn-budget-architecture`; Arbeitsbaum **clean**.

## 8. Unsupported/Missing Claims

Die folgenden Punkte sind **nicht durch Run 216 belegt** und werden ausdrücklich
als unsupported/missing markiert, nicht inferiert:

- **Missing:** Vollsuite-Ergebnis (~48.200 Items) — nie vollständig ausgeführt;
  übrige Subtrees (tools, hermes_cli, gateway, agent, …) ungezählt.
- **Missing:** Verifiziertes vollständiges Testinventar — nur methodenabhängige
  Collection-Näherungen.
- **Missing:** Unabhängiger Review-PASS für Run 216 — REVIEW_READY war
  Selbstmeldung; Review-Versuch t_2ecd97eb endete weder mit PASS noch REJECT
  und wird nicht als formaler Review behandelt.
- **Missing:** Ursachenanalyse über die Klassifikation in Abschnitt 5 hinaus —
  wurde in Run 216 bewusst nicht durchgeführt.
- **Unsupported:** Aussagen über Reihenfolgeabhängigkeit über „mindestens ein
  kombinierter Lauf deutete es an" hinaus — keine belastbare Evidenz.
- **Unsupported:** Exakte Testanzahl — 48.200 vs. ~33k widersprechende
  Nennungen in Run 216 wurden nicht aufgelöst.

---

## Recovery-Manifest (Acceptance Criteria)

- Tests executed: **NONE**
- Source-code changes: **NONE**
- package-lock.json touched: **NO**
- Files created: genau diese eine Datei (untracked im Repo, kein Commit)
- Files modified: keine
