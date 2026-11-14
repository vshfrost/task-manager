"""TASKS_STATE_DIR: де живе OAuth-стан і з якими правами (сервер: юзер tasks, /var/lib/task-manager).

Запуск: uv run python -m unittest discover -s tests -p 'test_state_dir.py' -v
"""
from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESOURCE = "https://tasks.example/tasks"
REDIRECT = "https://client.example/cb"


class StateDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Інші тести тримають посилання на вже імпортований модуль — повертаємо саме його.
        original = sys.modules.get("tasks_oauth")
        self.addCleanup(lambda: sys.modules.__setitem__("tasks_oauth", original)
                        if original else sys.modules.pop("tasks_oauth", None))

    def load(self, env: dict):
        with patch.dict(os.environ, env, clear=False):
            if "TASKS_STATE_DIR" not in env:
                os.environ.pop("TASKS_STATE_DIR", None)
            sys.modules.pop("tasks_oauth", None)
            return importlib.import_module("tasks_oauth")

    def test_default_next_to_code(self):
        m = self.load({})
        self.assertEqual(m.STATE_FILE, Path(m.__file__).resolve().parent / ".oauth_state.json")

    def test_blank_env_is_default(self):
        m = self.load({"TASKS_STATE_DIR": "  "})
        self.assertEqual(m.STATE_FILE.name, ".oauth_state.json")

    def test_state_dir(self):
        d = Path(self.tmp.name) / "state"
        m = self.load({"TASKS_STATE_DIR": str(d)})
        self.assertEqual(m.STATE_FILE, d / "oauth_state.json")

    def test_save_owner_only_and_atomic(self):
        d = Path(self.tmp.name) / "state"
        m = self.load({"TASKS_STATE_DIR": str(d)})
        p = m.TasksOAuthProvider(RESOURCE, RESOURCE, redirect_allow=(REDIRECT,))
        p.clients["offline-client"] = {"client_id": "offline-client", "redirect_uris": [REDIRECT]}
        old = os.umask(0)  # навіть з umask 0 файл лишається лише для власника
        try:
            p._save()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(m.STATE_FILE.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        self.assertEqual(sorted(x.name for x in d.iterdir()), ["oauth_state.json"])  # без .tmp
        data = json.loads(m.STATE_FILE.read_text(encoding="utf-8"))
        self.assertIn("offline-client", data["clients"])
        again = m.TasksOAuthProvider(RESOURCE, RESOURCE, redirect_allow=(REDIRECT,))
        self.assertIn("offline-client", again.clients)


if __name__ == "__main__":
    unittest.main()
