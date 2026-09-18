# Hermes-natives "Dreaming": Architekturvorschlag

Basierend auf dem, was in `hermes-agent` tatsächlich existiert — nicht auf dem Anthropic-Whitepaper nacherzählt. Kernprinzip: **kein neues Subsystem bauen, sondern zwei bereits vorhandene Bausteine kombinieren.**

## Was schon da ist und wiederverwendet wird

1. **`session_search`** — ein eingebautes Agenten-Tool (`agent/inline_tool_executors.py:164`, `agent/turn_tool_round.py:23`), mit dem ein Agent seine eigene Sessionhistorie durchsuchen kann. Das ist exakt die "Replay"-Stufe aus dem Anthropic-Modell — kein Custom-Parsing der rohen `sessions/request_dump_*.json`-Dateien nötig (die sind ohnehin Fehler-/Debug-Dumps, keine sauberen Transkripte — `reason`/`error`-Felder bestätigen das).
2. **`memory`** — das eingebaute Memory-Tool (OAuth-Alias `context_notes`, `agent/anthropic_adapter.py:253`).
3. **Das cron-System selbst** (`cron/jobs.py`, bereits geprüft und lebendig) — kein neuer Scheduler nötig.
4. **Das Muster aus `RMK-KNOWLEDGE Knowledge Maintenance`** (`rmk-knowledge/cron/jobs.json`, id `1fd3496dbf96`): strikte Scope-Begrenzung, `[SILENT]`-Konvention bei unverändertem Zustand, Hash-Chain für Idempotenz, explizite Nicht-Ziele. Genau dieses Gerüst wird für den Dreaming-Job übernommen statt neu erfunden.

**Wichtiger Befund während der Recherche:** Alle 9 Profile haben ein leeres `memories/`-Verzeichnis — das ist offenbar ungenutzte Infrastruktur, nicht der kanonische Memory-Store (der ist laut `rmk-knowledge`-Prompt "Obsidian"/`knowledge_store`). Der Dreaming-Job schreibt deshalb NICHT in `memories/`, sondern erzeugt einen Review-Report plus optional einen `memory`-Tool-Aufruf für unstrittige Fakten.

## Vorgeschlagenes Design

### Zwei-Stufen-Verfahren (non-destruktiv, wie im Original)

**Stufe 1 — Dream-Lauf (Agent-Job, LLM-gestützt):**
Liest die letzten N Sessions seit dem letzten Lauf via `session_search`, extrahiert wiederkehrende Fehler/Präferenzen/veraltete Fakten, schreibt einen **Vorschlag** (nicht live) in `reports/dream_proposals/<datum>.md`. Nur eindeutige, mehrfach bestätigte Fakten gehen direkt über `memory` in den Live-Kontext — alles Unsichere bleibt im Report zur manuellen Freigabe. Das entspricht Anthropics eigenem "Handoff"-Schritt (auto-live vs. review-first) UND deinem eigenen K01-K09-Prinzip ("Improvements müssen reproduzierbar, testbar, reversibel, unabhängig geprüft sein").

**Stufe 2 — Bookmark/Idempotenz:**
Wie bei `rmk-knowledge`: eine Marker-Datei (`cron/dream_bookmark.json`) hält fest, bis zu welchem Session-Zeitstempel schon geträumt wurde. Kein erneutes Verarbeiten derselben Sessions, kein Duplikat-Report.

### Zielprofil: Vorschlag `rmk-dev`

Begründung: leeres `memories/`, aber der mit Abstand aktivste `sessions/`-Ordner (viele Request-Dumps, aktiv für Implementierungsarbeit genutzt) — der Job hat hier sofort sichtbaren Nutzen (0 → kuratierte Erkenntnisse). Passt außerdem zur SOUL.md-Rolle von `rmk-dev` ("technical implementation specialist"), solange der Job strikt auf Memory-Kuration beschränkt bleibt (keine Code-Änderungen, kein Scope-Creep — SOUL.md verlangt das ohnehin: "Do not redefine scope", "report FOLLOW-UP OPPORTUNITY" statt selbst handeln).

**Alternative:** Jedes andere Profil geht genauso; sag einfach welches.

### Konkrete Job-Definition (Entwurf, passend zum `jobs.json`-Schema)

```json
{
  "name": "RMK-DEV Dreaming — Session Reflection",
  "prompt": "<siehe unten>",
  "schedule": { "kind": "cron", "expr": "30 4 * * *", "display": "täglich 04:30" },
  "model": "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
  "provider": "custom:omniroute-free",
  "base_url": "http://127.0.0.1:20128/v1",
  "deliver": "local",
  "enabled": true
}
```

Modell/Provider bewusst identisch zu `RMK-KNOWLEDGE Daily Knowledge Audit` gewählt — läuft über dein bestehendes free-combo/OmniRoute-Setup, keine zusätzlichen Kosten, kein Bruch mit dem K09-Ziel "free-first / zero-cost-proof".

### Prompt-Entwurf (deutsch, im Hausstil der bestehenden Jobs)

```
Du bist rmk-dev. Befolge SOUL.md. Fuehre einen begrenzten Dream-Lauf durch: Reflektiere
die eigene Sessionhistorie, nicht den Code.

Nutze session_search, um Sessions seit dem letzten Dream-Bookmark
(cron/dream_bookmark.json, Feld last_session_ts) zu lesen. Maximal 100 Sessions je Lauf
(Anthropic-Referenzwert, hier als Kappung uebernommen). Kein Bookmark vorhanden: die
letzten 20 Sessions.

Identifiziere ausschliesslich:
1. wiederkehrende Fehler (gleicher Fehler in mindestens 2 unabhaengigen Sessions),
2. stabile Arbeitspraeferenzen (Format, Tonalitaet, Tooling), die in mindestens 3
   Sessions konsistent auftraten,
3. veraltete Fakten (eine fruehere Session widerspricht einer spaeteren zum selben Thema).

Keine Spekulation, keine Verallgemeinerung aus einem Einzelfall. Bei Unsicherheit:
auslassen, nicht raten (Anthropics eigenes Risiko "hallucinated memory" gilt genauso
hier).

Nur eindeutig bestaetigte, risikofreie Fakten (Kategorie 2 und 3, mehrfach belegt) per
memory-Tool direkt speichern. Alles aus Kategorie 1 sowie alles mit Unsicherheit geht
NICHT direkt in memory, sondern ausschliesslich in den Report.

Schreibe den Report nach reports/dream_proposals/<YYYY-MM-DD>.md mit: gelesene
Sessions (Anzahl + Zeitraum), gefundene Muster je Kategorie mit Sessions-ID-Beleg,
was direkt gespeichert wurde, was zur manuellen Pruefung offen bleibt und warum.

Aktualisiere cron/dream_bookmark.json auf den Zeitstempel der zuletzt gelesenen
Session.

Keine Code-Aenderungen. Keine Recherche. Keine Nachrichten versenden. Bei
unveraendertem Zustand seit letztem Lauf (keine neuen Sessions): [SILENT].
```

## Was noch NICHT belastbar ist: echte "Idle"-Erkennung

Das war der eigentliche Namensbestandteil im Phantom-Job ("idle catch-up") — und laut meiner letzten Prüfung existiert Machine-Idle-Detection nirgends im Code. Die gute Nachricht: es ist technisch machbar, weil die Cron-Backends nativ unter Windows laufen (nicht in einer Sandbox) — ein `script`-Job (`no_agent: true`, wie der bestehende `OpenRouter Catalog Watcher`) könnte per `ctypes.windll.user32.GetLastInputInfo` echte Windows-Idle-Zeit lesen und den Dream-Lauf nur bei z. B. ≥15 Minuten Idle plus Mindestabstand seit letztem Lauf auslösen.

**Was mir dafür noch fehlt und was ich nicht raten will:** der exakte Mechanismus, mit dem ein externes Script einen ANDEREN, bereits definierten Cron-Job zuverlässig auslöst. Es gibt keinen CLI-Subcommand `hermes cron trigger` (geprüft, existiert nicht in `hermes_cli/cron.py` oder `cli.py`) — die Desktop-"Trigger now"-Funktion geht über einen REST-Call an den lokalen Gateway-Port, der pro Profil dynamisch vergeben wird (`gateway_state.json`). Das ist lösbar, aber ich wollte keinen ungetesteten Trigger-Mechanismus gegen deine vier laufenden Produktiv-Backends bauen, ohne dass du das siehst.

**Pragmatischer Zwischenschritt (Vorschlag):** Erstmal zeitbasiert (04:30 nachts, s.o.) statt idle-getriggert starten — funktioniert garantiert, kein Risiko. Die Idle-Gate-Ergänzung liefere ich als zweiten Schritt nach, sobald ich den Trigger-Mechanismus sauber verifiziert habe (entweder über den Gateway-Port aus `gateway_state.json`, oder falls vorhanden über eine interne Python-Funktion, die ich direkt importieren kann statt über HTTP zu gehen).

## Offene Entscheidungen, bevor ich das live einrichte

Eine neue Cron-Konfiguration ist eine dauerhafte Änderung an deinem laufenden System — das schreibe ich nicht ungefragt in ein produktives `jobs.json`.
