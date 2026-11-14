""".env.example — шаблон для /etc/task-manager/env, який читає systemd EnvironmentFile.

systemd, на відміну від python-dotenv, не відкидає коментар у кінці рядка: `TASKS_WIP_LIMIT=4  # …`
стає значенням `4  # …`, і сервіс падає на int(). Тому в шаблоні — лише KEY=value.

Запуск: uv run python -m unittest discover -s tests -p 'test_env_example.py' -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"
ASSIGN = re.compile(r"^\s*(export\s+)?[A-Za-z_][A-Za-z0-9_]*=")


class EnvExampleTests(unittest.TestCase):
    def assignments(self):
        return [(n, line) for n, line in enumerate(EXAMPLE.read_text(encoding="utf-8").splitlines(), 1)
                if ASSIGN.match(line)]

    def test_no_inline_comments(self):
        bad = [n for n, line in self.assignments() if re.search(r"\s#", line.split("=", 1)[1])]
        self.assertEqual(bad, [], f"коментар у кінці рядка (рядки {bad}) — винеси окремим рядком")

    def test_wip_limit_is_15(self):
        self.assertIn("TASKS_WIP_LIMIT=15", [line.strip() for _, line in self.assignments()])

    def test_no_export(self):
        bad = [n for n, line in self.assignments() if line.lstrip().startswith("export")]
        self.assertEqual(bad, [], "systemd EnvironmentFile не розбирає 'export'")


if __name__ == "__main__":
    unittest.main()
