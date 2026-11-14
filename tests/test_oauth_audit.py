"""Offline tests for the OAuth audit log: event, who, which client, where the code goes —
never rid, codes or tokens.

Run: uv run python -m unittest discover -s tests -p 'test_oauth_audit.py' -v
No sockets, no live state: the provider's state file is redirected to a temp dir.
"""
import asyncio
import sys
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tasks_oauth import TasksOAuthProvider  # noqa: E402

RESOURCE = 'https://tasks.example/tasks'
CLAUDE = 'https://claude.ai/api/mcp/auth_callback'
SCOPE = 'tasks'
USER = 'vlad'
LOGGER = 'tasks_oauth.audit'


def client(cid, uri, name):
    return OAuthClientInformationFull(client_id=cid, client_name=name, redirect_uris=[uri],
                                      token_endpoint_auth_method='none')


class AuditTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = patch('tasks_oauth.STATE_FILE', Path(self._tmp.name) / 'state.json')
        p.start()
        self.addCleanup(p.stop)
        self.prov = TasksOAuthProvider(RESOURCE, RESOURCE, redirect_allow=(CLAUDE,))

    def complete_login(self, rid):
        return self.prov.complete_login(rid)

    def test_full_flow_is_audited_without_secrets(self):
        info = client('c1abcdef-1234', CLAUDE, 'Claude')
        with self.assertLogs(LOGGER, 'INFO') as logs:
            asyncio.run(self.prov.register_client(info))
            with self.assertRaises(RegistrationError):
                asyncio.run(self.prov.register_client(client('evil0000-x', 'https://evil.example/cb', 'Evil')))
            params = AuthorizationParams(state='st', scopes=[SCOPE], code_challenge='c' * 43,
                redirect_uri=CLAUDE, redirect_uri_provided_explicitly=True, resource=RESOURCE)
            rid = parse_qs(urlsplit(asyncio.run(self.prov.authorize(info, params))).query)['rid'][0]
            self.prov.audit_pending('login_failed', rid)
            code = parse_qs(urlsplit(self.complete_login(rid)).query)['code'][0]
            tok = asyncio.run(self.prov.exchange_authorization_code(
                info, AuthorizationCode(**self.prov.codes[code])))
            rt = asyncio.run(self.prov.load_refresh_token(info, tok.refresh_token))
            tok2 = asyncio.run(self.prov.exchange_refresh_token(info, rt, []))
        out = '\n'.join(logs.output)
        where = 'client_id=c1abcdef redirect=claude.ai/api/mcp/auth_callback'
        for line in (
            f"event=register user=- client='Claude' {where}",
            "event=register_rejected user=- client='Evil' client_id=evil0000 redirect=evil.example/cb",
            f"event=login_failed user=- client='Claude' {where}",
            f"event=login user={USER} client='Claude' {where}",
            f"event=refresh user={USER} client='Claude' {where}",
        ):
            with self.subTest(line=line):
                self.assertIn(line, out)
        for secret in (rid, code, tok.access_token, tok.refresh_token, tok2.access_token, tok2.refresh_token):
            self.assertNotIn(secret, out)

    def test_unknown_rid_and_lockout(self):
        with self.assertLogs(LOGGER, 'INFO') as logs:
            self.prov.audit_pending('login_failed', 'never-issued-rid')
            self.prov.audit('login_locked')
        out = '\n'.join(logs.output)
        self.assertIn("event=login_failed user=- client='-' client_id=- redirect=-", out)
        self.assertIn('event=login_locked', out)
        self.assertNotIn('never-issued-rid', out)

    def test_client_name_cannot_forge_log_lines(self):
        with self.assertLogs(LOGGER, 'INFO') as logs:
            self.prov.audit('register_rejected', name='x\nOAUTH_AUDIT event=login user=vlad')
        self.assertEqual(len(logs.records), 1)
        self.assertNotIn('\n', logs.records[0].getMessage())

    def test_audit_logger_writes_on_its_own(self):
        lg = logging.getLogger(LOGGER)
        self.assertFalse(lg.propagate)
        self.assertEqual(lg.level, logging.INFO)
        self.assertTrue(any(isinstance(h, logging.StreamHandler) for h in lg.handlers))


if __name__ == '__main__':
    unittest.main()
