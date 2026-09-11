"""Deterministic workload classification; never selects concrete models."""
from dataclasses import dataclass
import re


ROUTES = frozenset({"rmk-fast", "rmk-general", "rmk-reason", "rmk-code",
                    "rmk-research", "rmk-vision"})


@dataclass(frozen=True)
class Workload:
    route: str
    task_class: str
    reason: str


# Ordered, deterministic keyword rules; first match wins, so the more specific
# workloads are tested before the broader ones. Stems end in ``\w*`` because an
# inflected or plural form has to match too ("unit tests", "refactoring",
# "programmierst") — a bare ``\b`` after the singular silently missed those and
# sent plainly-code work to rmk-general. Deliberately conservative: a prompt with
# no clear signal stays general rather than inventing a capability requirement.
_RULES = tuple((task_class, re.compile(pattern)) for task_class, pattern in (
    ("code", r"\b(code|codebase|coding|compile\w*|debug\w*|refactor\w*|repositor\w+"
             r"|implement\w*|implementier\w*|programmier\w*"
             r"|unit ?test\w*|regression ?test\w*|pytest|stack ?trace|traceback"
             r"|function|funktion|method|methode|class|klasse|module|modul"
             r"|api|endpoint|sdk|cli|script|skript"
             r"|bug|bugfix|patch|pull request|merge conflict"
             r"|python|javascript|typescript|rust|golang|java|sql|bash|powershell)\b"),
    ("research", r"\b(research\w*|recherch\w+|sources|quellen|literature|literatur"
                 r"|citation\w*|zitat\w*|state of the art|survey|marktanalyse"
                 r"|find out|herausfinden|look up|nachschlagen)\b"),
    ("reason", r"\b(reason\w*|architect\w+|architektur\w*|prove|proof|bewei\w+"
               r"|trade[- ]?offs?|abwäg\w*"
               r"|complex analysis|komplexe analyse|analy[sz]\w+"
               r"|strateg\w+|decision|entscheid\w+"
               r"|why does|warum|explain how|erkläre wie)\b"),
    ("fast", r"\b(translate|übersetze\w*|uppercase|lowercase"
             r"|extract|extrahier\w*|summari[sz]e|zusammenfass\w*"
             r"|rename|umbenenn\w*|reformat|format this)\b"),
))


def classify_workload(prompt: str = "", *, route: str | None = None,
                      vision: bool = False) -> Workload:
    """Explicit routes are validated; image requirements always stay visible.

    Text rules are deliberately lightweight and conservative. Ambiguous work
    remains general rather than inventing capability or model evidence.
    """
    if route is not None:
        if route not in ROUTES:
            raise ValueError(f"Unknown logical route: {route!r}")
        return Workload(route, route.removeprefix("rmk-"), "explicit-route")
    if vision:
        return Workload("rmk-vision", "vision", "image-required")
    text = prompt.casefold()
    for task_class, pattern in _RULES:
        if pattern.search(text):
            return Workload(f"rmk-{task_class}", task_class, "workload-rule")
    return Workload("rmk-general", "general", "default-general")
