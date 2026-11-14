"""Ручний порядок To Do крізь MCP-тули і веб-API дашборда.

Запуск: uv run python -m unittest tests.test_todo_order_api -v
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase  # noqa: E402
from test_web_api import WebCase  # noqa: E402


class ToolsOrderTests(ServerCase):
    def setUp(self):
        super().setUp()
        (self.root / "todo-order.json").unlink(missing_ok=True)

    def listed(self, **filters) -> list[str]:
        out = self.call("list_tasks", status="todo", **filters)
        return re.findall(r"^`(\w+)`", out, flags=re.M)

    def test_move_task_into_todo_goes_last(self):
        a, b, c = (self.add(text=n, title=n) for n in "ABC")
        for tid in (c, a, b):
            self.to_todo(tid)
        self.assertEqual(self.listed(), [c, a, b])

    def test_list_tasks_todo_is_in_manual_order_not_by_priority(self):
        low = self.add(text="low", title="low", priority="low")
        asap = self.add(text="asap", title="asap", priority="asap", deadline="2026-01-01")
        high = self.add(text="high", title="high", priority="high")
        for tid in (low, asap, high):
            self.to_todo(tid)
        self.assertEqual(self.listed(), [low, asap, high])
        self.assertEqual(self.listed(priority="high"), [high])

    def test_list_tasks_limit_cuts_the_manual_order_tail(self):
        a, b, c = (self.add(text=n, title=n) for n in "ABC")
        for tid in (c, b, a):
            self.to_todo(tid)
        out = self.call("list_tasks", status="todo", limit=2)
        self.assertEqual(re.findall(r"^`(\w+)`", out, flags=re.M), [c, b])

    def test_task_that_left_todo_is_not_listed_and_returns_last(self):
        a, b = self.add(text="A", title="A"), self.add(text="B", title="B")
        for tid in (a, b):
            self.to_todo(tid)
        self.call("move_task", id=a, status="in_progress")
        self.assertEqual(self.listed(), [b])
        self.call("move_task", id=a, status="testing")
        self.call("move_task", id=a, status="todo")
        self.assertEqual(self.listed(), [b, a])


class WebOrderTests(WebCase):
    def setUp(self):
        super().setUp()
        (self.root / "todo-order.json").unlink(missing_ok=True)

    def to_todo(self, tid, position=None):
        body = {"id": tid, "status": "todo"}
        if position is not None:
            body["position"] = position
        return self.post("/api/task/edit", body)

    def listed(self, **query) -> list[str]:
        return [t["id"] for t in self.get("/api/tasks", status="todo", **query).json()["tasks"]]

    def test_drop_without_position_goes_last(self):
        a, b, c = (self.add()["id"] for _ in range(3))
        for tid in (b, c, a):
            self.assertEqual(self.to_todo(tid).status_code, 200)
        self.assertEqual(self.listed(), [b, c, a])

    def test_drop_at_position_from_backlog_shifts_others(self):
        a, b, c, d = (self.add()["id"] for _ in range(4))
        for tid in (a, b, c):
            self.to_todo(tid)
        r = self.to_todo(d, 2)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.listed(), [a, d, b, c])

    def test_position_beyond_the_end_means_last(self):
        a, b, c = (self.add()["id"] for _ in range(3))
        self.to_todo(c)
        self.to_todo(a)
        self.assertEqual(self.to_todo(b, 50).status_code, 200)
        self.assertEqual(self.listed(), [c, a, b])

    def test_reorder_within_todo(self):
        a, b, c = (self.add()["id"] for _ in range(3))
        for tid in (a, b, c):
            self.to_todo(tid)
        self.assertEqual(self.to_todo(c, 1).status_code, 200)
        self.assertEqual(self.listed(), [c, a, b])
        self.assertEqual(self.to_todo(c, 3).status_code, 200)
        self.assertEqual(self.listed(), [a, b, c])

    def test_invalid_position_is_400_and_changes_nothing(self):
        a, b = (self.add()["id"] for _ in range(2))
        self.to_todo(a)
        for bad in (0, -3, "2", 1.5, "abc"):
            with self.subTest(position=bad):
                r = self.to_todo(b, bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("error", r.json())
                self.assertEqual(self.get("/api/task", id=b).json()["task"]["status"], "backlog")
                self.assertEqual(self.listed(), [a])

    def test_board_payload_lists_todo_cards_in_manual_order(self):
        low = self.add(priority="low")["id"]
        asap = self.add(priority="asap")["id"]
        for tid in (low, asap):
            self.to_todo(tid)
        tasks = self.get("/api/tasks").json()["tasks"]
        self.assertEqual([t["id"] for t in tasks if t["status"] == "todo"], [low, asap])

    def test_task_leaving_todo_is_gone_from_the_column(self):
        a, b, c = (self.add()["id"] for _ in range(3))
        for tid in (c, a, b):
            self.to_todo(tid)
        self.post("/api/task/edit", {"id": a, "status": "in_progress"})
        self.assertEqual(self.listed(), [c, b])
        self.post("/api/task/delete", {"id": b})
        self.assertEqual(self.listed(), [c])
        self.post("/api/task/edit", {"id": a, "status": "testing"})
        self.to_todo(a)
        self.assertEqual(self.listed(), [c, a])


if __name__ == "__main__":
    unittest.main()
