"""Правила переходів між колонками: store (move_task, edit_task), MCP-тули, веб-API.

Запуск: uv run python -m unittest tests.test_transitions -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase, request  # noqa: E402
from planner_store import PlannerError, PlannerStore  # noqa: E402

ALL = ("backlog", "todo", "blockers", "in_progress", "in_review", "testing", "done")
CHAR = {
    "backlog": " ", "todo": ">", "blockers": "b", "in_progress": "/", "in_review": "r",
    "testing": "t", "done": "x",
}
# Звідки МОЖНА перейти в колонку (з вимог): у backlog і done — звідки завгодно.
# Стор і веб-дашборд приймають і backlog → todo; через MCP він заборонений (test_blockers_api).
SOURCES = {
    "backlog": set(ALL),
    "todo": {"backlog", "testing", "in_progress", "in_review", "blockers"},
    "blockers": {"backlog", "todo", "testing"},
    "in_progress": {"todo"},
    "in_review": {"in_progress"},
    "testing": {"in_progress", "in_review", "blockers"},
    "done": set(ALL),
}


def moved(case: unittest.TestCase, store: PlannerStore, dst: str) -> dict:
    """move_task, де відмова — провал перевірки (а не помилка тесту)."""
    try:
        return store.move_task(TID, dst)
    except PlannerError as e:
        case.fail(f"перехід у {dst} мав бути дозволений: {e}")


PAIRS = [(src, dst) for src in ALL for dst in ALL if src != dst]
ALLOWED = [(s, d) for s, d in PAIRS if s in SOURCES[d]]
FORBIDDEN = [(s, d) for s, d in PAIRS if s not in SOURCES[d]]
TID = "aa0001"


def planner_text(status: str, extra: str = "") -> str:
    return f"# Tasks\n\n- [{CHAR[status]}] Задача  📌[Задача] ^{TID}\n{extra}"


class TransitionMatrixTests(unittest.TestCase):
    """Повна матриця 7×7: кожну пару перевіряє і move_task, і edit_task."""

    def store_with(self, status: str) -> tuple[PlannerStore, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "tasks.md").write_text(planner_text(status), encoding="utf-8")
        return PlannerStore(root), root

    def attempt(self, kind: str, store: PlannerStore, dst: str):
        """Перехід → None, якщо дозволено, інакше текст помилки."""
        try:
            if kind == "move":
                store.move_task(TID, dst)
            else:
                store.edit_task(TID, status=dst)
        except PlannerError as e:
            return str(e)
        return None

    def test_allowed_transitions_move_the_task(self):
        for kind in ("move", "edit"):
            for src, dst in ALLOWED:
                with self.subTest(kind=kind, src=src, dst=dst):
                    store, _ = self.store_with(src)
                    self.assertIsNone(self.attempt(kind, store, dst))
                    self.assertEqual(store.get_task(TID)["status"], dst)

    def test_forbidden_transitions_are_rejected_with_ukrainian_message(self):
        for kind in ("move", "edit"):
            for src, dst in FORBIDDEN:
                with self.subTest(kind=kind, src=src, dst=dst):
                    store, _ = self.store_with(src)
                    err = self.attempt(kind, store, dst)
                    self.assertIsNotNone(err, "перехід мав бути відхилений")
                    self.assertIn(f"Перехід {src} → {dst} заборонено", err)
                    for allowed in SOURCES[dst]:
                        self.assertIn(allowed, err)

    def test_message_names_target_and_single_source(self):
        store, _ = self.store_with("backlog")
        self.assertEqual(
            self.attempt("move", store, "in_progress"),
            "Перехід backlog → in_progress заборонено: в in_progress можна лише з todo",
        )
        self.assertEqual(
            self.attempt("edit", store, "in_review"),
            "Перехід backlog → in_review заборонено: в in_review можна лише з in_progress",
        )

    def test_same_status_is_a_noop_not_an_error(self):
        for kind in ("move", "edit"):
            for status in ALL:
                with self.subTest(kind=kind, status=status):
                    store, root = self.store_with(status)
                    before = (root / "tasks.md").read_text(encoding="utf-8")
                    self.assertIsNone(self.attempt(kind, store, status))
                    self.assertEqual(store.get_task(TID)["status"], status)
                    self.assertEqual((root / "tasks.md").read_text(encoding="utf-8"), before)

    def test_rejected_transition_leaves_file_unchanged_and_writes_no_history(self):
        for kind in ("move", "edit"):
            for src, dst in FORBIDDEN:
                with self.subTest(kind=kind, src=src, dst=dst):
                    store, root = self.store_with(src)
                    store.list_tasks()  # стороннє «запікання» вже відбулось
                    before = (root / "tasks.md").read_bytes()
                    history = store.history.read(limit=100)
                    self.assertIsNotNone(self.attempt(kind, store, dst))
                    self.assertEqual((root / "tasks.md").read_bytes(), before)
                    self.assertEqual(store.history.read(limit=100), history)

    def test_rejected_edit_does_not_apply_other_fields(self):
        store, _ = self.store_with("backlog")
        with self.assertRaises(PlannerError):
            store.edit_task(TID, status="in_progress", text="інший текст", priority="high")
        got = store.get_task(TID)
        self.assertEqual((got["text"], got["priority"], got["status"]), ("Задача", None, "backlog"))

    def test_allowed_transition_writes_history_and_marker(self):
        store, root = self.store_with("in_progress")
        moved(self, store, "in_review")
        self.assertIn("- [r] Задача", (root / "tasks.md").read_text(encoding="utf-8"))
        rec = store.history.read(task_id=TID, limit=1)[0]
        self.assertEqual(rec["changes"]["status"], ["in_progress", "in_review"])

    def test_full_happy_path(self):
        store, _ = self.store_with("backlog")
        path = ("todo", "blockers", "todo", "in_progress", "in_review", "testing", "blockers",
                "testing", "todo", "in_progress", "testing", "done")
        for status in path:
            self.assertIsNone(self.attempt("move", store, status), status)


class WipStillAppliesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def seed(self, in_progress: int) -> PlannerStore:
        lines = [f"- [/] wip-{i} ^bb{i:04d}" for i in range(in_progress)]
        (self.root / "tasks.md").write_text(
            "# Tasks\n" + "\n".join(lines) + f"\n- [>] Наступна ^{TID}\n", encoding="utf-8"
        )
        return PlannerStore(self.root)

    def test_todo_to_in_progress_blocked_at_15(self):
        store = self.seed(15)
        for call in (lambda: store.move_task(TID, "in_progress"),
                     lambda: store.edit_task(TID, status="in_progress")):
            with self.assertRaises(PlannerError) as cm:
                call()
            self.assertIn("WIP-ліміт", str(cm.exception))
            self.assertIn("15", str(cm.exception))
        self.assertEqual(store.get_task(TID)["status"], "todo")

    def test_todo_to_in_progress_allowed_at_14(self):
        store = self.seed(14)
        self.assertEqual(moved(self, store, "in_progress")["status"], "in_progress")
        self.assertEqual(store.count_in_progress(), 15)


class ToolTransitionTests(ServerCase):
    def seed(self, status: str) -> None:
        (self.root / "tasks.md").write_text(planner_text(status), encoding="utf-8")

    def test_move_and_edit_tools_return_the_error(self):
        self.seed("backlog")
        want = "⚠️ Перехід backlog → in_progress заборонено: в in_progress можна лише з todo"
        self.assertEqual(self.call("move_task", id=TID, status="in_progress"), want)
        self.assertEqual(self.call("edit_task", id=TID, status="in_progress"), want)
        self.assertIn("[backlog]", self.call("get_task", id=TID))

    def test_move_tool_walks_the_allowed_path(self):
        self.seed("todo")  # backlog → todo через MCP заборонено (test_blockers_api)
        for status, label in (("in_progress", "in progress"), ("in_review", "in review"),
                              ("testing", "testing"), ("blockers", "blockers"), ("todo", "to do"),
                              ("in_progress", "in progress"), ("done", "done")):
            self.assertIn(f"[{label}]", self.call("move_task", id=TID, status=status))

    def test_edit_tool_allows_testing_back_to_todo(self):
        self.seed("testing")
        self.assertIn("[to do]", self.call("edit_task", id=TID, status="todo"))


class WebTransitionTests(ServerCase):
    ENV = {"TASKS_TOKEN": "offline-bearer-fixture"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from _support import capture_app
        cls.app = capture_app(cls.tm.run_http)

    def edit(self, status: str):
        return request(self.app, "POST", "/api/task/edit", json_body={"id": TID, "status": status})

    def test_every_pair_via_the_edit_endpoint(self):
        for src, dst in PAIRS:
            with self.subTest(src=src, dst=dst):
                (self.root / "tasks.md").write_text(planner_text(src), encoding="utf-8")
                r = self.edit(dst)
                if src in SOURCES[dst]:
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(r.json()["task"]["status"], dst)
                else:
                    self.assertEqual(r.status_code, 400, r.text)
                    self.assertIn(f"Перехід {src} → {dst} заборонено", r.json()["error"])

    def test_rejected_drop_leaves_the_task_where_it_was(self):
        (self.root / "tasks.md").write_text(planner_text("backlog"), encoding="utf-8")
        r = self.edit("testing")
        self.assertEqual(r.status_code, 400)
        got = request(self.app, "GET", "/api/tasks").json()["tasks"]
        self.assertEqual([t["status"] for t in got], ["backlog"])


if __name__ == "__main__":
    unittest.main()
