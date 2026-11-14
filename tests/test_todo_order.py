"""Ручний порядок задач у колонці To Do: стор (`<planner>/todo-order.json`).

Запуск: uv run python -m unittest tests.test_todo_order -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner_store import PlannerError, PlannerStore  # noqa: E402


class TodoOrderCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "tasks.md").write_text("# Tasks\n\n## ✅ Done\n", encoding="utf-8")
        self.store = PlannerStore(self.root)
        self.order_file = self.root / "todo-order.json"

    def backlog(self, name: str, project: str | None = None, **kw) -> str:
        return self.store.add_task(name, title=name, project=project, **kw)["id"]

    def todo(self, name: str, position: int | None = None, project: str | None = None, **kw) -> str:
        tid = self.backlog(name, project, **kw)
        self.store.move_task(tid, "todo", position)
        return tid

    def listed(self) -> list[str]:
        return [t["id"] for t in self.store.list_tasks(status="todo")]

    def raw_order(self) -> str:
        self.assertTrue(self.order_file.exists(), "todo-order.json не створено")
        return self.order_file.read_text(encoding="utf-8")

    def saved(self) -> list[str]:
        return json.loads(self.raw_order())


class EnterTodoFromColumnTests(TodoOrderCase):
    """Куди стає задача, що вертається в To Do з іншої колонки (без явної позиції)."""

    def go(self, tid: str, status: str, position: int | None = None) -> None:
        """move_task, де відмова — провал перевірки (а не помилка тесту)."""
        try:
            self.store.move_task(tid, status, position)
        except PlannerError as e:
            self.fail(f"перехід у {status} мав бути дозволений: {e}")

    def parked(self, via: tuple[str, ...]) -> str:
        """Нова задача, яка пройшла через `via` (у To Do вона вже була першою)."""
        tid = self.todo("X")
        for status in via:
            self.go(tid, status)
        return tid

    def test_from_in_progress_goes_to_the_top(self):
        a, b = self.todo("A"), self.todo("B")
        x = self.parked(("in_progress",))
        self.go(x, "todo")
        self.assertEqual(self.listed(), [x, a, b])
        self.assertEqual(self.saved(), [x, a, b])

    def test_from_in_review_goes_to_the_top(self):
        a, b = self.todo("A"), self.todo("B")
        x = self.parked(("in_progress", "in_review"))
        self.go(x, "todo")
        self.assertEqual(self.listed(), [x, a, b])

    def test_edit_task_status_from_in_progress_goes_to_the_top(self):
        a = self.todo("A")
        x = self.parked(("in_progress",))
        try:
            self.store.edit_task(x, status="todo")
        except PlannerError as e:
            self.fail(f"перехід у todo мав бути дозволений: {e}")
        self.assertEqual(self.listed(), [x, a])

    def test_from_testing_goes_last(self):
        a, b = self.todo("A"), self.todo("B")
        x = self.parked(("in_progress", "testing"))
        self.go(x, "todo")
        self.assertEqual(self.listed(), [a, b, x])

    def test_from_blockers_goes_last(self):
        a, b = self.todo("A"), self.todo("B")
        x = self.parked(("blockers",))
        self.go(x, "todo")
        self.assertEqual(self.listed(), [a, b, x])

    def test_from_backlog_goes_last(self):
        a, b = self.todo("A"), self.todo("B")
        x = self.backlog("X")
        self.go(x, "todo")
        self.assertEqual(self.listed(), [a, b, x])

    def test_explicit_position_wins_over_the_top_rule(self):
        a, b = self.todo("A"), self.todo("B")
        x = self.parked(("in_progress",))
        self.go(x, "todo", 2)
        self.assertEqual(self.listed(), [a, x, b])

    def test_leaving_to_blockers_removes_the_id_from_the_order(self):
        a, b = self.todo("A"), self.todo("B")
        self.go(a, "blockers")
        self.assertEqual(self.listed(), [b])
        self.assertEqual(self.saved(), [b])


class EnterTodoTests(TodoOrderCase):
    def test_move_into_todo_without_position_goes_last(self):
        a, b, c = (self.backlog(n) for n in "ABC")
        for tid in (b, a, c):                      # порядок входу, не порядок створення
            self.store.move_task(tid, "todo")
        self.assertEqual(self.listed(), [b, a, c])
        self.assertEqual(self.saved(), [b, a, c])

    def test_order_spans_project_files(self):
        a = self.todo("A", project="home")
        b = self.todo("B")
        c = self.todo("C", project="work")
        self.assertEqual(self.listed(), [a, b, c])

    def test_drop_at_position_makes_task_nth_and_shifts_others(self):
        a, b, c = (self.todo(n) for n in "ABC")
        d = self.todo("D", position=2)
        self.assertEqual(self.listed(), [a, d, b, c])
        e = self.todo("E", position=1)
        self.assertEqual(self.listed(), [e, a, d, b, c])
        self.assertEqual(self.saved(), [e, a, d, b, c])

    def test_position_works_through_edit_task(self):
        a, b = self.todo("A"), self.todo("B")
        c = self.backlog("C")
        self.store.edit_task(c, status="todo", position=2)
        self.assertEqual(self.listed(), [a, c, b])

    def test_position_larger_than_list_means_last(self):
        a0, b0 = self.backlog("A"), self.backlog("B")
        self.store.move_task(b0, "todo")
        self.store.move_task(a0, "todo")
        c = self.todo("C", position=99)
        self.assertEqual(self.listed(), [b0, a0, c])

    def test_position_into_empty_todo(self):
        a = self.todo("A", position=5)
        self.assertEqual(self.saved(), [a])


class InvalidPositionTests(TodoOrderCase):
    def test_position_below_one_or_not_integer_is_error_and_writes_nothing(self):
        a, b = self.todo("A"), self.todo("B")
        c = self.backlog("C")
        tasks_before = (self.root / "tasks.md").read_text(encoding="utf-8")
        order_before = self.raw_order()
        for bad in (0, -1, "2", 1.5, True):
            for kind in ("move", "edit"):
                with self.subTest(position=bad, kind=kind):
                    with self.assertRaises(PlannerError):
                        if kind == "move":
                            self.store.move_task(c, "todo", bad)
                        else:
                            self.store.edit_task(c, status="todo", position=bad)
                    self.assertEqual(self.store.get_task(c)["status"], "backlog")
                    self.assertEqual((self.root / "tasks.md").read_text(encoding="utf-8"), tasks_before)
                    self.assertEqual(self.raw_order(), order_before)
        self.assertEqual(self.listed(), [a, b])


class ReorderTests(TodoOrderCase):
    def test_reorder_within_todo_via_move_task(self):
        a, b, c = (self.todo(n) for n in "ABC")
        self.store.move_task(c, "todo", 1)
        self.assertEqual(self.listed(), [c, a, b])
        self.store.move_task(c, "todo", 3)
        self.assertEqual(self.listed(), [a, b, c])
        self.assertEqual(self.saved(), [a, b, c])

    def test_reorder_within_todo_via_edit_task(self):
        a, b, c = (self.todo(n) for n in "ABC")
        self.store.edit_task(a, status="todo", position=2)
        self.assertEqual(self.listed(), [b, a, c])
        self.store.edit_task(c, position=1)        # статус не передано — задача вже в To Do
        self.assertEqual(self.listed(), [c, b, a])

    def test_same_position_changes_nothing(self):
        a0, b0, c0 = (self.backlog(n) for n in "ABC")
        for tid in (b0, a0, c0):                   # порядок входу B, A, C
            self.store.move_task(tid, "todo")
        self.store.move_task(a0, "todo", 2)
        self.assertEqual(self.listed(), [b0, a0, c0])


class LeaveTodoTests(TodoOrderCase):
    def test_task_leaving_todo_disappears_from_order(self):
        a, b, c = (self.todo(n) for n in "ABC")
        self.store.move_task(b, "in_progress")
        self.assertEqual(self.saved(), [a, c])
        self.store.move_task(b, "in_review")
        self.store.move_task(b, "testing")
        self.store.move_task(b, "todo")            # повернулась → знову в кінець
        self.assertEqual(self.listed(), [a, c, b])
        self.assertEqual(self.saved(), [a, c, b])

    def test_task_moved_to_backlog_or_done_disappears_from_order(self):
        a, b, c = (self.todo(n) for n in "ABC")
        self.store.move_task(a, "backlog")
        self.store.edit_task(c, status="done")
        self.assertEqual(self.saved(), [b])

    def test_deleted_task_disappears_from_order(self):
        a, b = self.todo("A"), self.todo("B")
        self.store.delete_task(a)
        self.assertEqual(self.saved(), [b])
        self.assertEqual(self.listed(), [b])


class MissingFromOrderTests(TodoOrderCase):
    OBSIDIAN = "- [>] Із Obsidian  📌[Із Obsidian] ^ab0001\n"

    def test_todo_task_missing_from_order_goes_last_and_is_not_lost(self):
        a, b = self.todo("A"), self.todo("B")
        (self.root / "projects").mkdir(exist_ok=True)
        (self.root / "projects" / "home.md").write_text("# Home\n\n" + self.OBSIDIAN, encoding="utf-8")
        self.assertEqual(self.listed(), [a, b, "ab0001"])
        c = self.todo("C")                         # наступна зміна порядку теж її не губить
        self.assertEqual(self.listed(), [a, b, "ab0001", c])
        self.assertEqual(self.saved(), [a, b, "ab0001", c])

    def test_missing_order_file_loses_no_todo_task(self):
        a, b = self.todo("A"), self.todo("B")
        self.raw_order()
        self.order_file.unlink()
        self.assertCountEqual(self.listed(), [a, b])
        c = self.todo("C")
        self.assertEqual(self.listed()[-1], c)
        self.assertCountEqual(self.listed(), [a, b, c])

    def test_ids_of_tasks_no_longer_in_todo_are_ignored(self):
        a, b = self.todo("A"), self.todo("B")
        self.order_file.write_text(json.dumps(["gone01", b, a, "gone02"]), encoding="utf-8")
        self.assertEqual(self.listed(), [b, a])


class ListOrderTests(TodoOrderCase):
    def test_list_todo_follows_manual_order_not_priority(self):
        low = self.todo("low", priority="low")
        none = self.todo("none")
        asap = self.todo("asap", priority="asap", deadline="2026-01-01")
        high = self.todo("high", priority="high")
        self.assertEqual(self.listed(), [low, none, asap, high])
        self.assertEqual(
            [t["id"] for t in self.store.list_tasks(status="todo", priority="high")], [high]
        )

    def test_other_columns_still_sorted_by_priority(self):
        low = self.backlog("low", priority="low")
        asap = self.backlog("asap", priority="asap")
        self.assertEqual([t["id"] for t in self.store.list_tasks(status="backlog")], [asap, low])

    def test_todo_inside_unfiltered_list_keeps_manual_relative_order(self):
        low = self.todo("low", priority="low")
        asap = self.todo("asap", priority="asap")
        todo_ids = [t["id"] for t in self.store.list_tasks() if t["status"] == "todo"]
        self.assertEqual(todo_ids, [low, asap])


if __name__ == "__main__":
    unittest.main()
