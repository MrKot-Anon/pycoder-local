"""Project-scoped, exact command approvals. This is a policy, not an OS sandbox."""

import hashlib
import json
import threading

from .util import private_json


class CommandPolicy:
    def __init__(self, root, directory):
        self.lock = threading.RLock()
        self.root = str(root.resolve())
        digest = hashlib.sha256(self.root.encode()).hexdigest()
        self.path = directory / "permissions" / (digest + ".json")
        self.session = set()
        self.saved = set()
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("workspace") != self.root:
                raise ValueError("Permission file belongs to another workspace")
            self.saved = set(data.get("commands", []))

    @staticmethod
    def key(command, cwd, shell):
        # Full command, not an unsafe lexical prefix. Workdir and shell matter.
        return json.dumps([str(cwd.resolve()), shell, command], ensure_ascii=False)

    def allows(self, key):
        with self.lock:
            return key in self.session or key in self.saved

    def remember(self, key, permanent=False):
        with self.lock:
            if permanent:
                saved = self.saved | {key}
                private_json(self.path, {"workspace": self.root, "commands": sorted(saved)})
                self.saved = saved
            else:
                self.session.add(key)

    def clear(self):
        with self.lock:
            private_json(self.path, {"workspace": self.root, "commands": []})
            self.saved.clear()
            self.session.clear()
