"""OAuth-інстанс (Claude-конектор + ChatGPT app): login-гейт, discovery, анотації,
ResourceGuard і мітка клієнта в журналі — за ZOMBIES. Без живих секретів і стану.

Запуск: uv run python -m unittest discover -s tests -p 'test_oauth_server.py' -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase, capture_app, request  # noqa: E402
from mcp.server.auth.middleware.auth_context import auth_context_var  # noqa: E402
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser  # noqa: E402
from mcp.server.auth.provider import AccessToken, AuthorizationParams  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402
from tasks_oauth import ResourceGuard, hash_password  # noqa: E402

RESOURCE = "https://tasks.example/tasks"
PASSWORD = "offline-password-fixture"
REDIRECT = "https://chatgpt.example/connector/oauth/cb"
READ_ONLY = {"list_tasks", "get_task", "board", "history", "elf_review_next"}
DESTRUCTIVE = {"edit_task", "delete_task"}
IDEMPOTENT = {"move_task", "set_priority", "set_deadline"}
ELF = {"elf_claim", "elf_release", "elf_block", "elf_submit", "elf_ci", "elf_merged", "elf_return"}  # пишуть, не ідемпотентні, не деструктивні


class OAuthCase(ServerCase):
    ENV = {
        "TASKS_TRANSPORT": "oauth",
        "TASKS_ISSUER_URL": RESOURCE,
        "TASKS_OAUTH_PASSWORD_HASH": hash_password(PASSWORD, iters=1_000),
        "TASKS_CLIENT": "phone",
        "TASKS_REDIRECT_ALLOWLIST": REDIRECT,
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = capture_app(cls.tm.run_oauth)
        cls.provider = cls.tm.OAUTH_PROVIDER

    def setUp(self):
        super().setUp()
        self.tm._login_state.update(fails=0, locked_until=0.0)

    def register(self, client_id="gpt", name="ChatGPT"):
        info = OAuthClientInformationFull(client_id=client_id, client_name=name, redirect_uris=[REDIRECT],
                                          token_endpoint_auth_method="none")
        asyncio.run(self.provider.register_client(info))
        return info

    def start_login(self) -> str:
        params = AuthorizationParams(state="st", scopes=["tasks"], code_challenge="c" * 43,
                                     redirect_uri=REDIRECT, redirect_uri_provided_explicitly=True,
                                     resource=RESOURCE)
        url = asyncio.run(self.provider.authorize(self.register(), params))
        return parse_qs(urlsplit(url).query)["rid"][0]

    def login(self, rid, password):
        return request(self.app, "POST", "/login", form={"rid": rid, "password": password})


class LoginZeroOneTests(OAuthCase):
    def test_form_carries_rid(self):
        rid = self.start_login()
        r = request(self.app, "GET", "/login", query={"rid": rid})
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"value='{rid}'", r.text)
        self.assertIn("action='login'", r.text, "відносний action — інакше за Caddy handle_path 404")

    def test_empty_password_rejected(self):
        self.assertEqual(self.login(self.start_login(), "").status_code, 401)

    def test_right_password_redirects_with_code(self):
        r = self.login(self.start_login(), PASSWORD)
        self.assertEqual(r.status_code, 302)
        loc = urlsplit(r.headers["location"])
        self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", REDIRECT)
        q = parse_qs(loc.query)
        self.assertEqual(q["state"], ["st"])
        self.assertIn(q["code"][0], self.provider.codes)

    def test_rid_is_single_use(self):
        rid = self.start_login()
        self.assertEqual(self.login(rid, PASSWORD).status_code, 302)
        r = self.login(rid, PASSWORD)
        self.assertEqual(r.status_code, 400)
        self.assertIn("застаріла", r.text)


class LoginBoundaryTests(OAuthCase):
    def test_four_fails_then_success_resets_counter(self):
        rid = self.start_login()
        for _ in range(4):
            self.assertEqual(self.login(rid, "wrong").status_code, 401)
        self.assertEqual(self.login(rid, PASSWORD).status_code, 302)
        self.assertEqual(self.tm._login_state["fails"], 0)

    def test_fifth_fail_locks_even_right_password(self):
        rid = self.start_login()
        for _ in range(5):
            self.assertEqual(self.login(rid, "wrong").status_code, 401)
        codes_before = set(self.provider.codes)
        r = self.login(rid, PASSWORD)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Забагато спроб", r.text)
        self.assertEqual(set(self.provider.codes), codes_before, "під локом код не видається")
        self.assertIn(rid, self.provider.pending, "rid не спалено — після лока можна дологінитись")

    def test_lock_expires(self):
        rid = self.start_login()
        self.tm._login_state["locked_until"] = 1.0  # у минулому
        self.assertEqual(self.login(rid, PASSWORD).status_code, 302)


XSS_RID = "x'\"><script>alert(1)</script>"
LOGIN_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"


class LoginHardeningTests(OAuthCase):
    """rid з адреси/форми — лише екранований; кожна відповідь /login — із захисними заголовками."""

    def assert_hardened(self, r):
        self.assertEqual(r.headers.get("content-security-policy"), LOGIN_CSP)
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("referrer-policy"), "no-referrer")
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.assertNotIn("<script>", r.text)

    def test_unknown_rid_is_not_reflected_and_has_no_form(self):
        r = request(self.app, "GET", "/login", query={"rid": XSS_RID})
        self.assertEqual(r.status_code, 200)
        self.assert_hardened(r)
        self.assertIn("застарів або невідомий", r.text)
        self.assertNotIn("x&#x27;", r.text)
        self.assertNotIn("<form", r.text)

    def test_wrong_password_escapes_rid(self):
        r = self.login(XSS_RID, "wrong")
        self.assertEqual(r.status_code, 401)
        self.assert_hardened(r)
        self.assertIn("Невірний пароль", r.text)

    def test_lockout_page_hardened(self):
        self.tm._login_state["locked_until"] = 10**12
        r = self.login(XSS_RID, PASSWORD)
        self.assertEqual(r.status_code, 429)
        self.assert_hardened(r)

    def test_stale_rid_page_hardened(self):
        r = self.login("never-issued", PASSWORD)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.headers.get("content-security-policy"), LOGIN_CSP)

    def test_success_redirect_has_no_form_action_block(self):
        # form-action блокував би 302 на redirect_uri клієнта в Chrome — його бути не повинно.
        r = request(self.app, "GET", "/login", query={"rid": "abc"})
        self.assertNotIn("form-action", r.headers.get("content-security-policy", ""))
        self.assertEqual(self.login(self.start_login(), PASSWORD).status_code, 302)


class ConsentScreenTests(OAuthCase):
    """Екран згоди: хто просить доступ, куди піде код, чи клієнт новий; усе екрановано."""

    def live_rid(self, name="ChatGPT", issued_at=None):
        info = OAuthClientInformationFull(
            client_id="consent", client_name=name, redirect_uris=[REDIRECT],
            token_endpoint_auth_method="none",
            client_id_issued_at=int(time.time()) if issued_at is None else issued_at)
        asyncio.run(self.provider.register_client(info))
        params = AuthorizationParams(state="st", scopes=["tasks"], code_challenge="c" * 43,
                                     redirect_uri=REDIRECT, redirect_uri_provided_explicitly=True,
                                     resource=RESOURCE)
        return parse_qs(urlsplit(asyncio.run(self.provider.authorize(info, params))).query)["rid"][0]

    def test_consent_shows_client_host_and_new_badge(self):
        r = request(self.app, "GET", "/login", query={"rid": self.live_rid()})
        self.assertIn("Доступ просить: <b>ChatGPT</b>", r.text)
        self.assertIn("Код піде на: <b>chatgpt.example</b>", r.text)
        self.assertIn("новий клієнт", r.text)
        self.assertIn("не вводь пароль", r.text)

    def test_old_client_has_no_new_badge(self):
        r = request(self.app, "GET", "/login", query={"rid": self.live_rid(issued_at=int(time.time()) - 3 * 86400)})
        self.assertNotIn("новий клієнт", r.text)

    def test_client_name_is_escaped(self):
        r = request(self.app, "GET", "/login", query={"rid": self.live_rid(name="<img src=x onerror=alert(1)>")})
        self.assertNotIn("<img", r.text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", r.text)


class RedirectAllowlistServerTests(OAuthCase):
    """P2 через справжній /register SDK: чужа адреса → 400, дозволена → 201."""

    def register_via_http(self, redirect):
        return request(self.app, "POST", "/register", json_body={
            "redirect_uris": [redirect], "client_name": "x", "token_endpoint_auth_method": "none"})

    def test_foreign_redirect_rejected(self):
        before = set(self.provider.clients)
        r = self.register_via_http("https://evil.example/cb")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_redirect_uri")
        self.assertEqual(set(self.provider.clients), before)

    def test_allowed_redirect_registered(self):
        r = self.register_via_http(REDIRECT)
        self.assertEqual(r.status_code, 201)
        self.assertIn(r.json()["client_id"], self.provider.clients)


class AuditRouteTests(OAuthCase):
    """/login пише в журнал аудиту невдалий пароль і блокування — без rid."""

    def test_wrong_password_is_audited(self):
        rid = self.start_login()
        with self.assertLogs("tasks_oauth.audit", "INFO") as logs:
            self.assertEqual(self.login(rid, "wrong").status_code, 401)
        self.assertIn("event=login_failed user=- client='ChatGPT'", logs.output[0])
        self.assertNotIn(rid, "\n".join(logs.output))

    def test_success_and_lockout_are_audited(self):
        rid = self.start_login()
        with self.assertLogs("tasks_oauth.audit", "INFO") as logs:
            self.assertEqual(self.login(rid, PASSWORD).status_code, 302)
            self.tm._login_state["locked_until"] = 10**12
            self.assertEqual(self.login(self.start_login(), PASSWORD).status_code, 429)
        out = "\n".join(logs.output)
        self.assertIn("event=login user=vlad client='ChatGPT'", out)
        self.assertIn("event=login_locked", out)


class DiscoveryInterfaceTests(OAuthCase):
    def test_protected_resource_metadata(self):
        d = request(self.app, "GET", "/.well-known/oauth-protected-resource/tasks").json()
        self.assertEqual(d["resource"], RESOURCE)
        self.assertEqual(d["scopes_supported"], ["tasks"])

    def test_authorization_server_metadata(self):
        d = request(self.app, "GET", "/.well-known/oauth-authorization-server").json()
        self.assertEqual(d["issuer"], RESOURCE)
        self.assertEqual(d["code_challenge_methods_supported"], ["S256"])
        self.assertTrue(d["registration_endpoint"].endswith("/register"))

    def test_mcp_without_token_is_401_with_metadata_hint(self):
        r = request(self.app, "POST", "/tasks", json_body={})
        self.assertEqual(r.status_code, 401)
        self.assertIn("oauth-protected-resource/tasks", r.headers["www-authenticate"])

    def test_no_dashboard_on_public_instance(self):
        for path in ("/task-manager", "/api/tasks"):
            with self.subTest(path=path):
                self.assertEqual(request(self.app, "GET", path).status_code, 404)

    def test_resource_guard_installed(self):
        self.assertEqual(self.provider.resource, RESOURCE)
        self.assertTrue(any(m.cls is ResourceGuard and m.kwargs == {"resource": RESOURCE}
                            for m in self.app.user_middleware))
        r = request(self.app, "POST", "/token", form={"grant_type": "refresh_token", "resource": "https://evil.example"})
        self.assertEqual((r.status_code, r.json()["error"]), (400, "invalid_target"))

    def test_tool_annotations(self):
        tools = asyncio.run(self.tm.mcp.list_tools())
        self.assertEqual({t.name for t in tools}, READ_ONLY | DESTRUCTIVE | IDEMPOTENT | {"add_task", *ELF})
        for t in tools:
            with self.subTest(tool=t.name):
                a = t.annotations
                self.assertEqual(a.read_only_hint, t.name in READ_ONLY)
                self.assertEqual(bool(a.destructive_hint), t.name in DESTRUCTIVE)
                self.assertEqual(bool(a.idempotent_hint), t.name in IDEMPOTENT)
                self.assertIs(a.open_world_hint, False)


class ClientLabelTests(OAuthCase):
    def as_token(self, client_id):
        tok = AccessToken(token="t", client_id=client_id, scopes=["tasks"], resource=RESOURCE, subject="vlad")
        return auth_context_var.set(AuthenticatedUser(tok))

    def test_history_signed_by_oauth_client(self):
        import planner_history

        self.register("cl", "Claude")
        self.register("gpt", "ChatGPT")
        self.register("anon", "")
        for cid, label in (("gpt", "chatgpt"), ("cl", "claude"), ("anon", "phone"), ("unknown", "phone")):
            with self.subTest(client=cid):
                reset = self.as_token(cid)
                try:
                    self.assertEqual(planner_history.current_client(), label)
                    with planner_history.as_client("web"):
                        self.assertEqual(planner_history.current_client(), "web")
                finally:
                    auth_context_var.reset(reset)
        self.assertEqual(planner_history.current_client(), "phone", "без токена — env-мітка")

    def test_tool_call_logs_client_and_user(self):
        self.register("gpt", "ChatGPT")
        reset = self.as_token("gpt")
        try:
            tid = self.add()
        finally:
            auth_context_var.reset(reset)
        self.assertIn("vlad@chatgpt · add_task", self.call("history", id=tid))


class StartupExceptionTests(OAuthCase):
    def test_refuses_to_start_without_password(self):
        with patch.dict(os.environ, {"TASKS_OAUTH_PASSWORD_HASH": "", "TASKS_OAUTH_PASSWORD": ""}), \
             self.assertRaises(SystemExit):
            capture_app(self.tm.run_oauth)


if __name__ == "__main__":
    unittest.main()
