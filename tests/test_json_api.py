"""list_tasks / get_task з format="json": машинний вивід для адаптера Elf v2.

Без format — той самий текст, що й раніше (Claude/ChatGPT-конектори не змінюються).

Запуск: uv run python -m unittest tests.test_json_api -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase  # noqa: E402

TASK_KEYS = {"id", "title", "text", "status", "priority", "deadline", "energy",
             "domains", "project", "links"}


class JsonCase(ServerCase):
    def js(self, tool: str, **args):
        return self.parse(tool, self.call(tool, format="json", **args))

    def parse(self, tool: str, out: str):
        try:
            return json.loads(out)
        except ValueError:
            self.fail(f"{tool} не повернув JSON: {out!r}")


class ListJsonTests(JsonCase):
    def setUp(self):
        super().setUp()
        self.a = self.add(text="Full wording A", title="Task A", priority="high", deadline="2026-10-01",
                          energy="M", domains="alpha beta", links="projects/demo/overview")
        self.b = self.add(text="Full wording B", title="Task B", project="demo")
        self.c = self.add(text="Full wording C", title="Task C", priority="low")
        self.to_todo(self.c)
        self.d = self.add(text="Full wording D", title="Task D")
        self.call("move_task", id=self.d, status="done")

    def by_id(self, data) -> dict:
        return {t["id"]: t for t in data["tasks"]}

    def test_task_fields(self):
        t = self.by_id(self.js("list_tasks"))[self.a]
        self.assertEqual(t, {
            "id": self.a, "title": "Task A", "text": "Full wording A", "status": "backlog",
            "priority": "high", "deadline": "2026-10-01", "energy": "M",
            "domains": ["alpha", "beta"], "project": "global", "links": ["projects/demo/overview"],
        })

    def test_empty_optional_fields_are_null_or_empty_lists(self):
        t = self.by_id(self.js("list_tasks"))[self.b]
        self.assertEqual(
            (t["priority"], t["deadline"], t["energy"], t["domains"], t["links"]),
            (None, None, None, [], []),
        )

    def test_project_is_slug_or_global(self):
        tasks = self.by_id(self.js("list_tasks"))
        self.assertEqual((tasks[self.b]["project"], tasks[self.a]["project"]), ("demo", "global"))

    def test_top_level_keys_and_counters(self):
        data = self.js("list_tasks")
        self.assertEqual(set(data), {"tasks", "more", "hidden_done"})
        self.assertEqual(set(data["tasks"][0]), TASK_KEYS)
        self.assertEqual((data["more"], data["hidden_done"]), (0, 1))

    def test_done_hidden_and_counted(self):
        data = self.js("list_tasks")
        self.assertEqual(sorted(t["id"] for t in data["tasks"]), sorted([self.a, self.b, self.c]))
        self.assertEqual(data["hidden_done"], 1)

    def test_status_done_shows_done_tasks_and_hides_nothing(self):
        data = self.js("list_tasks", status="done")
        self.assertEqual([t["id"] for t in data["tasks"]], [self.d])
        self.assertEqual(data["hidden_done"], 0)

    def test_status_filter(self):
        data = self.js("list_tasks", status="todo")
        self.assertEqual([(t["id"], t["status"]) for t in data["tasks"]], [(self.c, "todo")])

    def test_other_filters_apply(self):
        self.assertEqual([t["id"] for t in self.js("list_tasks", project="demo")["tasks"]], [self.b])
        self.assertEqual([t["id"] for t in self.js("list_tasks", domain="alpha")["tasks"]], [self.a])
        self.assertEqual([t["id"] for t in self.js("list_tasks", priority="low")["tasks"]], [self.c])
        self.assertEqual([t["id"] for t in self.js("list_tasks", query="wording b")["tasks"]], [self.b])

    def test_limit_cuts_list_and_reports_more(self):
        data = self.js("list_tasks", limit=2)
        self.assertEqual((len(data["tasks"]), data["more"]), (2, 1))

    def test_same_order_as_text_output(self):
        text_ids = [ln.split("`")[1] for ln in self.call("list_tasks").splitlines() if ln.startswith("`")]
        self.assertEqual([t["id"] for t in self.js("list_tasks")["tasks"]], text_ids)

    def test_empty_result(self):
        self.assertEqual(self.js("list_tasks", status="blockers"),
                         {"tasks": [], "more": 0, "hidden_done": 0})

    def test_unknown_status_is_error(self):
        data = self.js("list_tasks", status="nope")
        self.assertEqual(set(data), {"error"})
        self.assertIn("nope", data["error"])

    def test_unknown_format_is_error(self):
        self.assertEqual(set(self.parse("list_tasks", self.call("list_tasks", format="xml"))), {"error"})


class GetJsonTests(JsonCase):
    def test_fields_and_plan(self):
        tid = self.add(text="Full wording", title="Short", priority="asap", energy="S",
                       domains="alpha", links="projects/demo/overview", project="demo")
        self.tm.store.edit_task(tid, plan="# Plan\n- step one\n")
        self.assertEqual(self.js("get_task", id=tid), {
            "id": tid, "title": "Short", "text": "Full wording", "status": "backlog",
            "priority": "asap", "deadline": None, "energy": "S", "domains": ["alpha"],
            "project": "demo", "links": ["projects/demo/overview"], "plan": "# Plan\n- step one\n",
        })

    def test_plan_is_null_without_plan(self):
        tid = self.add()
        data = self.js("get_task", id=tid)
        self.assertIn("plan", data)
        self.assertIsNone(data["plan"])
        self.assertEqual(data["project"], "global")

    def test_unknown_id_is_error(self):
        data = self.js("get_task", id="zzzzzz")
        self.assertEqual(set(data), {"error"})
        self.assertTrue(data["error"])

    def test_unknown_format_is_error(self):
        tid = self.add()
        out = self.call("get_task", id=tid, format="yaml")
        self.assertEqual(set(self.parse("get_task", out)), {"error"})


class TextUnchangedTests(ServerCase):
    def test_list_text_without_format(self):
        tid = self.add(text="Full wording", title="Short", priority="high", deadline="2026-10-01")
        expected = f"`{tid}` [backlog] Short · ⏫ high 📅 2026-10-01 @global"
        self.assertEqual(self.call("list_tasks"), expected)
        self.assertEqual(self.call("list_tasks", format=""), expected)

    def test_list_text_notes_for_hidden_done_and_rest(self):
        a = self.add(title="A")
        self.add(title="B")
        d = self.add(title="D")
        self.call("move_task", id=d, status="done")
        self.assertEqual(
            self.call("list_tasks", limit=1),
            f"`{a}` [backlog] A · @global\n(ще 1 — звузь фільтр або збільш `limit`)\n"
            "(+1 done приховано — `status=done`)",
        )

    def test_get_text_without_format(self):
        tid = self.add(text="Full wording", title="Short", energy="M", domains="alpha",
                       links="projects/demo/overview")
        self.tm.store.edit_task(tid, plan="step one")
        expected = (f"`{tid}` [backlog] 📌 Short · Full wording · ⚡M #alpha @global\n"
                    "🔗 projects/demo/overview\n\n📝 План:\nstep one")
        self.assertEqual(self.call("get_task", id=tid), expected)
        self.assertEqual(self.call("get_task", id=tid, format=""), expected)

    def test_get_text_unknown_id_still_warns(self):
        self.assertTrue(self.call("get_task", id="zzzzzz").startswith("⚠️"))


if __name__ == "__main__":
    unittest.main()
