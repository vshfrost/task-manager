"""
OAuth 2.1 authorization server для task-manager MCP (single-user).
==================================================================
Клон патерну brain-access/brain_oauth.py (той самий SDK-інтерфейс і hardening).
Реалізує інтерфейс OAuthAuthorizationServerProvider з MCP SDK 2.0.
Особливості:
  - Dynamic Client Registration (конектор Claude реєструється сам).
  - Гейт /login: код видається лише після правильного пароля (TASKS_OAUTH_PASSWORD).
  - Стан (клієнти/токени) персиститься у .oauth_state.json поруч із кодом (gitignored); з
    TASKS_STATE_DIR (сервер, юзер tasks) — у $TASKS_STATE_DIR/oauth_state.json, 0600.
  - Токени привʼязані до resource (RFC 8707) і scope рівно `tasks`; ResourceGuard на /token;
    прострочені/legacy-токени вичищаються на старті. Клієнти: Claude-конектор і ChatGPT app.

Захист: єдиний «юзер» — Влад. Пароль гейтить видачу коду навіть на публічному Funnel.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizeError,
    TokenError,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

CODE_TTL = 300          # 5 хв
ACCESS_TTL = 3600       # 1 год
REFRESH_TTL = 60 * 60 * 24 * 30  # 30 днів
SUBJECT = "vlad"
# Поруч із кодом; з TASKS_STATE_DIR (сервер: /var/lib/task-manager) — у каталозі стану сервісу.
STATE_FILE = (Path(os.environ["TASKS_STATE_DIR"]).expanduser() / "oauth_state.json"
              if os.environ.get("TASKS_STATE_DIR", "").strip()
              else Path(__file__).resolve().parent / ".oauth_state.json")

log = logging.getLogger(__name__)

# Журнал аудиту OAuth: подія, хто, який клієнт, куди йде код. Свій вивід у stderr → journald,
# щоб не залежати від налаштувань логування SDK. Без секретів: ні токенів, ні кодів, ні rid.
# Читати: journalctl -u <сервіс> | grep OAUTH_AUDIT
audit_log = logging.getLogger(f"{__name__}.audit")
if not audit_log.handlers:
    _audit_handler = logging.StreamHandler()
    _audit_handler.setFormatter(logging.Formatter("OAUTH_AUDIT %(message)s"))
    audit_log.addHandler(_audit_handler)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False

# Кому можна віддавати код (P2). Запис — точна адреса; «…/*» — рівно один сегмент-ID у кінці;
# «loopback» — http://127.0.0.1 | localhost | [::1] на будь-якому порту (CLI-логін Codex/Claude Code).
# Перевизначення без коду: TASKS_REDIRECT_ALLOWLIST=<через кому>.
REDIRECT_ALLOWLIST_ENV = "TASKS_REDIRECT_ALLOWLIST"
DEFAULT_REDIRECT_ALLOWLIST = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/connector/oauth/L5rDATRQa6U6",
    "loopback",
)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_ID_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}")


def redirect_allowlist() -> tuple[str, ...]:
    raw = os.environ.get(REDIRECT_ALLOWLIST_ENV, "")
    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    return items or DEFAULT_REDIRECT_ALLOWLIST


def redirect_allowed(uri: str, allowlist: tuple[str, ...]) -> bool:
    """Чи дозволена адреса повернення коду. Порівнюємо розібрані частини, а не рядок —
    щоб `https://claude.ai@evil/…`, чужий порт чи `../` у ID не пройшли."""
    try:
        u = urlsplit(str(uri))
        port = u.port
    except ValueError:
        return False
    if u.username is not None or u.password is not None or u.fragment or not u.hostname:
        return False
    for entry in allowlist:
        if entry == "loopback":
            if u.scheme == "http" and u.hostname in _LOOPBACK_HOSTS:
                return True
            continue
        e = urlsplit(entry)
        if (u.scheme, u.hostname, port, u.query) != (e.scheme, e.hostname, e.port, e.query):
            continue
        if e.path.endswith("/*"):
            base = e.path[:-1]
            if u.path.startswith(base) and _ID_SEGMENT.fullmatch(u.path[len(base):]):
                return True
        elif u.path == e.path:
            return True
    return False


class TasksRefreshToken(RefreshToken):
    resource: str | None = None


class TasksOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(
        self, issuer_url: str, resource_url: str, redirect_allow: tuple[str, ...] | None = None
    ) -> None:
        self.issuer = issuer_url.rstrip("/")
        self.resource = resource_url
        self.redirect_allow = redirect_allow or redirect_allowlist()
        self.clients: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}  # rid -> {client_id, params} (тільки в памʼяті)
        self._load()
        self._report_blocked_clients()

    # --- персистентність --------------------------------------------------
    def _load(self) -> None:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.clients = data.get("clients", {})
            self.access = data.get("access", {})
            self.refresh = data.get("refresh", {})
            before = (len(self.access), len(self.refresh))
            now = time.time()
            self.access = {
                token: value for token, value in self.access.items()
                if value.get("resource") == self.resource
                and set(value.get("scopes", [])) == {"tasks"}
                and value.get("expires_at", 0) >= now
            }
            self.refresh = {
                token: value for token, value in self.refresh.items()
                if value.get("resource") == self.resource
                and set(value.get("scopes", [])) == {"tasks"}
                and value.get("expires_at", 0) >= now
            }
            if before != (len(self.access), len(self.refresh)):
                self._save()

    def _save(self) -> None:
        """Атомарно й лише для власника (0600): тут живі токени."""
        data = json.dumps(
            {"clients": self.clients, "access": self.access, "refresh": self.refresh},
            ensure_ascii=False,
        ).encode("utf-8")
        STATE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, prefix=".oauth_state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, STATE_FILE)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # --- клієнти (DCR) ----------------------------------------------------
    def _client_allowed(self, d: dict) -> bool:
        uris = d.get("redirect_uris") or []
        return bool(uris) and all(redirect_allowed(u, self.redirect_allow) for u in uris)

    def _report_blocked_clients(self) -> None:
        """Клієнти зі стейту, чиї redirect_uri поза allowlist, для сервера не існують.
        Записи не видаляємо (відкат — змінною оточення); у журнал — лише назва й хост."""
        blocked = [d for d in self.clients.values() if not self._client_allowed(d)]
        if blocked:
            log.warning(
                "OAuth: %d клієнт(ів) поза allowlist redirect_uri — заблоковано: %s",
                len(blocked),
                ", ".join(
                    f"{str(d.get('client_name') or d.get('client_id'))[:40]!r} → "
                    f"{urlsplit(str((d.get('redirect_uris') or [''])[0])).hostname}"
                    for d in blocked
                ),
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        d = self.clients.get(client_id)
        if not d or not self._client_allowed(d):
            return None  # невідомий або поза allowlist: ні authorize, ні token, ні refresh
        return OAuthClientInformationFull(**d)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or not all(redirect_allowed(u, self.redirect_allow) for u in uris):
            self.audit("register_rejected", client_id=client_info.client_id,
                       name=client_info.client_name, redirect_uri=uris[0] if uris else None)
            raise RegistrationError(
                "invalid_redirect_uri", "redirect_uri is not on this server's allowlist"
            )
        self.clients[client_info.client_id] = client_info.model_dump(mode="json")
        self._save()
        self.audit("register", client_id=client_info.client_id)

    def describe_pending(self, rid: str) -> dict | None:
        """Для екрана згоди на /login: хто просить доступ і куди піде код. Без секретів."""
        pend = self.pending.get(rid)
        if not pend:
            return None
        client = self.clients.get(pend["client_id"], {})
        return {
            "client_name": client.get("client_name") or pend["client_id"],
            "redirect_host": urlsplit(str(pend["params"].get("redirect_uri") or "")).hostname or "?",
            "registered_at": client.get("client_id_issued_at"),
        }

    def client_label(self, client_id: str | None) -> str | None:
        """Мітка джерела для журналу задач з `client_name`, який клієнт назвав при DCR
        (ChatGPT → `chatgpt`, Claude → `claude`). Назву задає сам клієнт на публічному
        /register, тож це лише підпис: санітизуємо до [a-z0-9-] і ≤20 символів."""
        name = (self.clients.get(client_id or "") or {}).get("client_name") or ""
        label = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20].strip("-")
        return label or None

    def audit(
        self, event: str, *, client_id: str | None = None, subject: str | None = None,
        redirect_uri: object = None, name: str | None = None,
    ) -> None:
        """Рядок журналу аудиту. Без секретів: лише подія, хто, клієнт і куди йде код."""
        client = self.clients.get(client_id or "", {})
        uri = redirect_uri or (client.get("redirect_uris") or [None])[0]
        u = urlsplit(str(uri or ""))
        audit_log.info(
            "event=%s user=%s client=%r client_id=%s redirect=%s",
            event, subject or "-", str(name or client.get("client_name") or "-")[:40],
            (client_id or "-")[:8], f"{u.hostname}{u.path}" if u.hostname else "-",
        )

    def audit_pending(self, event: str, rid: str) -> None:
        """Аудит події на /login за rid (сам rid у журнал не йде)."""
        pend = self.pending.get(rid)
        if pend:
            self.audit(event, client_id=pend["client_id"],
                       redirect_uri=pend["params"].get("redirect_uri"))
        else:
            self.audit(event)

    # --- authorize: віддаємо на власний login-гейт ------------------------
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource is not None and params.resource != self.resource:
            raise AuthorizeError("invalid_target", "Unsupported resource")
        if set(params.scopes or ["tasks"]) != {"tasks"}:
            raise AuthorizeError("invalid_scope", "Unsupported scope")
        params = params.model_copy(update={"resource": self.resource})
        rid = secrets.token_urlsafe(24)
        self.pending[rid] = {
            "client_id": client.client_id,
            "params": params.model_dump(mode="json"),
        }
        return f"{self.issuer}/login?rid={rid}"

    def complete_login(self, rid: str) -> str:
        """Викликається login-роутом ПІСЛЯ вірного пароля. Повертає фінальний
        redirect_uri назад у клієнт (Claude) з видачею auth-code."""
        pend = self.pending.pop(rid, None)
        if not pend:
            raise KeyError("rid")
        params = AuthorizationParams(**pend["params"])
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or ["tasks"],
            expires_at=int(time.time()) + CODE_TTL,
            client_id=pend["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=SUBJECT,
        ).model_dump(mode="json")
        self.audit("login", client_id=pend["client_id"], subject=SUBJECT,
                   redirect_uri=params.redirect_uri)
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    # --- authorization code ----------------------------------------------
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        d = self.codes.get(authorization_code)
        if not d or d["client_id"] != client.client_id:
            return None
        if d["expires_at"] < time.time():
            self.codes.pop(authorization_code, None)
            return None
        return AuthorizationCode(**d)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        stored = await self.load_authorization_code(client, authorization_code.code)
        if stored is None:
            raise TokenError("invalid_grant", "Invalid authorization code")
        self._validate_grant(stored.resource, stored.scopes)
        self.codes.pop(stored.code)
        return self._issue(client.client_id, stored.scopes, stored.resource)

    # --- refresh token ----------------------------------------------------
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        d = self.refresh.get(refresh_token)
        if not d or d["client_id"] != client.client_id:
            return None
        if d["expires_at"] < time.time():
            self.refresh.pop(refresh_token, None)
            self._save()
            return None
        if d.get("resource") != self.resource or set(d["scopes"]) != {"tasks"}:
            return None
        return TasksRefreshToken(**d)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        stored = await self.load_refresh_token(client, refresh_token.token)
        if stored is None:
            raise TokenError("invalid_grant", "Invalid refresh token")
        use_scopes = scopes or list(stored.scopes)
        self._validate_grant(stored.resource, use_scopes)
        if not set(use_scopes).issubset(stored.scopes):
            raise TokenError("invalid_scope", "Scope escalation is forbidden")
        self.refresh.pop(stored.token)
        self.audit("refresh", client_id=client.client_id, subject=SUBJECT)
        return self._issue(client.client_id, use_scopes, stored.resource)

    # --- access token -----------------------------------------------------
    async def load_access_token(self, token: str) -> AccessToken | None:
        d = self.access.get(token)
        if not d:
            return None
        if d["expires_at"] < time.time():
            self.access.pop(token, None)
            self._save()
            return None
        if d.get("resource") != self.resource or set(d["scopes"]) != {"tasks"}:
            return None
        return AccessToken(**d)

    async def revoke_token(self, token) -> None:
        t = getattr(token, "token", token)
        self.access.pop(t, None)
        self.refresh.pop(t, None)
        self._save()

    async def exchange_identity_assertion(self, *args, **kwargs):
        raise NotImplementedError("identity assertion не підтримується")

    # --- helpers ----------------------------------------------------------
    def _validate_grant(self, resource: str | None, scopes: list[str]) -> None:
        if resource != self.resource:
            raise TokenError("invalid_target", "Unsupported resource")
        if set(scopes) != {"tasks"}:
            raise TokenError("invalid_scope", "Unsupported scope")

    def _issue(self, client_id: str, scopes: list[str], resource: str) -> OAuthToken:
        self._validate_grant(resource, scopes)
        at = secrets.token_urlsafe(32)
        rt = secrets.token_urlsafe(32)
        now = int(time.time())
        self.access[at] = AccessToken(
            token=at, client_id=client_id, scopes=scopes,
            expires_at=now + ACCESS_TTL, subject=SUBJECT, resource=resource,
        ).model_dump(mode="json")
        self.refresh[rt] = TasksRefreshToken(
            token=rt, client_id=client_id, scopes=scopes,
            expires_at=now + REFRESH_TTL, subject=SUBJECT, resource=resource,
        ).model_dump(mode="json")
        self._save()
        return OAuthToken(
            access_token=at, token_type="Bearer", expires_in=ACCESS_TTL,
            scope=" ".join(scopes), refresh_token=rt,
        )


def hash_password(password: str, *, iters: int = 600_000, salt: bytes | None = None) -> str:
    """Повертає рядок 'pbkdf2_sha256$iters$salt_hex$hash_hex'."""
    import hashlib

    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${salt.hex()}${dk.hex()}"


def check_password(candidate: str) -> bool:
    """Constant-time перевірка. Пріоритет — хеш (TASKS_OAUTH_PASSWORD_HASH);
    fallback на plaintext (TASKS_OAUTH_PASSWORD) для сумісності."""
    import hashlib

    stored = os.environ.get("TASKS_OAUTH_PASSWORD_HASH", "")
    if stored:
        try:
            algo, iters_s, salt_hex, hash_hex = stored.split("$")
            assert algo == "pbkdf2_sha256"
            dk = hashlib.pbkdf2_hmac(
                "sha256", candidate.encode(), bytes.fromhex(salt_hex), int(iters_s)
            )
            return hmac.compare_digest(dk.hex(), hash_hex)
        except (ValueError, AssertionError):
            return False
    plain = os.environ.get("TASKS_OAUTH_PASSWORD", "")
    return bool(plain) and hmac.compare_digest(candidate, plain)


class ResourceGuard:
    """Validate token resource indicators omitted by the SDK's provider interface.

    Pure ASGI: replay the body unchanged to the SDK for client auth and PKCE.
    Caddy strips the external /tasks prefix before forwarding /token.
    """
    def __init__(self, app, resource: str):
        self.app = app
        self.resource = resource

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] != '/token' or scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        from urllib.parse import parse_qs
        from starlette.responses import JSONResponse

        messages = []
        body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > 65536:
                return await JSONResponse({'error': 'invalid_request'}, status_code=413)(scope, receive, send)
            if not message.get('more_body', False):
                break
        try:
            form = parse_qs(body.decode('utf-8'), keep_blank_values=True)
        except UnicodeDecodeError:
            return await JSONResponse({'error': 'invalid_request'}, status_code=400)(scope, receive, send)
        resources = form.get('resource')
        if resources is not None and resources != [self.resource]:
            return await JSONResponse(
                {'error': 'invalid_target'}, status_code=400,
                headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'},
            )(scope, receive, send)

        async def replay():
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)
