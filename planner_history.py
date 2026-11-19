"""
planner_history — журнал змін задач (append-only JSONL) + детектор ручних правок.
=================================================================================

Навіщо: у самому markdown видно лише ПОТОЧНИЙ стан. Хто, коли й що саме змінив —
не видно ніяк. Особливо після появи `edit_task`, який міняє будь-яке поле, включно
з текстом задачі: LLM може тихо переписати формулювання, і без журналу це не спіймати.

Розкладка (всередині `planner/`, бо це єдиний writable-корінь сервісу — отже само
потрапляє в бекап):
    planner/.history/YYYY-MM.jsonl   — по файлу на місяць, append-only, нічого не чистимо
    planner/.history/.state.json     — sha256 керованих файлів (для детекту ручних правок)

Формат запису (один JSON на рядок):
    {"ts": ISO8601, "op": ..., "tool": ..., "id": ..., "file": ...,
     "client": ..., "user": ..., "changes": {"поле": [було, стало]}, "snapshot": {...}}

`op`:
    add      — задачу створено (`changes` = поля з непорожніми значеннями)
    edit     — змінено поля (`changes` = лише те, що реально відрізняється)
    delete   — задачу видалено (`snapshot` = повний стан на момент видалення,
               бо операція незворотна і відновити більше нізвідки)
    external — файл змінився ПОВЗ сервіс (Obsidian, редактор, git). Полів не знаємо,
               але журнал чесно фіксує, що тут була стороння правка й він не повний.

`client` — ЗВІДКИ прийшла зміна: на OAuth-інстансі — клієнт токена (`claude` / `chatgpt`),
інакше env `TASKS_CLIENT` (systemd-юніти ставлять `mac` / `phone`); дашборд — `web`.
`user` — ХТО її зробив: `subject` з OAuth access-токена.

Це різні осі, і плутати їх не варто: `client` каже про пристрій/транспорт, `user` — про
людину. Сьогодні `user` завжди `vlad` (сервіс однокористувацький, `tasks_oauth.SUBJECT`),
але поле пишемо вже зараз: коли зʼявиться другий пароль,
старі записи лишаться коректними, а нові самі почнуть розрізняти людей — переписувати
журнал заднім числом не доведеться.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

HISTORY_DIRNAME = ".history"
STATE_FILENAME = ".state.json"

# Мітка клієнта за замовчуванням — з env, тобто ПРО ІНСТАНС:
# mac (bearer/tailnet) | phone (OAuth/Funnel) | local (stdio-розробка).
CLIENT = os.environ.get("TASKS_CLIENT", "local").strip() or "local"

# ...але один інстанс може обслуговувати різні джерела. Bearer-інстанс — це і MCP-виклики
# з Claude Code, і кліки у веб-дашборді; з env-міткою обидва були б `mac` і не відрізнялись.
# Тому джерело конкретного запиту кладемо в ContextVar: у asyncio кожен запит виконується
# у власній копії контексту, тож паралельні запити не переплутають мітки між собою.
_client_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tasks_client_source", default=None
)


@contextlib.contextmanager
def as_client(name: str):
    """Позначити всі записи журналу всередині блоку конкретним джерелом (напр. 'web')."""
    token = _client_ctx.set(name)
    try:
        yield
    finally:
        _client_ctx.reset(token)


# OAuth-інстанс обслуговує і Claude-конектор, і ChatGPT app — env-мітка `phone` їх не
# розрізняє. Тому OAuth-інстанс реєструє резолвер: client_id токена → мітка клієнта
# (`claude` / `chatgpt`, з його DCR client_name). Без токена або резолвера — env-мітка.
_token_client_resolver = None


def set_token_client_resolver(fn) -> None:
    """fn(client_id) -> str | None. Викликається на старті OAuth-інстансу."""
    global _token_client_resolver
    _token_client_resolver = fn


def _token_client() -> str | None:
    if _token_client_resolver is None:
        return None
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        tok = get_access_token()
        return _token_client_resolver(tok.client_id) if tok else None
    except Exception:
        return None


def current_client() -> str:
    """Джерело цього запиту: явно виставлене (напр. 'web') → клієнт OAuth-токена
    (claude/chatgpt) → мітка інстансу з env."""
    return _client_ctx.get() or _token_client() or CLIENT

# Дефолтний юзер, коли токена нема (stdio / bearer-транспорт — там особи не видно).
DEFAULT_USER = os.environ.get("TASKS_DEFAULT_USER", "vlad").strip() or "vlad"


def current_user(default: str = "") -> str:
    """Ім'я юзера з OAuth access-token.subject. У stdio/bearer токена нема → default."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        tok = get_access_token()
        if tok and getattr(tok, "subject", None):
            return tok.subject
    except Exception:
        pass
    return default or DEFAULT_USER


# Поля, які показуємо в діфі (порядок — як у to_dict).
_FIELDS = ("status", "text", "domains", "priority", "deadline", "energy", "links", "file", "plan")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def diff(before: dict, after: dict) -> dict:
    """{поле: [було, стало]} — лише поля, що реально змінились."""
    out: dict = {}
    for k in _FIELDS:
        b, a = before.get(k), after.get(k)
        if b != a:
            out[k] = [b, a]
    return out


def initial(after: dict) -> dict:
    """Для `add`: поля з непорожніми значеннями, щоб не сміттярити None/[] у журналі."""
    return {k: [None, after.get(k)] for k in _FIELDS if after.get(k) not in (None, [], "")}


class HistoryLog:
    def __init__(self, root: Path):
        self.dir = Path(root) / HISTORY_DIRNAME

    # --- запис -------------------------------------------------------------
    def _month_file(self, when: datetime | None = None) -> Path:
        when = when or datetime.now().astimezone()
        return self.dir / f"{when:%Y-%m}.jsonl"

    def _append(self, rec: dict) -> None:
        """Дописує рядок під flock. Журнал НІКОЛИ не має ламати саму операцію:
        якщо запис не вдався — мовчки пропускаємо (дані задач важливіші за аудит)."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self._month_file(), "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def record(
        self,
        op: str,
        *,
        tool: str,
        task_id: str,
        file: str,
        changes: dict | None = None,
        snapshot: dict | None = None,
    ) -> None:
        # Порожній діф не пишемо: «оновив, нічого не змінивши» — це шум.
        if op == "edit" and not changes:
            return
        rec = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "op": op,
            "tool": tool,
            "id": task_id,
            "file": file,
            "client": current_client(),
            "user": current_user(),
        }
        if changes:
            rec["changes"] = changes
        if snapshot is not None:
            rec["snapshot"] = snapshot
        self._append(rec)

    def record_external(self, file: str, before_hash: str, after_hash: str) -> None:
        """Файл змінився повз сервіс. Що саме — не знаємо, але мовчати про це не можна."""
        self._append(
            {
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "op": "external",
                "tool": "-",
                "id": "-",
                "file": file,
                "client": "-",
                "user": "-",   # правку зробили повз сервіс — особи не знаємо
                "changes": {"sha256": [before_hash[:12], after_hash[:12]]},
            }
        )

    # --- стан хешів (детект ручних правок) ---------------------------------
    @property
    def _state_file(self) -> Path:
        return self.dir / STATE_FILENAME

    def load_state(self) -> dict[str, str]:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def update_hashes(self, updates: dict[str, str]) -> None:
        """Read-modify-write під flock: два інстанси (bearer+oauth) ділять цей файл."""
        if not updates:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "a+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read()
                try:
                    state = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    state = {}
                state.update(updates)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(state, ensure_ascii=False, indent=0))
                f.flush()
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    # --- читання -----------------------------------------------------------
    def read(self, task_id: str | None = None, limit: int = 20) -> list[dict]:
        """Останні записи, найновіші спершу. Йде по місячних файлах від свіжих до старих
        і зупиняється, щойно набрав `limit`."""
        out: list[dict] = []
        if not self.dir.is_dir():
            return out
        for path in sorted(self.dir.glob("*.jsonl"), reverse=True):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                # external стосується файлу, не задачі — показуємо завжди:
                # саме він попереджає, що журнал по цій задачі може бути неповним.
                if task_id and rec.get("id") != task_id and rec.get("op") != "external":
                    continue
                out.append(rec)
                if len(out) >= limit:
                    return out
        return out
