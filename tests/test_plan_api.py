"""Поле `plan` через MCP-тули і веб-API дашборда; `text` — лише один рядок.

Запуск: uv run python -m unittest tests.test_plan_api -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase  # noqa: E402
from test_web_api import WebCase  # noqa: E402

PLAN = "# План\n\n- [ ] крок перший\n- [x] крок другий\n\n## Фідбек\nКолега: ок"
PLAN_MARK = "📝 План"


class ToolsCase(ServerCase):
    def plan_file(self, tid: str) -> Path:
        return self.root / "plans" / f"{tid}.md"

    def plan_bytes(self, tid: str) -> bytes | None:
        p = self.plan_file(tid)
        return p.read_bytes() if p.exists() else None


class ToolPlanTests(ToolsCase):
    def test_get_task_shows_plan(self):
        tid = self.add()
        self.call("edit_task", id=tid, plan=PLAN)
        out = self.call("get_task", id=tid)
        self.assertIn(PLAN_MARK, out)
        self.assertIn(PLAN, out)

    def test_edit_task_saves_plan_byte_for_byte(self):
        tid = self.add()
        self.assertTrue(self.call("edit_task", id=tid, plan=PLAN).startswith("✅"))
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))

    def test_edit_task_plan_keeps_task_line_and_id(self):
        tid = self.add(priority="high")
        before = (self.root / "tasks.md").read_text(encoding="utf-8")
        self.call("edit_task", id=tid, plan=PLAN)
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))
        self.assertEqual((self.root / "tasks.md").read_text(encoding="utf-8"), before)
        self.assertIn(f"`{tid}`", self.call("list_tasks"))

    def test_checkbox_lines_in_plan_are_not_listed_as_tasks(self):
        tid = self.add()
        self.call("edit_task", id=tid, plan=PLAN)
        self.assertIn(PLAN, self.call("get_task", id=tid))
        listing = self.call("list_tasks")
        self.assertNotIn("крок перший", listing)
        self.assertEqual(listing.count("`"), 2, listing)

    def test_none_plan_removes_it(self):
        tid = self.add(plan=PLAN)
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))
        self.call("edit_task", id=tid, plan="none")
        self.assertFalse(self.plan_file(tid).exists())
        self.assertNotIn(PLAN_MARK, self.call("get_task", id=tid))

    def test_task_without_plan_shows_no_plan_field(self):
        tid = self.add()
        self.assertNotIn(PLAN_MARK, self.call("get_task", id=tid))

    def test_edit_without_plan_param_keeps_plan(self):
        tid = self.add(plan=PLAN)
        self.call("edit_task", id=tid, priority="low")
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))

    def test_add_task_with_plan_saves_plan(self):
        tid = self.add(plan=PLAN)
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))
        self.assertIn(PLAN, self.call("get_task", id=tid))

    def test_delete_task_removes_plan_file(self):
        tid = self.add(plan=PLAN)
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))
        self.call("delete_task", id=tid)
        self.assertFalse(self.plan_file(tid).exists())

    def test_plan_change_shows_in_history(self):
        tid = self.add()
        self.call("edit_task", id=tid, plan="v1")
        self.call("edit_task", id=tid, plan="v2")
        out = self.call("history", id=tid)
        self.assertIn("plan: v1 → v2", out)
        self.assertIn("plan: — → v1", out)


class ToolOneLineTextTests(ToolsCase):
    def test_add_task_text_with_newline_is_error(self):
        out = self.call("add_task", text="рядок 1\nрядок 2", title="Т")
        self.assertTrue(out.startswith("⚠️"), out)
        self.assertEqual(self.call("list_tasks"), "Порожньо (за цими фільтрами задач немає).")

    def test_edit_task_text_with_newline_is_error(self):
        tid = self.add()
        out = self.call("edit_task", id=tid, text="рядок 1\nрядок 2")
        self.assertTrue(out.startswith("⚠️"), out)
        self.assertIn("Задача", self.call("get_task", id=tid))
        self.assertNotIn("рядок", self.call("get_task", id=tid))


class WebPlanTests(WebCase):
    def get_task(self, tid):
        return self.get("/api/task", id=tid)

    def plan_bytes(self, tid):
        p = self.root / "plans" / f"{tid}.md"
        return p.read_bytes() if p.exists() else None

    def test_load_task_returns_plan(self):
        tid = self.add(plan=PLAN)["id"]
        r = self.get_task(tid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["plan"], PLAN)
        self.assertEqual(r.json()["task"]["id"], tid)

    def test_load_task_without_plan_has_no_plan_field(self):
        tid = self.add()["id"]
        r = self.get_task(tid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("plan", r.json()["task"])

    def test_load_unknown_task_is_404(self):
        r = self.get_task("zzzzzz")
        self.assertEqual(r.status_code, 404)
        self.assertIn("zzzzzz", r.text)

    def test_add_with_plan_saves_it(self):
        tid = self.add(plan=PLAN)["id"]
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))

    def test_edit_sets_plan_byte_for_byte(self):
        tid = self.add()["id"]
        r = self.post("/api/task/edit", {"id": tid, "plan": PLAN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))

    def test_edit_none_plan_removes_it(self):
        tid = self.add(plan=PLAN)["id"]
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))
        r = self.post("/api/task/edit", {"id": tid, "plan": "none"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.plan_bytes(tid))
        self.assertNotIn("plan", self.get_task(tid).json()["task"])

    def test_edit_without_plan_key_keeps_plan(self):
        tid = self.add(plan=PLAN)["id"]
        self.post("/api/task/edit", {"id": tid, "priority": "low"})
        self.assertEqual(self.plan_bytes(tid), PLAN.encode("utf-8"))

    def test_board_list_does_not_carry_plans(self):
        self.add(plan=PLAN)
        self.assertEqual(self.get("/api/tasks").status_code, 200)
        self.assertTrue(all("plan" not in t for t in self.get("/api/tasks").json()["tasks"]))

    def test_plan_change_recorded_in_history_as_web(self):
        tid = self.add()["id"]
        self.post("/api/task/edit", {"id": tid, "plan": "v1"})
        rec = self.get("/api/history", id=tid).json()["records"][0]
        self.assertEqual(rec["changes"], {"plan": [None, "v1"]})
        self.assertEqual(rec["client"], "web")

    def test_add_text_with_newline_is_400(self):
        r = self.post("/api/task/add", {"text": "а\nб", "title": "Т"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())
        self.assertEqual(self.get("/api/tasks").json()["tasks"], [])

    def test_edit_text_with_newline_is_400(self):
        tid = self.add()["id"]
        r = self.post("/api/task/edit", {"id": tid, "text": "а\nб"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.get_task(tid).json()["task"]["text"], "Задача")

    def test_dashboard_dialog_has_multiline_plan_field(self):
        page = self.get("/task-manager").text
        self.assertIn('<textarea id="f_plan"', page)
        self.assertIn("api/task?id=", page)


if __name__ == "__main__":
    unittest.main()
