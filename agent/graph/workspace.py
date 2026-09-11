"""Controller-owned file capabilities and content-bound test evidence."""

import hashlib
import json
from pathlib import Path

from .process import run_process


class Workspace:
    def __init__(self, request):
        self.request = request
        self.root = Path(request.workspace).resolve() if request.workspace else None

    def path(self, name):
        if name not in self.request.files or self.root is None:
            raise ValueError("File is outside the task capability")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError("Unsafe workspace path")
        path = self.root / relative
        for candidate in (path, *path.parents):
            if candidate == self.root:
                break
            if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                raise ValueError("Symlink/junction scope is not supported")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Path escaped workspace")
        if path.exists() and not path.is_file():
            raise ValueError("Scoped path must be a file")
        return path

    def snapshot(self):
        result = {}
        for name in self.request.files:
            path = self.path(name)
            if path.exists() and path.stat().st_size > 200000:
                raise ValueError("Scoped file exceeds context limit")
            result[name] = path.read_bytes().decode("utf-8") if path.exists() else None
        return result

    @staticmethod
    def digest(files):
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def apply(self, replacements, expected):
        if self.request.task_type != "software_change":
            if replacements:
                raise ValueError("Text tasks cannot modify files")
            return
        if not isinstance(replacements, dict):
            raise ValueError("files must be a replacement object")
        if self.snapshot() != expected:
            raise ValueError("Workspace changed while executor was running")
        for name, content in replacements.items():
            self.path(name)
            if not isinstance(content, str) or len(content.encode()) > 200000:
                raise ValueError("Replacement must be bounded UTF-8 text")
        # All replacements validated before the first write. Crashes leave a running
        # checkpoint requiring reconciliation; never silently replay a partial edit.
        for name, content in replacements.items():
            path = self.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))

    async def test(self):
        before = self.digest(self.snapshot())
        evidence = []
        for command in self.request.test_commands:
            code, output = await run_process(command, cwd=str(self.root), timeout=self.request.node_timeout)
            evidence.append({"argv": command, "exit_code": code, "output": output[-12000:]})
            if code:
                break
        after = self.digest(self.snapshot())
        passed = before == after and all(e["exit_code"] == 0 for e in evidence)
        return {"passed": passed, "digest": after, "checks": evidence,
                "reason": "Tests changed scoped artifacts" if before != after else ""}
