"""Bearer-інстанс: API веб-дашборда і bearer-гейт MCP-ендпоінта, за ZOMBIES.

Запуск: uv run python -m unittest discover -s tests -p 'test_web_api.py' -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase, arequest, capture_app, request  # noqa: E402

TOKEN = "offline-bearer-fixture"


class WebCase(ServerCase):
    ENV = {"TASKS_TOKEN": TOKEN}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = capture_app(cls.tm.run_http)

    def get(self, path, **query):
        return request(self.app, "GET", path, query=query)

    def post(self, path, body=None, **kw):
        return request(self.app, "POST", path, json_body=body, **kw)

    def add(self, **fields):
        r = self.post("/api/task/add", {"text": "Задача", "title": "Задача", **fields})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["task"]


class ZeroTests(WebCase):
    def test_empty_board_payload(self):
        d = self.get("/api/tasks").json()
        self.assertEqual(d["tasks"], [])
        self.assertEqual(d["templates"], [])
        self.assertEqual((d["wip_limit"], d["title_max"]), (15, 50))
        self.assertIn("global", d["projects"])

    def test_empty_history(self):
        self.assertEqual(self.get("/api/history").json(), {"records": []})


class OneTests(WebCase):
    def test_add_edit_delete_signed_web(self):
        t = self.add(domains="#побут, дім", links=["projects/home/overview"])
        self.assertEqual(t["domains"], ["побут", "дім"])
        r = self.post("/api/task/edit", {"id": t["id"], "priority": "high", "domains": ["a"], "links": "none"})
        self.assertEqual(r.json()["task"]["priority"], "high")
        self.assertEqual(r.json()["task"]["links"], [])
        self.assertEqual(self.post("/api/task/delete", {"id": t["id"]}).status_code, 200)
        recs = self.get("/api/history", id=t["id"]).json()["records"]
        self.assertEqual([r["op"] for r in recs], ["delete", "edit", "add"])
        self.assertEqual({r["client"] for r in recs}, {"web"})


class BacklogTests(WebCase):
    def test_new_task_lands_in_backlog(self):
        self.assertEqual(self.add()["status"], "backlog")

    def test_create_ignores_status_from_client(self):
        for status in ("done", "in_progress", "later"):
            with self.subTest(status=status):
                self.assertEqual(self.add(status=status)["status"], "backlog")

    def test_filter_by_backlog(self):
        a, b = self.add(title="A"), self.add(title="B")
        self.post("/api/task/edit", {"id": b["id"], "status": "done"})
        got = self.get("/api/tasks", status="backlog").json()["tasks"]
        self.assertEqual([t["id"] for t in got], [a["id"]])

    def test_dashboard_has_backlog_not_todo(self):
        page = self.get("/task-manager").text
        self.assertIn('["backlog","backlog"]', page)


class ColumnsTests(WebCase):
    def page(self):
        return self.get("/task-manager").text

    def test_board_has_seven_columns_in_order(self):
        self.assertIn(
            'const COLS = [["backlog","backlog"],["todo","to do"],["blockers","blockers"],'
            '["in_progress","in progress"],["in_review","in review"],["testing","testing"],["done","done"]];',
            self.page(),
        )

    def test_counters_for_all_seven_columns_in_order(self):
        page = self.page()
        marks = ('mk("backlog"', 'mk("to do"', 'mk("blockers"', 'mk("in progress"', 'mk("in review"', 'mk("testing"', 'mk("done"')
        for m in marks:
            self.assertIn(m, page)
        self.assertEqual([page.index(m) for m in marks], sorted(page.index(m) for m in marks))

    def test_status_select_offers_the_new_columns(self):
        page = self.page()
        self.assertIn('<option value="todo">to do</option>', page)
        self.assertIn('<option value="in_review">in review</option>', page)
        self.assertIn('<option value="blockers">blockers</option>', page)
        self.assertLess(page.index('<option value="todo">'), page.index('<option value="blockers">'))
        self.assertLess(page.index('<option value="blockers">'), page.index('<option value="in_progress">'))

    def test_new_columns_have_their_own_colours(self):
        page = self.page()
        for var in ("--todo:", "--review:", "--blockers:"):
            self.assertIn(var, page)

    def test_grid_fits_seven_columns(self):
        self.assertIn("grid-template-columns:repeat(7,minmax(0,1fr))", self.page())

    def test_card_shows_task_id_chip(self):
        self.assertIn('chip(t.id, "id")', self.page())

    def test_id_chip_goes_first_in_meta(self):
        page = self.page()
        self.assertLess(page.index('chip(t.id, "id")'), page.index("chip(PRIO["))

    def test_id_chip_text_is_selectable(self):
        page = self.page()
        self.assertIn(".chip.id{", page)
        self.assertIn("user-select:text", page)


class ManyTests(WebCase):
    def test_filters(self):
        self.add(text="A", title="A", priority="high", domains="побут")
        self.add(text="B", title="B", project="home")
        c = self.add(text="C", title="C")
        for status in ("todo", "in_progress", "testing"):
            self.post("/api/task/edit", {"id": c["id"], "status": status})
        titles = lambda **q: sorted(t["title"] for t in self.get("/api/tasks", **q).json()["tasks"])  # noqa: E731
        self.assertEqual(titles(), ["A", "B", "C"])
        self.assertEqual(titles(priority="high"), ["A"])
        self.assertEqual(titles(domain="побут"), ["A"])
        self.assertEqual(titles(project="home"), ["B"])
        self.assertEqual(titles(status="testing"), ["C"])
        self.assertIn("home", self.get("/api/tasks").json()["projects"])

    def test_templates_listed(self):
        tpl = self.root / "projects" / "home"
        tpl.mkdir(exist_ok=True)
        (tpl / "template.md").write_text("- [ ] Сміття  ⚡S 📌[Сміття]\n", encoding="utf-8")
        try:
            self.assertEqual([t["title"] for t in self.get("/api/tasks").json()["templates"]], ["Сміття"])
        finally:
            (tpl / "template.md").unlink()


class BoundaryTests(WebCase):
    def test_history_limit_clamped(self):
        t = self.add()
        for p in ("asap", "high", "low"):
            self.post("/api/task/edit", {"id": t["id"], "priority": p})
        self.assertEqual(len(self.get("/api/history", limit="1").json()["records"]), 1)
        self.assertEqual(len(self.get("/api/history", limit="-3").json()["records"]), 1)
        self.assertEqual(len(self.get("/api/history", limit="999").json()["records"]), 4)

    def test_title_and_deadline_bounds(self):
        self.assertEqual(self.post("/api/task/add", {"text": "x", "title": "я" * 50}).status_code, 200)
        self.assertEqual(self.post("/api/task/add", {"text": "x", "title": "я" * 51}).status_code, 400)
        r = self.post("/api/task/add", {"text": "x", "title": "x", "deadline": "2026-13-45"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("реальною датою у форматі РРРР-ММ-ДД", r.json()["error"])


class InterfaceTests(WebCase):
    def test_pages(self):
        page = self.get("/task-manager")
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        legacy = self.get("/ui")
        self.assertEqual((legacy.status_code, legacy.headers["location"]), (302, "/task-manager"))

    def test_task_shape(self):
        t = self.add()
        self.assertTrue({"id", "title", "text", "status", "priority", "deadline", "energy",
                         "domains", "links", "file"} <= set(t))

    def test_dashboard_not_behind_bearer(self):
        # Дашборд захищає мережа (tailnet), не bearer — браузер заголовків не шле.
        self.assertEqual(self.get("/api/tasks").status_code, 200)


class BearerTests(WebCase):
    def test_mcp_endpoint_requires_token(self):
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": TOKEN},
                        {"authorization": f"Basic {TOKEN}"}):
            with self.subTest(headers=headers):
                r = request(self.app, "POST", "/tasks", headers=headers, json_body={})
                self.assertEqual((r.status_code, r.json()), (401, {"error": "unauthorized"}))
        self.assertEqual(request(self.app, "GET", "/tasks/x").status_code, 401)

    def test_valid_token_reaches_mcp(self):
        async def run():
            async with self.app.router.lifespan_context(self.app):
                return await arequest(self.app, "GET", "/tasks",
                                      headers={"authorization": f"Bearer {TOKEN}", "accept": "application/json"})
        r = asyncio.run(run())
        self.assertNotEqual(r.status_code, 401)
        self.assertEqual(r.status_code, 406, "MCP відповів сам: GET без text/event-stream")

    def test_missing_token_refuses_to_start(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"TASKS_TOKEN": ""}), self.assertRaises(SystemExit):
            capture_app(self.tm.run_http)


class ExceptionTests(WebCase):
    def test_bad_requests_are_400(self):
        t = self.add()
        cases = [
            ("/api/task/add", {"text": "без заголовка"}),
            ("/api/task/add", {"text": "", "title": "x"}),
            ("/api/task/edit", {"priority": "high"}),
            ("/api/task/edit", {"id": "zzzzzz", "text": "x"}),
            ("/api/task/edit", {"id": t["id"], "energy": "XL"}),
            ("/api/task/delete", {"id": "zzzzzz"}),
            ("/api/task/delete", {}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=body):
                r = self.post(path, body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertTrue(r.json()["error"])

    def test_malformed_json_is_400_not_500(self):
        for path in ("/api/task/add", "/api/task/edit", "/api/task/delete"):
            for raw in (b"{not json", b"[1, 2]"):
                with self.subTest(path=path, raw=raw):
                    self.assertEqual(request(self.app, "POST", path, raw=raw).status_code, 400)

    def test_non_numeric_limit_is_400(self):
        self.assertEqual(self.get("/api/history", limit="abc").status_code, 400)


class SimpleScenarioTests(WebCase):
    def test_drag_card_to_done(self):
        t = self.add(text="Винести сміття", title="Сміття", project="home")
        for status in ("todo", "in_progress", "in_review", "testing", "done"):
            self.assertEqual(self.post("/api/task/edit", {"id": t["id"], "status": status}).status_code, 200)
        done = self.get("/api/tasks", status="done").json()["tasks"]
        self.assertEqual([x["title"] for x in done], ["Сміття"])


if __name__ == "__main__":
    unittest.main()
