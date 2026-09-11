"""Session-scoped clarification before a user-approved execution contract.

Intake has no tools. Only an exact, current start token can cross into HGES.
"""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import secrets
import sqlite3
import time
from uuid import uuid4

from .models import HermesModels
from .request import GraphRequest, route
from .workspace import Workspace

QUESTIONS = (
    "Was soll konkret entstehen und für wen? Bei einem Bot-Ersteller: Welche Arten von Bots soll er erstellen, und welche Aufgaben sollen diese übernehmen?",
    "Wo soll das Ergebnis laufen (Plattform, Technik, Projektordner), und welche Schnittstellen werden benötigt? Bitte keine Schlüssel oder Passwörter senden.",
    "Was darf Hermes selbst ausführen, was braucht deine Freigabe, und woran prüfen wir den Erfolg? Nenne auch Grenzen, etwa nur Code erzeugen statt Bots veröffentlichen.",
)
INSTRUCTION = """You clarify an execution request, never execute it. Reply in the user's language.
Return JSON {"questions": [up to 3 concrete unanswered questions], "request": null or an object}.
Read the whole dialogue. Ask only unresolved questions. 'yes', 'continue', 'just do it' and
similar vague answers do not resolve missing requirements. For bot builders clarify kinds
of generated bots, target platform/runtime, capabilities/integrations, autonomy boundaries,
workspace and observable success. Never assume deployment, credentials, paid services,
installation, network access or unrestricted execution. No secret values in dialogue.
When enough is known, propose a SMALL executable contract with goal, acceptance_criteria,
task_type (text or software_change), complexity (1..10), risk (low/medium/high), workspace,
files (exact relative paths), test_commands (argv arrays, no shell strings).
Code creation is software_change, never text. Text only produces an answer in chat.
The workspace must exist. The supplied candidate workspace is a hint, not consent.
You may propose implementation files and safe local test commands for explicit preview
approval, but must ask if the intended product, platform or permissions remain unclear.
Include ALL constraints in goal and acceptance criteria. Unavailable credentials or services
must be clarified, not silently replaced by stubs. Do not claim a deployed bot: this workflow
edits scoped UTF-8 files and runs the exact approved tests; deployments need a separate task.
Return questions OR a request. Do not provide budgets, board IDs or execution mode.
"""


class Intake:
    def __init__(self, session_id, *, path=None, models=None):
        if not session_id:
            raise ValueError("Graph benötigt eine Sitzung.")
        from hermes_constants import get_hermes_home
        self.path = Path(path or get_hermes_home() / "graph" / "intake.sqlite3")
        self.session_id = str(session_id)
        self.models = models

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS drafts (session_id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL)")
        return db

    def load(self):
        if not self.path.exists():
            return None
        db = self.connect()
        try:
            row = db.execute("SELECT version,data FROM drafts WHERE session_id=?", (self.session_id,)).fetchone()
            draft = dict(json.loads(row[1]), version=row[0]) if row else None
            if draft and draft["status"] == "running" and draft.get("graph_db"):
                path = Path(draft["graph_db"])
                if path.is_file():
                    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as graph_db:
                        run = graph_db.execute("SELECT state FROM graph_runs WHERE task_id=?", (draft["graph_id"],)).fetchone()
                    if run:
                        state = json.loads(run[0])
                        if state["status"] not in {"pending", "running"}:
                            draft.update(status=state["status"], summary=state.get("artifacts", {}).get("answer", "")
                                         or state.get("outputs", {}).get("direct", {}).get("output", {}).get("answer", "")
                                         or "\n".join(state.get("decisions", [])))
            return draft
        finally:
            db.close()

    def save(self, draft, expected):
        data = {k: v for k, v in draft.items() if k != "version"}
        db = self.connect()
        try:
            with db:
                if expected is None:
                    db.execute("INSERT INTO drafts VALUES (?,0,?)", (self.session_id, json.dumps(data)))
                    version = 0
                else:
                    cur = db.execute("UPDATE drafts SET version=version+1,data=? WHERE session_id=? AND version=?",
                                     (json.dumps(data), self.session_id, expected))
                    if cur.rowcount != 1:
                        raise ValueError("Der Auftrag wurde parallel geändert. Bitte /graph status verwenden.")
                    version = expected + 1
            draft["version"] = version
        finally:
            db.close()

    async def handle(self, text, *, cwd=None, progress=None, interrupted=None):
        text = text.strip()
        arg = text.partition(" ")[2].strip() if text.split(maxsplit=1)[0:1] == ["/graph"] else text
        draft = self.load()
        if arg in {"status", "show"}:
            return self.describe(draft)
        if arg in {"cancel", "abbrechen"}:
            if draft and draft["status"] == "running":
                return "Der Graph läuft. Verwende Stop in der App; danach /graph status."
            if draft:
                draft.update(status="cancelled", token=None)
                self.save(draft, draft["version"])
            return "Graph-Dialog abgebrochen."
        if draft and draft["status"] == "running":
            return self.describe(draft)
        if arg.startswith("start") and (arg == "start" or arg.startswith("start ")):
            return await self.start(draft, arg.removeprefix("start").strip(), progress, interrupted)
        if not draft or draft["status"] in {"cancelled", "succeeded", "failed", "needs_attention", "budget_exhausted"}:
            if not arg:
                return "Beschreibe dein Vorhaben: /graph <Auftrag>. Hermes klärt zuerst die Anforderungen."
            if len(arg) > 6000:
                return "Bitte das Vorhaben auf höchstens 6.000 Zeichen kürzen."
            expected = draft["version"] if draft else None
            draft = {"status": "collecting", "goal": arg, "dialogue": [arg], "questions": list(QUESTIONS),
                     "cwd": cwd, "token": None, "rounds": 0}
            self.save(draft, expected)
            return self.describe(draft)
        if not arg:
            return self.describe(draft)
        if len(arg) > 6000 or sum(map(len, draft["dialogue"])) + len(arg) > 14000:
            return "Bitte die Anforderungen kürzer fassen (höchstens 6.000 Zeichen pro Antwort)."
        if arg.casefold().strip(".! ") in {"ja", "yes", "ok", "okay", "weiter", "mach einfach", "leg los", "freigegeben"}:
            return self.describe(draft) + "\nBitte beantworte die offenen Fragen konkret bzw. verwende den angezeigten Startbefehl."
        if draft["rounds"] >= 12:
            return "Die Klärungsgrenze von 12 Modellaufrufen ist erreicht. Mit /graph cancel abbrechen und den Auftrag enger fassen."
        draft["dialogue"].append(arg)
        draft.update(status="collecting", token=None, request=None, rounds=draft["rounds"] + 1)
        self.save(draft, draft["version"])  # edits revoke approval BEFORE a provider call
        try:
            request = GraphRequest("Clarify request", ["Resolve missing requirements"], node_tokens=32000, output_tokens=4000)
            model = self.models or HermesModels(request)
            proposal, _ = await model("intake", INSTRUCTION, {"dialogue": draft["dialogue"], "candidate_workspace": draft["cwd"]})
            questions = proposal.get("questions")
            if not isinstance(questions, list) or len(questions) > 3 or any(not isinstance(q, str) or not q.strip() for q in questions):
                raise ValueError("Ungültige Rückfragen des Klärungsmodells")
            if questions:
                draft["questions"] = questions
            else:
                allowed = {"goal", "acceptance_criteria", "task_type", "complexity", "risk", "workspace", "files", "test_commands"}
                raw = proposal.get("request")
                if not isinstance(raw, dict) or set(raw) - allowed:
                    raise ValueError("Ungültiger Auftragsvorschlag")
                from hermes_cli.config import load_config_readonly
                from .budget import ExecutionBudget
                cfg = load_config_readonly().get("graph") or {}
                contract = GraphRequest.from_dict({**raw, "budget": asdict(ExecutionBudget(**cfg.get("budget", {}))),
                                                  **{k: cfg[k] for k in ("node_tokens", "output_tokens", "node_timeout") if k in cfg}})
                Workspace(contract).snapshot()
                draft.update(status="ready", request=asdict(contract), token=secrets.token_hex(4), questions=[])
            self.save(draft, draft["version"])
        except Exception as exc:
            return f"Der Auftrag bleibt in Klärung ({type(exc).__name__}). Es wurde nichts ausgeführt.\n" + self.describe(self.load())
        return self.describe(draft)

    def describe(self, draft):
        if not draft:
            return "Kein Graph-Auftrag. Beginne mit /graph <Vorhaben>."
        if draft["status"] == "collecting":
            return "Bevor Hermes startet, fehlen noch Angaben:\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(draft["questions"], 1)) + "\n\nAntworte hier im Chat. /graph cancel beendet den Dialog."
        if draft["status"] == "ready":
            r = GraphRequest.from_dict(draft["request"])
            mode, _ = route(r)
            return (f"Auftragsentwurf – noch nicht gestartet\n\nZiel: {r.goal}\n"
                    + "Erfolgskriterien:\n" + "\n".join("- " + c for c in r.acceptance_criteria)
                    + f"\nArbeitsordner: {r.workspace or '(Textausgabe im Chat)'}\nDateien: {json.dumps(r.files, ensure_ascii=False)}"
                    + f"\nTestbefehle (exakte Argumentlisten): {json.dumps(r.test_commands, ensure_ascii=False)}"
                    + f"\nAblauf: {mode}; Risiko: {r.risk}\nBudget: {r.budget.max_tokens} reservierte Tokens, "
                    + f"{r.budget.max_model_calls} Modellaufrufe, {r.budget.max_runtime_seconds:g} Sekunden, {r.budget.max_repair_cycles} Reparaturen."
                    + "\nBeim Start wird eine Kanban-Karte angelegt. Die Klärungsaufrufe zählen separat (maximal 12)."
                    + f"\n\nÄnderungen einfach antworten. Diesen Entwurf freigeben und ausführen: /graph start {draft['token']}")
        return (f"Graph: {draft['status']}\n{draft.get('summary', '')}\nGraph-ID: {draft.get('graph_id', '–')}"
                + ("\nBei einem Prozessabbruch bleibt der Auftrag gesperrt: zuerst den Graph-Checkpoint prüfen und mit hermes graph recover abgleichen; kein automatischer Neustart." if draft['status'] == 'running' else ""))

    async def start(self, draft, token, progress, interrupted):
        if not draft or draft["status"] != "ready" or not token or not secrets.compare_digest(token, draft["token"]):
            return "Kein passender freigegebener Entwurf. Bitte /graph status prüfen; nur der aktuelle Startbefehl gilt."
        request = GraphRequest.from_dict(draft["request"])
        Workspace(request).snapshot()
        draft.update(status="running", token=None, graph_id=str(uuid4()), started_at=time.time())
        self.save(draft, draft["version"])  # only one caller owns execution
        from .service import execute
        def on_progress(node):
            draft["summary"] = f"Aktueller Schritt: {node}"
            self.save(draft, draft["version"])
            if progress:
                progress(f"Graph-Schritt: {node}\n")
        def on_created(graph_db, card_id):
            draft.update(graph_db=str(graph_db), card_id=card_id)
            self.save(draft, draft["version"])
        task = asyncio.create_task(execute(request, task_id=draft["graph_id"], create_card=True,
                                           session_id=self.session_id, on_progress=on_progress, on_created=on_created))
        try:
            if progress:
                progress("Der freigegebene Graph startet.\n\n")
            while not task.done():
                await asyncio.wait({task}, timeout=0.25)
                if interrupted and interrupted():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    raise asyncio.CancelledError()
            record = await task
            draft["status"] = record["state"]["status"]
            state = record["state"]
            draft["summary"] = (state.get("artifacts", {}).get("answer", "")
                                or state.get("outputs", {}).get("direct", {}).get("output", {}).get("answer", "")
                                or "\n".join(state.get("decisions", [])))
        except BaseException as exc:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            draft.update(status="needs_attention", summary=f"Ausführung unterbrochen oder fehlgeschlagen ({type(exc).__name__}). Kein automatischer Neustart.")
            if not isinstance(exc, (Exception, asyncio.CancelledError)):
                raise
        finally:
            self.save(draft, draft["version"])
        return self.describe(draft)
