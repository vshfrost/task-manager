"""Offline OAuth regression tests; no live credentials or production state.

Run: uv run python -m unittest discover -s tests -p 'test_oauth_flow.py' -v
All issuance uses fixed test doubles in a temporary state store.
"""
import base64
import hashlib
import json
import tempfile
import time
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlencode

from starlette.applications import Starlette
from starlette.routing import Route
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tasks_oauth import (  # noqa: E402
    ResourceGuard, TasksOAuthProvider, check_password, hash_password,
)

RESOURCE = 'https://tasks.example/tasks'
REDIRECT = 'https://client.example/callback'
VERIFIER = 'offline-test-fixture-' * 3



class LocalHTTP:
    """Minimal in-process ASGI client; never opens a socket."""
    def __init__(self, app):
        self.app = app

    async def post(self, path, *, data=None, content=None, headers=None):
        body = (urlencode(data) if data is not None else content or '').encode()
        scope = dict(type='http', asgi={'version': '3.0'}, http_version='1.1',
            method='POST', scheme='http', path=path, raw_path=path.encode(),
            query_string=b'', root_path='', server=('test', 80), client=('test', 1),
            headers=[(b'content-type', b'application/x-www-form-urlencoded')])
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(message):
            messages.append(message)
        await self.app(scope, receive, send)
        class Result:
            status_code = messages[0]['status']
            def json(self):
                return json.loads(b''.join(m.get('body', b'') for m in messages))
        return Result()


class OAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state.json'
        self.state_patch = patch('tasks_oauth.STATE_FILE', self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.sequence = iter(f'offline-fixture-{i}' for i in range(100))
        self.random_patch = patch('tasks_oauth.secrets.token_urlsafe', side_effect=lambda n: next(self.sequence))
        self.random_patch.start()
        self.addCleanup(self.random_patch.stop)
        self.provider = TasksOAuthProvider(RESOURCE, RESOURCE, redirect_allow=(REDIRECT,))
        self.client = OAuthClientInformationFull(
            client_id='offline-client', redirect_uris=[REDIRECT],
            token_endpoint_auth_method='none',
            grant_types=['authorization_code', 'refresh_token'], scope='tasks',
        )
        await self.provider.register_client(self.client)
        handler = TokenHandler(self.provider, ClientAuthenticator(self.provider))
        app = Starlette(routes=[Route('/token', handler.handle, methods=['POST'])])
        app.add_middleware(ResourceGuard, resource=RESOURCE)
        self.http = LocalHTTP(app)

    async def code(self, resource=RESOURCE):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')
        params = AuthorizationParams(state='test', scopes=['tasks'], code_challenge=challenge,
            redirect_uri=REDIRECT, redirect_uri_provided_explicitly=True, resource=resource)
        login = await self.provider.authorize(self.client, params)
        rid = parse_qs(urlsplit(login).query)['rid'][0]
        # Password UI is outside this unit; model its successful gate.
        redirect = self.provider.complete_login(rid)
        return parse_qs(urlsplit(redirect).query)['code'][0]

    async def exchange(self, code, **overrides):
        data = dict(grant_type='authorization_code', client_id=self.client.client_id,
            code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER, resource=RESOURCE)
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        return await self.http.post('/token', data=data)

    async def test_code_refresh_resource_and_persistence(self):
        code = await self.code()
        response = await self.exchange(code)
        self.assertEqual(response.status_code, 200)
        issued = response.json()
        access = await self.provider.load_access_token(issued['access_token'])
        self.assertEqual(access.resource, RESOURCE)
        self.assertEqual(access.subject, 'vlad')
        refresh = dict(grant_type='refresh_token', client_id=self.client.client_id,
            refresh_token=issued['refresh_token'], resource=RESOURCE)
        for extra, error in [({'resource': 'https://other.example'}, 'invalid_target'),
                             ({'scope': 'admin'}, 'invalid_scope')]:
            bad = await self.http.post('/token', data={**refresh, **extra})
            self.assertEqual(bad.json()['error'], error)
        response = await self.http.post('/token', data=refresh)
        self.assertEqual(response.status_code, 200)
        rotated = response.json()
        reloaded = TasksOAuthProvider(RESOURCE, RESOURCE, redirect_allow=(REDIRECT,))
        self.assertEqual((await reloaded.load_access_token(rotated['access_token'])).resource, RESOURCE)
        self.assertEqual((await reloaded.load_refresh_token(self.client, rotated['refresh_token'])).resource, RESOURCE)
        self.assertEqual((await self.http.post('/token', data=refresh)).json()['error'], 'invalid_grant')
        self.assertEqual((await self.exchange(code)).json()['error'], 'invalid_grant')

    async def test_resource_rejected_at_authorize_and_token(self):
        with self.assertRaises(AuthorizeError):
            await self.code('https://other.example')
        code = await self.code()
        result = await self.exchange(code, resource='https://other.example')
        self.assertEqual(result.json()['error'], 'invalid_target')
        self.assertEqual((await self.exchange(code)).status_code, 200)

    async def test_legacy_client_without_resource_gets_bound_token(self):
        code = await self.code(None)
        result = await self.exchange(code, resource=None)
        self.assertEqual(result.status_code, 200)
        self.assertEqual((await self.provider.load_access_token(result.json()['access_token'])).resource, RESOURCE)

    async def test_pkce_redirect_client_and_expiry(self):
        code = await self.code()
        for overrides in ({'code_verifier': 'incorrect-fixture'}, {'redirect_uri': 'https://wrong.example'},
                          {'client_id': 'unknown-client'}):
            self.assertGreaterEqual((await self.exchange(code, **overrides)).status_code, 400)
        self.provider.codes[code]['expires_at'] = time.time() - 1
        self.assertEqual((await self.exchange(code)).json()['error'], 'invalid_grant')

    async def test_invalid_persisted_tokens_rejected(self):
        result = (await self.exchange(await self.code())).json()
        for bucket, key in [(self.provider.access, result['access_token']),
                            (self.provider.refresh, result['refresh_token'])]:
            original = dict(bucket[key])
            for change in ({'resource': None}, {'resource': 'https://wrong.example'},
                           {'scopes': ['admin']}, {'expires_at': 0}):
                bucket[key] = {**original, **change}
                if bucket is self.provider.access:
                    self.assertIsNone(await self.provider.load_access_token(key))
                else:
                    self.assertIsNone(await self.provider.load_refresh_token(self.client, key))

    async def test_startup_prunes_legacy_mismatched_and_expired_tokens(self):
        good = (await self.exchange(await self.code())).json()
        state = json.loads(self.state.read_text())
        state["access"]["legacy"] = {
            **state["access"][good["access_token"]], "resource": None,
        }
        state["access"]["wrong"] = {
            **state["access"][good["access_token"]],
            "resource": "https://wrong.example",
        }
        state["refresh"]["expired"] = {
            **state["refresh"][good["refresh_token"]], "expires_at": 0,
        }
        self.state.write_text(json.dumps(state))

        reloaded = TasksOAuthProvider(RESOURCE, RESOURCE, redirect_allow=(REDIRECT,))
        self.assertEqual(set(reloaded.access), {good["access_token"]})
        self.assertEqual(set(reloaded.refresh), {good["refresh_token"]})
        persisted = json.loads(self.state.read_text())
        self.assertNotIn("legacy", persisted["access"])
        self.assertNotIn("wrong", persisted["access"])
        self.assertNotIn("expired", persisted["refresh"])

    async def test_client_label_from_dcr_name(self):
        for name, label in [('ChatGPT', 'chatgpt'), ('Claude', 'claude'),
                            ('  Evil <script>/../x  ' * 3, 'evil-script-x-evil-s'), ('', None)]:
            info = self.client.model_copy(update={'client_id': f'c-{label}', 'client_name': name})
            await self.provider.register_client(info)
            self.assertEqual(self.provider.client_label(info.client_id), label)
        self.assertIsNone(self.provider.client_label('unknown'))
        self.assertIsNone(self.provider.client_label(None))

    async def test_duplicate_resource_rejected(self):
        response = await self.http.post('/token', content='resource=x&resource=y',
            headers={'content-type': 'application/x-www-form-urlencoded'})
        self.assertEqual(response.json()['error'], 'invalid_target')


    async def test_revoke_removes_both_tokens(self):
        issued = (await self.exchange(await self.code())).json()
        for key in ('access_token', 'refresh_token'):
            await self.provider.revoke_token(issued[key])
        self.assertIsNone(await self.provider.load_access_token(issued['access_token']))
        self.assertIsNone(await self.provider.load_refresh_token(self.client, issued['refresh_token']))

    async def test_expired_access_token_is_dropped(self):
        issued = (await self.exchange(await self.code())).json()
        self.provider.access[issued['access_token']]['expires_at'] = time.time() - 1
        self.assertIsNone(await self.provider.load_access_token(issued['access_token']))
        self.assertNotIn(issued['access_token'], self.provider.access)

    async def test_unknown_or_foreign_code_and_refresh(self):
        self.assertIsNone(await self.provider.load_authorization_code(self.client, 'nope'))
        self.assertIsNone(await self.provider.load_refresh_token(self.client, 'nope'))
        code = await self.code()
        other = self.client.model_copy(update={'client_id': 'other'})
        self.assertIsNone(await self.provider.load_authorization_code(other, code))

    async def test_authorize_rejects_foreign_scope(self):
        params = AuthorizationParams(state='s', scopes=['admin'], code_challenge='c' * 43,
            redirect_uri=REDIRECT, redirect_uri_provided_explicitly=True, resource=RESOURCE)
        with self.assertRaises(AuthorizeError):
            await self.provider.authorize(self.client, params)

    async def test_complete_login_unknown_rid(self):
        with self.assertRaises(KeyError):
            self.provider.complete_login('never-issued')

    async def test_identity_assertion_not_supported(self):
        with self.assertRaises(NotImplementedError):
            await self.provider.exchange_identity_assertion()

    async def test_oversized_token_body_rejected(self):
        big = await self.http.post('/token', content='a=' + 'x' * 70_000)
        self.assertEqual(big.status_code, 413)


class PasswordTests(unittest.TestCase):
    def test_hash_format_and_check(self):
        stored = hash_password('right-fixture', iters=1_000, salt=b'0' * 16)
        algo, iters, salt, digest = stored.split('$')
        self.assertEqual((algo, iters, salt), ('pbkdf2_sha256', '1000', (b'0' * 16).hex()))
        self.assertEqual(len(digest), 64)
        with patch.dict('os.environ', {'TASKS_OAUTH_PASSWORD_HASH': stored}, clear=True):
            self.assertTrue(check_password('right-fixture'))
            for wrong in ('', 'wrong', 'right-fixture ', 'RIGHT-FIXTURE'):
                self.assertFalse(check_password(wrong), wrong)

    def test_hash_takes_priority_over_plaintext(self):
        stored = hash_password('from-hash', iters=1_000)
        env = {'TASKS_OAUTH_PASSWORD_HASH': stored, 'TASKS_OAUTH_PASSWORD': 'from-plain'}
        with patch.dict('os.environ', env, clear=True):
            self.assertTrue(check_password('from-hash'))
            self.assertFalse(check_password('from-plain'))

    def test_plaintext_fallback_and_nothing_configured(self):
        with patch.dict('os.environ', {'TASKS_OAUTH_PASSWORD': 'plain-fixture'}, clear=True):
            self.assertTrue(check_password('plain-fixture'))
            self.assertFalse(check_password('other'))
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(check_password(''), 'без пароля в env вхід закритий')

    def test_malformed_hash_never_matches(self):
        for bad in ('garbage', 'md5$1$00$00', 'pbkdf2_sha256$x$00$00', 'pbkdf2_sha256$1$zz$00'):
            with self.subTest(bad=bad), patch.dict('os.environ', {'TASKS_OAUTH_PASSWORD_HASH': bad}, clear=True):
                self.assertFalse(check_password('anything'))


if __name__ == '__main__':
    unittest.main()
