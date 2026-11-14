"""Колонка blockers і нові переходи крізь MCP-тули та веб-API дашборда.

backlog → todo дозволений лише з дашборда (веб-API); через MCP (`move_task` і зміна статусу
в `edit_task`) — помилка. Решта переходів в обох інтерфейсах однакова.

Запуск: uv run python -m unittest tests.test_blockers_api -v
"""
from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase  # noqa: E402
from test_transitions import ALL, CHAR, SOURCES, TID  # noqa: E402
from test_web_api import WebCase  # noqa: E402

PAIRS = [(src, dst) for src in ALL for dst in ALL if src != dst]
MCP_ONLY_FORBIDDEN = ("backlog", "todo")  # заборонений лише через MCP, дашборд його дозволяє
LABEL = {"in_progress": "in progress", "in_review": "in review", "todo": "to do"}


def seed(case: ServerCase, status: str) -> None:
    (case.root / "tasks.md").write_text(
        f"# Tasks\n\n- [{CHAR[status]}] Задача  📌[Задача] ^{TID}\n", encoding="utf-8"
    )
    (case.root / "todo-order.json").unlink(missing_ok=True)


class McpTransitionTests(ServerCase):
    def attempt(self, kind: str, dst: str) -> str:
        if kind == "move":
            return self.call("move_task", id=TID, status=dst)
        return self.call("edit_task", id=TID, status=dst)

    def test_every_pair_over_mcp_except_backlog_to_todo(self):
        for kind in ("move", "edit"):
            for src, dst in PAIRS:
                with self.subTest(kind=kind, src=src, dst=dst):
                    seed(self, src)
                    out = self.attempt(kind, dst)
                    if src in SOURCES[dst] and (src, dst) != MCP_ONLY_FORBIDDEN:
                        self.assertTrue(out.startswith("✅"), out)
                        self.assertIn(f"[{LABEL.get(dst, dst)}]", out)
                    else:
                        self.assertTrue(out.startswith("⚠️"), out)
                        self.assertIn(f"Перехід {src} → {dst} заборонено", out)

    def test_move_task_backlog_to_todo_is_an_error_and_task_stays(self):
        seed(self, "backlog")
        out = self.call("move_task", id=TID, status="todo")
        self.assertTrue(out.startswith("⚠️"), out)
        self.assertIn("[backlog]", self.call("get_task", id=TID))

    def test_edit_task_status_backlog_to_todo_is_an_error_and_applies_nothing(self):
        seed(self, "backlog")
        out = self.call("edit_task", id=TID, status="todo", text="інший текст", priority="high")
        self.assertTrue(out.startswith("⚠️"), out)
        got = self.call("get_task", id=TID)
        self.assertIn("[backlog]", got)
        self.assertNotIn("інший текст", got)

    def test_edit_task_without_status_change_still_works_in_backlog(self):
        seed(self, "backlog")
        self.assertTrue(self.call("edit_task", id=TID, priority="high").startswith("✅"))


class McpTodoPositionTests(ServerCase):
    def setUp(self):
        super().setUp()
        (self.root / "todo-order.json").unlink(missing_ok=True)

    def listed(self) -> list[str]:
        return re.findall(r"^`(\w+)`", self.call("list_tasks", status="todo"), flags=re.M)

    def test_from_in_progress_over_mcp_goes_to_the_top(self):
        a, b, x = (self.add(text=n, title=n) for n in "ABX")
        for tid in (a, b, x):
            self.to_todo(tid)
        self.call("move_task", id=x, status="in_progress")
        self.call("move_task", id=x, status="todo")
        self.assertEqual(self.listed(), [x, a, b])

    def test_from_in_review_over_mcp_goes_to_the_top(self):
        a, x = (self.add(text=n, title=n) for n in "AX")
        for tid in (a, x):
            self.to_todo(tid)
        for status in ("in_progress", "in_review", "todo"):
            self.call("edit_task", id=x, status=status)
        self.assertEqual(self.listed(), [x, a])

    def test_from_blockers_and_testing_over_mcp_go_last(self):
        a, b, c = (self.add(text=n, title=n) for n in "ABC")
        for tid in (a, b, c):
            self.to_todo(tid)
        self.call("move_task", id=b, status="blockers")
        self.call("move_task", id=b, status="todo")
        self.call("move_task", id=a, status="in_progress")
        self.call("move_task", id=a, status="testing")
        self.call("move_task", id=a, status="todo")
        self.assertEqual(self.listed(), [c, b, a])


class McpBoardAndDocsTests(ServerCase):
    def test_board_counts_blockers(self):
        a, b = self.add(title="Чекає"), self.add(title="Теж чекає")
        self.add(title="Колись")
        for tid in (a, b):
            self.call("move_task", id=tid, status="blockers")
        self.assertEqual(
            self.call("board").splitlines()[0],
            "📋 backlog: 1 · to do: 0 · blockers: 2 · in progress: 0/15 · in review: 0 · "
            "testing: 0 · done: 0 · усього: 3",
        )

    def test_list_tasks_filters_by_blockers(self):
        a = self.add(title="Чекає")
        self.add(title="Колись")
        self.call("move_task", id=a, status="blockers")
        self.assertEqual(self.call("list_tasks", status="blockers"), f"`{a}` [blockers] Чекає · @global")

    def test_instructions_and_tool_docs_describe_blockers_and_new_transitions(self):
        ins = self.tm.INSTRUCTIONS
        self.assertIn("backlog | todo | blockers | in_progress | in_review | testing | done", ins)
        self.assertIn("into blockers only from backlog, todo or testing", ins)
        self.assertIn("into testing only from in_progress, in_review or blockers", ins)
        self.assertIn("into todo only from testing, in_progress, in_review or blockers", ins)
        self.assertIn("backlog to todo is not possible via MCP", ins)
        tools = {t.name: " ".join(t.description.split()) for t in asyncio.run(self.tm.mcp.list_tools())}
        for name in ("move_task", "edit_task"):
            with self.subTest(tool=name):
                self.assertIn("blockers", tools[name])
                self.assertIn("backlog → todo", tools[name])


class WebBlockersTests(WebCase):
    def setUp(self):
        super().setUp()
        (self.root / "todo-order.json").unlink(missing_ok=True)

    def edit(self, tid: str, status: str, **extra):
        return self.post("/api/task/edit", {"id": tid, "status": status, **extra})

    def listed(self) -> list[str]:
        return [t["id"] for t in self.get("/api/tasks", status="todo").json()["tasks"]]

    def test_dashboard_backlog_to_todo_is_allowed(self):
        tid = self.add()["id"]
        r = self.edit(tid, "todo")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["status"], "todo")

    def test_dashboard_moves_through_blockers(self):
        tid = self.add()["id"]
        for status in ("blockers", "todo", "blockers", "testing"):
            r = self.edit(tid, status)
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.get("/api/tasks", status="blockers").json()["tasks"], [])

    def test_dashboard_in_progress_to_todo_goes_to_the_top(self):
        a, b, x = (self.add()["id"] for _ in range(3))
        for tid in (a, b, x):
            self.edit(tid, "todo")
        self.edit(x, "in_progress")
        self.assertEqual(self.edit(x, "todo").status_code, 200)
        self.assertEqual(self.listed(), [x, a, b])

    def test_dashboard_drop_with_position_beats_the_top_rule(self):
        a, b, x = (self.add()["id"] for _ in range(3))
        for tid in (a, b, x):
            self.edit(tid, "todo")
        self.edit(x, "in_progress")
        self.assertEqual(self.edit(x, "todo", position=3).status_code, 200)
        self.assertEqual(self.listed(), [a, b, x])

    def test_dashboard_blockers_to_todo_goes_last(self):
        a, b = (self.add()["id"] for _ in range(2))
        self.edit(a, "todo")
        self.assertEqual(self.edit(b, "blockers").status_code, 200)
        self.assertEqual(self.edit(b, "todo").status_code, 200)
        self.assertEqual(self.listed(), [a, b])

    def test_forbidden_drop_into_blockers_is_400(self):
        tid = self.add()["id"]
        self.edit(tid, "todo")
        self.edit(tid, "in_progress")
        r = self.edit(tid, "blockers")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("Перехід in_progress → blockers заборонено", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
