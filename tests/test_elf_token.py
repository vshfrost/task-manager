"""Bearer-інстанс: другий, обмежений токен `elf` (TASKS_ELF_TOKEN).

Токен elf: лише з loopback; з MCP-тулів — `elf_*`, `list_tasks`, `get_task`, `board`,
`move_task`, `comment_task` і `edit_task` лише з аргументами з {id, domains}; дашборд і решта
тулів → 403. Зміни під ним у журналі з клієнтом `elf`. Головний токен — без змін.

Запуск: uv run python -m unittest tests.test_elf_token -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase, arequest, capture_app, request  # noqa: E402

MAIN = "main-token-fixture"
ELF = "elf-token-fixture"
MCP_HEADERS = {"accept": "application/json, text/event-stream"}
LOOPBACK = ("127.0.0.1", 1)


def _body(resp) -> dict:
    """JSON-RPC відповідь: або JSON, або один SSE-івент `data: {...}`."""
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return json.loads(resp.text)


class TokenCase(ServerCase):
    ENV = {"TASKS_TOKEN": MAIN, "TASKS_ELF_TOKEN": ELF}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = capture_app(cls.tm.run_http)

    def tool(self, token, name, args=None, client=LOOPBACK):
        """Повна MCP-сесія по HTTP: initialize → initialized → tools/call. Повертає Response
        виклику (або відповідь гейта: 401/403)."""
        hdrs = {**MCP_HEADERS, "authorization": f"Bearer {token}"}
        # Менеджер MCP-сесій SDK запускається лише раз на екземпляр — для кожної сесії свіжий.
        self.tm.mcp._lowlevel_server._session_manager = None
        app = capture_app(self.tm.run_http)

        async def run():
            async with app.router.lifespan_context(app):
                init = await arequest(app, "POST", "/tasks", headers=hdrs, client=client, json_body={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}}})
                if init.status_code != 200:
                    return init
                sess = {**hdrs, "mcp-session-id": init.headers["mcp-session-id"]}
                await arequest(app, "POST", "/tasks", headers=sess, client=client,
                               json_body={"jsonrpc": "2.0", "method": "notifications/initialized"})
                return await arequest(app, "POST", "/tasks", headers=sess, client=client,
                                      json_body={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                 "params": {"name": name, "arguments": args or {}}})

        return asyncio.run(run())

    def text(self, resp) -> str:
        self.assertEqual(resp.status_code, 200, resp.text)
        res = _body(resp)["result"]
        self.assertFalse(res.get("isError"), res)
        return res["content"][0]["text"]

    def seed(self, tags=("to-agent",)) -> str:
        t = self.tm.store.add_task("Fixture task", title="Fixture task", domains=list(tags))
        self.tm.store.move_task(t["id"], "todo")
        return t["id"]

    def statuses(self) -> dict[str, str]:
        return {t["id"]: t["status"] for t in self.tm.store.list_tasks()}


class ElfAllowedTests(TokenCase):
    def test_elf_token_from_loopback_may_claim(self):
        tid = self.seed()
        self.assertIn(tid, self.text(self.tool(ELF, "elf_claim", {"agent": "claude"})))
        self.assertEqual(self.statuses()[tid], "in_progress")

    def test_elf_token_from_loopback_may_escalate(self):
        tid = self.seed(("to-agent", "dev"))
        self.tm.store.move_task(tid, "in_progress")
        self.tm.store.submit(tid, "claude", "https://github.com/example/repo/pull/7")
        out = self.text(self.tool(ELF, "elf_escalate", {"id": tid, "reason": "red", "agent": "claude"}))
        self.assertTrue(out.startswith("✅"), out)
        self.assertIn("to-admin", self.tm.store.get_task(tid)["domains"])

    def test_elf_token_from_loopback_may_read(self):
        tid = self.seed()
        self.assertIn(tid, self.text(self.tool(ELF, "list_tasks")))
        self.assertIn("Fixture task", self.text(self.tool(ELF, "get_task", {"id": tid})))
        self.assertTrue(self.text(self.tool(ELF, "board")))

    def test_elf_token_from_ipv6_loopback_is_allowed(self):
        self.seed()
        self.assertTrue(self.text(self.tool(ELF, "board", client=("::1", 1))))

    def test_elf_token_may_move_task(self):
        tid = self.seed()
        out = self.text(self.tool(ELF, "move_task", {"id": tid, "status": "in_progress"}))
        self.assertTrue(out.startswith("✅"), out)
        self.assertEqual(self.statuses()[tid], "in_progress")

    def test_elf_token_move_task_follows_transition_rules(self):
        tid = self.tm.store.add_task("Fixture task", title="Fixture task", domains=["to-agent"])["id"]
        out = self.text(self.tool(ELF, "move_task", {"id": tid, "status": "todo"}))
        self.assertTrue(out.startswith("⚠️"), out)
        self.assertEqual(self.statuses()[tid], "backlog")

    def test_elf_token_may_comment_task(self):
        tid = self.seed()
        out = self.text(self.tool(ELF, "comment_task", {"id": tid, "text": "note"}))
        self.assertTrue(out.startswith("✅"), out)
        self.assertIn("note", self.tm.store.get_task(tid)["plan"])

    def test_elf_token_may_edit_task_domains(self):
        tid = self.seed()
        out = self.text(self.tool(ELF, "edit_task", {"id": tid, "domains": "to-agent dev"}))
        self.assertTrue(out.startswith("✅"), out)
        self.assertEqual(self.tm.store.get_task(tid)["domains"], ["to-agent", "dev"])


class ElfDeniedTests(TokenCase):
    def test_elf_token_cannot_call_write_tools(self):
        tid = self.seed()
        before = self.statuses()
        calls = [
            ("edit_task", {"id": tid, "text": "changed"}),
            ("add_task", {"text": "new", "title": "new"}),
            ("delete_task", {"id": tid}),
            ("set_priority", {"id": tid, "priority": "high"}),
            ("set_deadline", {"id": tid, "deadline": "2026-12-01"}),
        ]
        for name, args in calls:
            with self.subTest(tool=name):
                self.assertEqual(self.tool(ELF, name, args).status_code, 403)
        self.assertEqual(self.statuses(), before)
        self.assertEqual(self.tm.store.get_task(tid)["text"], "Fixture task")

    def test_elf_token_edit_task_beyond_id_and_domains_is_forbidden(self):
        tid = self.seed()
        calls = [
            {"id": tid, "status": "in_progress"},
            {"id": tid, "text": "changed"},
            {"id": tid, "plan": "# new plan"},
            {"id": tid, "domains": "dev", "title": "changed"},
            {"id": tid, "domains": "dev", "priority": ""},
        ]
        for args in calls:
            with self.subTest(args=sorted(args)):
                self.assertEqual(self.tool(ELF, "edit_task", args).status_code, 403)
        task = self.tm.store.get_task(tid)
        self.assertEqual((task["status"], task["text"], task["domains"], task.get("plan")),
                         ("todo", "Fixture task", ["to-agent"], None))

    def test_elf_token_cannot_call_project_tools(self):
        for name, args in (("create_project", {"slug": "demo", "name": "Demo"}), ("list_projects", {})):
            with self.subTest(tool=name):
                self.assertEqual(self.tool(ELF, name, args).status_code, 403)

    def test_elf_token_from_non_loopback_is_forbidden(self):
        self.seed()
        for client in (("192.0.2.10", 5), ("2001:db8::1", 5)):
            with self.subTest(client=client):
                self.assertEqual(self.tool(ELF, "board", client=client).status_code, 403)

    def test_elf_token_cannot_use_dashboard_api(self):
        hdrs = {"authorization": f"Bearer {ELF}"}
        for method, path, body in (
            ("GET", "/api/tasks", None),
            ("GET", "/api/history", None),
            ("POST", "/api/task/add", {"text": "x", "title": "x"}),
        ):
            with self.subTest(path=path):
                r = request(self.app, method, path, headers=hdrs, json_body=body)
                self.assertEqual(r.status_code, 403)
        self.assertEqual(self.statuses(), {})


class MainTokenTests(TokenCase):
    def test_main_token_may_call_every_kind_of_tool(self):
        tid = self.seed(tags=())
        self.assertIn("Fixture task", self.text(self.tool(MAIN, "get_task", {"id": tid})))
        self.assertIn("✅", self.text(self.tool(MAIN, "add_task", {"text": "n", "title": "n"})))
        self.assertIn("✅", self.text(self.tool(MAIN, "edit_task", {"id": tid, "text": "changed"})))
        self.assertTrue(self.text(self.tool(MAIN, "elf_review_next", {"agent": "claude"})))

    def test_main_token_from_any_address_and_dashboard(self):
        self.assertTrue(self.text(self.tool(MAIN, "board", client=("192.0.2.10", 5))))
        r = request(self.app, "GET", "/api/tasks", headers={"authorization": f"Bearer {MAIN}"})
        self.assertEqual(r.status_code, 200)

    def test_wrong_token_is_unauthorized(self):
        r = request(self.app, "POST", "/tasks", headers={"authorization": "Bearer nope"}, json_body={})
        self.assertEqual((r.status_code, r.json()), (401, {"error": "unauthorized"}))


class ElfHistoryTests(TokenCase):
    def test_change_via_elf_token_is_recorded_as_client_elf(self):
        tid = self.seed()
        self.text(self.tool(ELF, "elf_claim", {"agent": "claude"}))
        self.assertEqual(self.tm.store.history.read(tid)[0]["client"], "elf")

    def test_change_via_main_token_keeps_instance_label(self):
        tid = self.seed()
        self.text(self.tool(MAIN, "elf_claim", {"agent": "claude"}))
        self.assertEqual(self.tm.store.history.read(tid)[0]["client"], "local")


class ElfDisabledTests(ServerCase):
    ENV = {"TASKS_TOKEN": MAIN}

    def test_unset_elf_token_rejects_would_be_elf_token(self):
        app = capture_app(self.tm.run_http)
        r = request(app, "POST", "/tasks", headers={"authorization": f"Bearer {ELF}"}, json_body={})
        self.assertEqual(r.status_code, 401)

    def test_empty_elf_token_rejects_empty_bearer(self):
        with patch.dict(os.environ, {"TASKS_ELF_TOKEN": ""}):
            app = capture_app(self.tm.run_http)
        for authz in ("Bearer ", "Bearer"):
            with self.subTest(authz=authz):
                r = request(app, "POST", "/tasks", headers={"authorization": authz}, json_body={})
                self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
