"""Поле `plan` задачі на рівні стору: окремий файл `plans/<id>.md`, не рядок задачі.

Запуск: uv run python -m unittest tests.test_plan_store -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner_store import PlannerError, PlannerStore  # noqa: E402

# Багаторядковий markdown зі списком-чекбоксами, порожніми рядками, хвостовими пробілами,
# табуляцією й секцією «Фідбек», без фінального переводу рядка.
PLAN = (
    "# План\n\n"
    "- [ ] перший крок  \n"
    "- [x] другий крок\n"
    "\tвідступ табом\n\n"
    "## Фідбек\n"
    "Колега: ок, але 📅 2026-12-01 не встигаємо"
)


class PlanStoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "projects").mkdir()
        (self.root / "tasks.md").write_text("# Tasks\n\n## 🔥 Активні\n\n## ✅ Done\n", encoding="utf-8")
        self.s = PlannerStore(self.root)

    def plan_file(self, tid: str) -> Path:
        return self.root / "plans" / f"{tid}.md"

    def plan_bytes(self, tid: str) -> bytes | None:
        p = self.plan_file(tid)
        return p.read_bytes() if p.exists() else None


class SaveTests(PlanStoreCase):
    def test_multiline_plan_saved_byte_for_byte(self):
        tid = self.s.add_task("Задача", title="Задача")["id"]
        self.s.edit_task(tid, plan=PLAN)
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))
        self.assertEqual(self.s.get_task(tid).get("plan"), PLAN)

    def test_crlf_in_plan_kept(self):
        tid = self.s.add_task("Задача", title="Задача")["id"]
        self.s.edit_task(tid, plan="a\r\nb\r\n")
        self.assertEqual(self.plan_bytes(tid), b"a\r\nb\r\n")
        self.assertEqual(self.s.get_task(tid).get("plan"), "a\r\nb\r\n")

    def test_plan_leaves_id_and_task_line_unchanged(self):
        tid = self.s.add_task("Задача", title="Задача", priority="high")["id"]
        before = (self.root / "tasks.md").read_text(encoding="utf-8")
        self.s.edit_task(tid, plan=PLAN)
        self.assertEqual((self.root / "tasks.md").read_text(encoding="utf-8"), before)
        self.assertEqual([t["id"] for t in self.s.list_tasks()], [tid])
        self.assertEqual(self.s.get_task(tid)["text"], "Задача")
        self.assertEqual(self.s.get_task(tid).get("plan"), PLAN)

    def test_plan_survives_other_edits_and_project_move(self):
        tid = self.s.add_task("Задача", title="Задача", plan=PLAN)["id"]
        self.s.edit_task(tid, priority="low", project="home")
        self.assertEqual(self.s.get_task(tid).get("plan"), PLAN)

    def test_add_task_with_plan_saves_it(self):
        t = self.s.add_task("Задача", title="Задача", plan=PLAN)
        self.assertEqual(self.plan_bytes(t["id"]), PLAN.encode("utf-8"))
        self.assertEqual(self.s.get_task(t["id"]).get("plan"), PLAN)

    def test_add_task_without_plan_has_no_plan_field(self):
        t = self.s.add_task("Задача", title="Задача")
        self.assertNotIn("plan", self.s.get_task(t["id"]))
        self.assertFalse(self.plan_file(t["id"]).exists())


class NotScannedTests(PlanStoreCase):
    PLAN = "- [ ] не задача ^abc123\n- [/] теж не задача"

    def test_checkbox_lines_in_plan_are_not_tasks(self):
        tid = self.s.add_task("Задача", title="Задача")["id"]
        self.s.edit_task(tid, plan=self.PLAN)
        self.assertEqual([t["id"] for t in self.s.list_tasks()], [tid])
        self.assertEqual(self.s.list_tasks(query="не задача"), [])
        self.assertEqual(self.s.get_task(tid).get("plan"), self.PLAN)

    def test_plans_dir_is_not_scanned_as_task_files(self):
        (self.root / "plans").mkdir()
        stray = "- [ ] підкинута  ^zz9999\n"
        (self.root / "plans" / "zz9999.md").write_text(stray, encoding="utf-8")
        (self.root / "plans" / "tasks.md").write_text("- [ ] ще одна  ^yy8888\n", encoding="utf-8")
        self.s.add_task("Задача", title="Задача")
        self.assertEqual([t["text"] for t in self.s.list_tasks()], ["Задача"])
        self.assertEqual(self.s._managed_files(), ["tasks.md"])
        self.assertEqual((self.root / "plans" / "zz9999.md").read_text(encoding="utf-8"), stray)


class ClearTests(PlanStoreCase):
    def _with_plan(self) -> str:
        tid = self.s.add_task("Задача", title="Задача", plan=PLAN)["id"]
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"), "передумова: план збережено")
        return tid

    def test_empty_plan_removes_it(self):
        tid = self._with_plan()
        self.s.edit_task(tid, plan="")
        self.assertFalse(self.plan_file(tid).exists())
        self.assertNotIn("plan", self.s.get_task(tid))

    def test_none_plan_removes_it(self):
        tid = self._with_plan()
        self.s.edit_task(tid, plan="none")
        self.assertFalse(self.plan_file(tid).exists())
        self.assertNotIn("plan", self.s.get_task(tid))

    def test_python_none_plan_removes_it(self):
        tid = self._with_plan()
        self.s.edit_task(tid, plan=None)
        self.assertNotIn("plan", self.s.get_task(tid))

    def test_whitespace_only_plan_removes_it(self):
        tid = self._with_plan()
        self.s.edit_task(tid, plan=" \n\t\n")
        self.assertNotIn("plan", self.s.get_task(tid))

    def test_clearing_absent_plan_is_not_an_error(self):
        tid = self.s.add_task("Задача", title="Задача")["id"]
        self.s.edit_task(tid, plan="none")
        self.assertNotIn("plan", self.s.get_task(tid))

    def test_omitted_plan_is_left_alone(self):
        tid = self._with_plan()
        self.s.edit_task(tid, priority="low")
        self.assertEqual(self.s.get_task(tid).get("plan"), PLAN)

    def test_delete_task_removes_plan_file(self):
        tid = self._with_plan()
        self.assertTrue(self.plan_file(tid).exists())
        self.s.delete_task(tid)
        self.assertFalse(self.plan_file(tid).exists())


class OneLineTextTests(PlanStoreCase):
    def test_add_task_text_with_newline_rejected(self):
        with self.assertRaises(PlannerError):
            self.s.add_task("перший рядок\nдругий рядок", title="Задача")
        self.assertEqual(self.s.list_tasks(), [])

    def test_edit_task_text_with_newline_rejected(self):
        tid = self.s.add_task("Задача", title="Задача")["id"]
        with self.assertRaises(PlannerError):
            self.s.edit_task(tid, text="перший\nдругий")
        self.assertEqual(self.s.get_task(tid)["text"], "Задача")

    def test_edit_task_text_with_carriage_return_rejected(self):
        tid = self.s.add_task("Задача", title="Задача")["id"]
        with self.assertRaises(PlannerError):
            self.s.edit_task(tid, text="перший\r\nдругий")

    def test_newline_in_plan_is_fine(self):
        tid = self.s.add_task("Задача", title="Задача", plan="a\nb")["id"]
        self.assertEqual(self.s.get_task(tid).get("plan"), "a\nb")


class HistoryTests(PlanStoreCase):
    def test_plan_added_by_edit_is_recorded(self):
        tid = self.s.add_task("Задача", title="Задача")["id"]
        self.s.edit_task(tid, plan="v1")
        rec = self.s.history.read(tid, 1)[0]
        self.assertEqual((rec["op"], rec["tool"]), ("edit", "edit_task"))
        self.assertEqual(rec["changes"], {"plan": [None, "v1"]})

    def test_plan_changed_is_recorded_was_to_now(self):
        tid = self.s.add_task("Задача", title="Задача", plan="v1")["id"]
        self.s.edit_task(tid, plan="v2")
        self.assertEqual(self.s.history.read(tid, 1)[0]["changes"], {"plan": ["v1", "v2"]})

    def test_plan_cleared_is_recorded(self):
        tid = self.s.add_task("Задача", title="Задача", plan="v1")["id"]
        self.s.edit_task(tid, plan="none")
        self.assertEqual(self.s.history.read(tid, 1)[0]["changes"], {"plan": ["v1", None]})

    def test_same_plan_again_is_not_recorded(self):
        tid = self.s.add_task("Задача", title="Задача", plan="v1")["id"]
        self.assertEqual(self.plan_bytes(tid), b"v1")
        self.s.edit_task(tid, plan="v1")
        self.assertEqual([r["op"] for r in self.s.history.read(tid, 10)], ["add"])

    def test_add_with_plan_is_recorded(self):
        tid = self.s.add_task("Задача", title="Задача", plan="v1")["id"]
        self.assertEqual(self.s.history.read(tid, 1)[0]["changes"].get("plan"), [None, "v1"])

    def test_delete_snapshot_keeps_plan(self):
        tid = self.s.add_task("Задача", title="Задача", plan="v1")["id"]
        self.s.delete_task(tid)
        self.assertEqual(self.s.history.read(tid, 1)[0]["snapshot"].get("plan"), "v1")

    def test_plan_write_is_not_an_external_edit(self):
        tid = self.s.add_task("Задача", title="Задача")["id"]
        self.s.edit_task(tid, plan="v1")
        self.assertEqual(self.plan_bytes(tid), b"v1")
        self.s.list_tasks()
        self.assertNotIn("external", [r["op"] for r in self.s.history.read(None, 20)])


if __name__ == "__main__":
    unittest.main()
