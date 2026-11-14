"""Спільне для тестів: тимчасовий planner, завантаження сервера з потрібним env і
мінімальний ASGI-клієнт (без мережі й без httpx — його немає в залежностях)."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Модулі, що читають env при імпорті (корінь planner, WIP-ліміт, мітка клієнта).
_SERVER_MODULES = ("task_manager", "web_ui", "planner_store", "planner_history", "planner_model")


def make_planner(root: Path) -> Path:
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / "tasks.md").write_text("# Tasks\n\n## 🔥 Активні\n\n## ✅ Done\n", encoding="utf-8")
    return root


class ServerCase(unittest.TestCase):
    """Кожен клас отримує свіжий planner і свіжо імпортований task_manager з ENV.

    env лишається пропатченим на весь клас: частину змінних сервер читає під час
    виклику (TASKS_TOKEN, хеш пароля), а не лише при імпорті."""

    ENV: dict[str, str] = {}

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = make_planner(Path(cls._tmp.name))
        env = {"TASKS_PLANNER_ROOT": str(cls.root), "TASKS_CLIENT": "local", **cls.ENV}
        cls._patches = [
            patch.dict("os.environ", env, clear=True),
            patch("dotenv.load_dotenv"),
            patch("tasks_oauth.TasksOAuthProvider._load"),
            patch("tasks_oauth.TasksOAuthProvider._save"),
        ]
        for p in cls._patches:
            p.start()
        for name in _SERVER_MODULES:
            sys.modules.pop(name, None)
        cls.tm = importlib.import_module("task_manager")

    @classmethod
    def tearDownClass(cls):
        for p in reversed(cls._patches):
            p.stop()
        for name in _SERVER_MODULES:
            sys.modules.pop(name, None)
        cls._tmp.cleanup()

    def setUp(self):
        # Кожен тест — з чистого planner (журнал теж).
        make_planner(self.root)
        for f in (self.root / "projects").glob("*.md"):
            f.unlink()
        hist = self.root / ".history"
        if hist.is_dir():
            for f in hist.iterdir():
                f.unlink()

    def call(self, tool: str, **args) -> str:
        """Виклик тула через MCP-інтерфейс (як це робить клієнт)."""
        res = asyncio.run(self.tm.mcp.call_tool(tool, args))
        self.assertFalse(res.is_error, res)
        return res.content[0].text

    def to_todo(self, tid: str) -> None:
        """backlog → todo напряму через стор: через MCP цей перехід заборонений (лише дашборд)."""
        self.tm.store.move_task(tid, "todo")

    def add(self, text="Задача", title="Задача", **kw) -> str:
        out = self.call("add_task", text=text, title=title, **kw)
        self.assertTrue(out.startswith("✅"), out)
        return out.split("`")[1]


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status_code, self.headers, self.body = status, headers, body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.body)


def request(app, method: str, path: str, **kw) -> Response:
    """Один HTTP-запит в ASGI-застосунок у процесі — сокет не відкривається."""
    return asyncio.run(arequest(app, method, path, **kw))


async def arequest(app, method: str, path: str, *, query: dict | None = None,
                   headers: dict | None = None, json_body=None, form: dict | None = None,
                   raw: bytes | None = None) -> Response:
    """Async-варіант: потрібен, коли запит іде всередині lifespan (MCP-сесії)."""
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs.setdefault("content-type", "application/json")
    elif form is not None:
        body = urlencode(form).encode()
        hdrs.setdefault("content-type", "application/x-www-form-urlencoded")
    elif raw is not None:
        body = raw
        hdrs.setdefault("content-type", "application/json")
    hdrs.setdefault("host", "127.0.0.1")
    scope = dict(
        type="http", asgi={"version": "3.0"}, http_version="1.1", method=method,
        scheme="http", path=path, raw_path=path.encode(),
        query_string=urlencode(query or {}).encode(), root_path="",
        server=("127.0.0.1", 80), client=("127.0.0.1", 1),
        headers=[(k.encode(), v.encode()) for k, v in hdrs.items()],
    )
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers_out = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Response(start["status"], headers_out, payload)


def capture_app(run) -> object:
    """run_http/run_oauth будують застосунок і віддають його uvicorn — перехоплюємо."""
    captured = {}
    with patch("uvicorn.run", lambda app, **kw: captured.setdefault("app", app)):
        run()
    return captured["app"]
