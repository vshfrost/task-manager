"""Статуси `todo` ([>]), `blockers` ([b]) і `in_review` ([r]): парсинг, round-trip, відсутність ліміту, фільтр.

Запуск: uv run python -m unittest tests.test_statuses -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase  # noqa: E402
from planner_model import STATUSES, parse_task_line  # noqa: E402
from planner_store import PlannerError, PlannerStore  # noqa: E402


class ModelTests(unittest.TestCase):
    def test_columns_in_board_order(self):
        self.assertEqual(
            STATUSES, ("backlog", "todo", "blockers", "in_progress", "in_review", "testing", "done")
        )

    def test_gt_parses_as_todo_and_roundtrips(self):
        line = "- [>] Взяти в роботу  #побут ⏫ 📅 2026-10-01 ⚡S 📌[Взяти] ^ab12cd"
        t = parse_task_line(line)
        self.assertIsNotNone(t)
        self.assertEqual((t.status, t.text), ("todo", "Взяти в роботу"))
        self.assertEqual(t.render(), line)

    def test_b_parses_as_blockers_and_roundtrips(self):
        line = "- [b] Чекає на відповідь  #робота ⏫ 📌[Чекає] ^ef56ab"
        t = parse_task_line(line)
        self.assertIsNotNone(t)
        self.assertEqual((t.status, t.text), ("blockers", "Чекає на відповідь"))
        self.assertEqual(t.render(), line)

    def test_r_parses_as_in_review_and_roundtrips(self):
        line = "- [r] Чекає рев'ю  #розробка 🔺 [[projects/x/overview]] 📌[Рев'ю] ^cd34ef"
        t = parse_task_line(line)
        self.assertIsNotNone(t)
        self.assertEqual((t.status, t.text), ("in_review", "Чекає рев'ю"))
        self.assertEqual(t.render(), line)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
        self.store = PlannerStore(self.root)

    def create(self, text, status):
        try:
            return self.store.add_task(text, title=text, status=status)
        except PlannerError as e:
            self.fail(f"створення в статусі {status} відхилено: {e}")

    def test_todo_and_in_review_have_no_limit(self):
        # store.add_task(status=...) — створення, не перехід: правила переходів не діють.
        for status in ("todo", "in_review"):
            with self.subTest(status=status):
                for i in range(20):
                    self.create(f"{status}-{i}", status)
                got = [t for t in self.store.list_tasks() if t["status"] == status]
                self.assertEqual(len(got), 20)
        text = (self.root / "tasks.md").read_text(encoding="utf-8")
        self.assertEqual((text.count("- [>] "), text.count("- [r] ")), (20, 20))

    def test_list_filters_by_todo_and_in_review(self):
        for status in ("backlog", "todo", "in_progress", "in_review", "testing", "done"):
            self.create(f"t-{status}", status)
        for status in ("todo", "in_review"):
            with self.subTest(status=status):
                got = self.store.list_tasks(status=status)
                self.assertEqual([t["text"] for t in got], [f"t-{status}"])


class ToolTests(ServerCase):
    def test_list_tasks_status_filter_todo_and_in_review(self):
        (self.root / "tasks.md").write_text(
            "# Tasks\n- [ ] Колись ^aa0001\n- [>] Наступна ^aa0002\n- [r] На рев'ю ^aa0003\n",
            encoding="utf-8",
        )
        self.assertEqual(self.call("list_tasks", status="todo"), "`aa0002` [to do] Наступна · @global")
        self.assertEqual(self.call("list_tasks", status="in_review"), "`aa0003` [in review] На рев'ю · @global")

    def test_todo_and_in_review_are_known_statuses(self):
        tid = self.add()
        self.to_todo(tid)  # backlog → todo через MCP заборонено, тож фікстура йде в стор
        self.assertIn("[to do]", self.call("list_tasks", status="todo"))
        self.assertNotIn("Невідомий статус", self.call("list_tasks", status="in_review"))


if __name__ == "__main__":
    unittest.main()
