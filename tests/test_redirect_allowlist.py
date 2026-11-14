"""P2: allowlist redirect_uri у TasksOAuthProvider — розбір адрес, реєстрація, збережені
клієнти поза списком. Без мережі й без живого стейту (STATE_FILE — у тимчасовій теці).

Запуск: uv run python -m unittest discover -s tests -p 'test_redirect_allowlist.py' -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tasks_oauth  # noqa: E402
from mcp.server.auth.provider import RegistrationError  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402
from tasks_oauth import TasksOAuthProvider, redirect_allowed, redirect_allowlist  # noqa: E402

RESOURCE = "https://tasks.example/tasks"
CLAUDE = "https://claude.ai/api/mcp/auth_callback"
CHATGPT = "https://chatgpt.com/connector/oauth/L5rDATRQa6U6"
DEFAULT = tasks_oauth.DEFAULT_REDIRECT_ALLOWLIST


def client(cid, *uris, name="Claude"):
    return OAuthClientInformationFull(client_id=cid, client_name=name, redirect_uris=list(uris),
                                      token_endpoint_auth_method="none")


class RedirectAllowedTests(unittest.TestCase):
    def test_real_clients_pass(self):
        for uri in (CLAUDE, CHATGPT, "http://127.0.0.1:53682/callback",
                    "http://localhost:1455/auth/callback", "http://[::1]:8080/cb"):
            with self.subTest(uri=uri):
                self.assertTrue(redirect_allowed(uri, DEFAULT))

    def test_everything_else_rejected(self):
        for uri in (
            "https://evil.example/cb",
            "https://claude.ai.evil.example/api/mcp/auth_callback",
            "https://claude.ai@evil.example/api/mcp/auth_callback",
            "https://claude.ai:8443/api/mcp/auth_callback",
            "http://claude.ai/api/mcp/auth_callback",
            "https://claude.ai/api/mcp/auth_callback/extra",
            "https://claude.ai/api/mcp/auth_callback?next=https://evil.example",
            "https://claude.ai/api/mcp/auth_callback#frag",
            "https://chatgpt.com/connector/oauth/OTHERID",
            "https://chat.openai.com/aip/g-x/oauth/callback",
            "https://127.0.0.1:53682/callback",
            "http://127.0.0.1.evil.example/cb",
            "http://127.0.0.1:99999/cb",
            "not a url",
            "",
        ):
            with self.subTest(uri=uri):
                self.assertFalse(redirect_allowed(uri, DEFAULT))

    def test_wildcard_is_one_id_segment(self):
        allow = ("https://chatgpt.com/connector/oauth/*",)
        self.assertTrue(redirect_allowed("https://chatgpt.com/connector/oauth/Any_1-x", allow))
        for uri in ("https://chatgpt.com/connector/oauth/", "https://chatgpt.com/connector/oauth/a/b",
                    "https://chatgpt.com/connector/oauth/../../evil"):
            with self.subTest(uri=uri):
                self.assertFalse(redirect_allowed(uri, allow))

    def test_env_overrides_default(self):
        with patch.dict(os.environ, {"TASKS_REDIRECT_ALLOWLIST": f" {CLAUDE} , loopback "}):
            self.assertEqual(redirect_allowlist(), (CLAUDE, "loopback"))
        with patch.dict(os.environ, {"TASKS_REDIRECT_ALLOWLIST": ""}):
            self.assertEqual(redirect_allowlist(), DEFAULT)


class ProviderAllowlistTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state.json"
        p = patch("tasks_oauth.STATE_FILE", self.state)
        p.start()
        self.addCleanup(p.stop)

    def test_register_rejects_foreign_or_mixed_redirects(self):
        prov = TasksOAuthProvider(RESOURCE, RESOURCE, redirect_allow=DEFAULT)
        for info in (client("evil", "https://evil.example/cb"),
                     client("mixed", CLAUDE, "https://evil.example/cb")):
            with self.subTest(client=info.client_id):
                with self.assertRaises(RegistrationError) as ctx:
                    asyncio.run(prov.register_client(info))
                self.assertEqual(ctx.exception.error, "invalid_redirect_uri")
        self.assertEqual(prov.clients, {})

    def test_stored_client_outside_allowlist_is_invisible_but_kept(self):
        self.state.write_text(json.dumps({"clients": {
            "old": client("old", "https://evil.example/cb", name="Old").model_dump(mode="json"),
            "claude": client("claude", CLAUDE).model_dump(mode="json"),
        }}), encoding="utf-8")
        with self.assertLogs("tasks_oauth", "WARNING") as logs:
            prov = TasksOAuthProvider(RESOURCE, RESOURCE, redirect_allow=DEFAULT)
        self.assertIsNone(asyncio.run(prov.get_client("old")))
        self.assertIsNotNone(asyncio.run(prov.get_client("claude")))
        self.assertIn("old", prov.clients, "запис не видаляється — відкат змінною оточення")
        self.assertIn("'Old' → evil.example", logs.output[0])

    def test_describe_pending_unknown_rid(self):
        prov = TasksOAuthProvider(RESOURCE, RESOURCE, redirect_allow=DEFAULT)
        self.assertIsNone(prov.describe_pending("nope"))


if __name__ == "__main__":
    unittest.main()
