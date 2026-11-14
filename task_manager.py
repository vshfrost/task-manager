"""
task-manager — MCP-сервер керування задачами над доменом `~/AI/planner/`.
========================================================================

Тули: list_tasks · get_task · board · add_task · move_task (правила переходів + WIP-гард) · set_priority ·
set_deadline · delete_task. Джерело правди — markdown-файли planner.

Транспорти (патерн brain-access):
  stdio (за замовч.)              — локальний Claude Code на Mac / розробка
  --http  / TASKS_TRANSPORT=http  — HTTP+bearer, :8792, лише tailnet (Mac)
  --oauth / TASKS_TRANSPORT=oauth — HTTP+OAuth, :8793, публічно за Caddy/Funnel
                                    (Claude-конектор на телефоні/вебі + ChatGPT app)

Корінь planner:  env TASKS_PLANNER_ROOT (за замовч. ~/AI/planner)
WIP-ліміт:       env TASKS_WIP_LIMIT (за замовч. 15)
"""
from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from planner_store import MAX_IN_PROGRESS, UNSET, PlannerError, PlannerStore

# Людяний шлях MCP-ендпоінта (Caddy роутить сюди), як /brain і /media.
TASKS_HTTP_PATH = os.environ.get("TASKS_HTTP_PATH", "/tasks")

# Інструкції будь-якому клієнту (телефон/ПК), що вмикає цей конектор: лише про роботу
# з задачами — жодних згадок інших конекторів чи правил (їх клієнт бере не звідси).
INSTRUCTIONS = (
    "Vlad's task manager: create, update and track tasks and projects "
    "(markdown in `planner/` is the source of truth; these tools are the only way to change it). "
    "Start with `board` or `list_tasks` to see the current state. Statuses: "
    "backlog | todo | blockers | in_progress | in_review | testing | done; "
    "new tasks always go to backlog; moving into in_progress is capped at 15 (WIP limit). "
    "Transitions: into todo only from testing, in_progress, in_review or blockers "
    "(backlog to todo is not possible via MCP — only Vlad does it on the dashboard); "
    "into blockers only from backlog, todo or testing; into in_progress only from todo; "
    "into in_review only from in_progress; into testing only from in_progress, in_review or blockers; "
    "into backlog and done from anywhere. "
    "A task returning to todo from in_progress or in_review goes to the top of the column, "
    "from testing or blockers to the end. "
    "Every task needs a short `title` (<=50 chars) — it is what the board and calendar show; "
    "`text` stays the full wording. "
    "Use `links` on add_task for the note the task came from, e.g. projects/<name>/overview."
)

_OAUTH = os.environ.get("TASKS_TRANSPORT") == "oauth" or "--oauth" in sys.argv
OAUTH_PROVIDER = None

if _OAUTH:
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

    from tasks_oauth import TasksOAuthProvider, check_password

    _ISSUER = os.environ.get("TASKS_ISSUER_URL", "http://localhost:8793")
    # За Caddy prefix-routing issuer уже містить /tasks → resource = issuer.
    _RESOURCE = os.environ.get(
        "TASKS_RESOURCE_URL",
        _ISSUER if _ISSUER.rstrip("/").endswith("/tasks") else f"{_ISSUER}/tasks",
    )
    OAUTH_PROVIDER = TasksOAuthProvider(_ISSUER, _RESOURCE)

    # Журнал: `client` = хто саме з OAuth-клієнтів (claude/chatgpt), а не просто `phone`.
    from planner_history import set_token_client_resolver

    set_token_client_resolver(OAUTH_PROVIDER.client_label)
    mcp = MCPServer(
        "task-manager",
        instructions=INSTRUCTIONS,
        auth_server_provider=OAUTH_PROVIDER,
        auth=AuthSettings(
            issuer_url=_ISSUER,
            resource_server_url=_RESOURCE,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["tasks"], default_scopes=["tasks"]
            ),
            required_scopes=["tasks"],
        ),
    )

    import html

    from starlette.requests import Request
    from starlette.responses import HTMLResponse, RedirectResponse

    # Заголовки сторінки входу: жодних скриптів, фреймів чи витоку rid через Referer.
    # form-action НЕ ставимо: Chrome застосовує його й до 302 після POST, а успішний вхід
    # редиректить на redirect_uri клієнта (claude.ai / chatgpt.com / loopback).
    _LOGIN_HEADERS = {
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }

    def _consent_html(info: dict) -> str:
        """Екран згоди: хто просить доступ, куди піде код, коли клієнта зареєстровано."""
        import time as _t

        ts = info.get("registered_at")
        if ts:
            when = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts))
            new = " 🆕 <b>новий клієнт</b>" if _t.time() - ts < 86400 else ""
        else:
            when, new = "невідомо", ""
        return (
            "<p>Доступ просить: <b>{name}</b><br>Код піде на: <b>{host}</b><br>"
            "Клієнт зареєстрований: {when}{new}</p>"
            "<p style='color:#a60'>Якщо ти щойно сам не підключав цей конектор — не вводь пароль.</p>"
        ).format(
            name=html.escape(str(info["client_name"])[:80]),
            host=html.escape(str(info["redirect_host"])),
            when=when,
            new=new,
        )

    def _login_page(rid: str, err: str = "", status: int = 200) -> "HTMLResponse":
        """Сторінка входу. Форма — лише для живого запиту (rid, який видав цей сервер) і
        разом з екраном згоди; невідомий rid не відображається зовсім. err — наш HTML."""
        info = OAUTH_PROVIDER.describe_pending(rid) if rid else None
        if info:
            main = _consent_html(info) + _LOGIN_FORM.format(rid=html.escape(rid, quote=True))
        elif rid:
            main = "<p>Запит на вхід застарів або невідомий. Почни підключення конектора знову.</p>"
        else:
            main = ""
        return HTMLResponse(
            _LOGIN_HTML.format(err=err, main=main), status_code=status, headers=_LOGIN_HEADERS
        )

    _LOGIN_HTML = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>planner — вхід</title></head>"
        "<body style='font-family:sans-serif;max-width:420px;margin:15vh auto;padding:0 1rem'>"
        "<h2>📋 Доступ до задач</h2>{err}{main}</body></html>"
    )
    _LOGIN_FORM = (
        # action відносний ('login'), щоб за Caddy handle_path (/tasks стрипиться) форма
        # постила на /tasks/login → бекенд /login, а не на кореневий /login (404).
        "<form method='post' action='login'>"
        "<input type='hidden' name='rid' value='{rid}'>"
        "<input type='password' name='password' placeholder='Пароль' autofocus "
        "style='width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box'>"
        "<button style='margin-top:.8rem;padding:.6rem 1.2rem;font-size:1rem'>Увійти</button>"
        "</form>"
    )

    # Rate-limit логіну — ГЛОБАЛЬНИЙ: за Funnel реальний IP клієнта замаскований (127.0.0.1),
    # тож по-IP лічити нічого. Легітимний логін рідкісний (refresh живе 30 днів).
    _login_state = {"fails": 0, "locked_until": 0.0}
    _LOGIN_MAX_FAILS = 5
    _LOGIN_LOCK_SECONDS = 900  # 15 хв

    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: "Request"):
        import time as _t

        if request.method == "GET":
            rid = request.query_params.get("rid", "")
            return _login_page(rid)

        now = _t.time()
        if now < _login_state["locked_until"]:
            wait_min = int(_login_state["locked_until"] - now) // 60 + 1
            OAUTH_PROVIDER.audit("login_locked")
            return _login_page(
                "",
                err=f"<p style='color:#c00'>Забагато спроб. Спробуй за ~{wait_min} хв.</p>",
                status=429,
            )

        form = await request.form()
        rid = str(form.get("rid", ""))
        if not check_password(str(form.get("password", ""))):
            _login_state["fails"] += 1
            if _login_state["fails"] >= _LOGIN_MAX_FAILS:
                _login_state["locked_until"] = now + _LOGIN_LOCK_SECONDS
                _login_state["fails"] = 0
            OAUTH_PROVIDER.audit_pending("login_failed", rid)
            return _login_page(rid, err="<p style='color:#c00'>Невірний пароль</p>", status=401)

        _login_state["fails"] = 0
        _login_state["locked_until"] = 0.0
        try:
            target = OAUTH_PROVIDER.complete_login(rid)
        except KeyError:
            return HTMLResponse(
                "Сесія авторизації застаріла. Почни знову.", status_code=400, headers=_LOGIN_HEADERS
            )
        return RedirectResponse(target, status_code=302)
else:
    mcp = MCPServer("task-manager", instructions=INSTRUCTIONS)

store = PlannerStore()

# Веб-дашборд (канбан + календар) — ТІЛЬКИ на bearer/stdio інстансі.
# На OAuth-інстансі його реєструвати НЕ МОЖНА: той стоїть за Caddy/Funnel, і будь-який
# його маршрут стає публічним, а дашборд власної авторизації не має (захист — tailnet
# + фаєрвол). Умова нижче — перший запобіжник, assert у web_ui.register — другий.
if not _OAUTH:
    import web_ui

    web_ui.register(mcp, store, is_oauth=False)

# Анотації тулів: за ними ChatGPT (і Claude) вирішують, що питати на підтвердження.
# edit_task — деструктивний: очищає поля ('none') і фізично переносить задачу між файлами.
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
)
# move/set_* — повтор з тим самим значенням нічого не змінює.
_SET = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
)
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
)

_STATUS_LABEL = {
    "backlog": "backlog", "todo": "to do", "blockers": "blockers", "in_progress": "in progress",
    "in_review": "in review", "testing": "testing", "done": "done",
}
_PRIO_LABEL = {"asap": "🔺 asap", "high": "⏫ high", "low": "🔽 low", None: "—"}


def _split(s: str) -> list[str]:
    if not s:
        return []
    return [x.strip().lstrip("#") for x in s.replace(",", " ").split() if x.strip()]


def _opt(s: str | None):
    """Три стани поля для edit_task: '' → UNSET (не чіпати); 'none'/'-' → None (очистити);
    інакше — саме значення."""
    if s is None or s == "":
        return UNSET
    return None if s.strip().lower() in ("none", "-", "—") else s.strip()


def _opt_list(s: str | None):
    """Те саме для списків: '' → UNSET; 'none'/'-' → [] (очистити); інакше — розбитий список."""
    if s is None or s == "":
        return UNSET
    return [] if s.strip().lower() in ("none", "-", "—") else _split(s)


def _plan_opt(s: str | None):
    """План для edit_task: '' → UNSET (не чіпати); решту віддаємо як є, БЕЗ обрізання
    (багаторядковий markdown зберігається байт-у-байт); 'none' стор сам трактує як «прибрати»."""
    return UNSET if not s else s


def _none(s: str | None) -> str | None:
    """'', 'none', '-' → None; інакше сам рядок."""
    if s is None:
        return None
    s = s.strip()
    return None if s.lower() in ("", "none", "-", "—") else s


def _fmt(t: dict) -> str:
    bits = [f"`{t['id']}`", f"[{_STATUS_LABEL[t['status']]}]"]
    # Заголовок показуємо лише тоді, коли він реально коротший за текст — інакше
    # у виводі двічі стояв би один і той самий рядок.
    if t.get("title") and t["title"] != t["text"]:
        bits.append(f"📌 {t['title']} ·")
    bits.append(t["text"])
    meta = []
    if t["priority"]:
        meta.append(_PRIO_LABEL[t["priority"]])
    if t["deadline"]:
        meta.append(f"📅 {t['deadline']}")
    if t["energy"]:
        meta.append(f"⚡{t['energy']}")
    if t["domains"]:
        meta.append(" ".join(f"#{d}" for d in t["domains"]))
    proj = t["file"].replace("projects/", "").replace(".md", "") if t["file"] != "tasks.md" else "global"
    meta.append(f"@{proj}")
    if meta:
        bits.append("· " + " ".join(meta))
    return " ".join(bits)


def _fmt_short(t: dict) -> str:
    """Рядок для списків і відповідей запису: без повного тексту (його дає get_task)."""
    title = t.get("title") or t["text"]
    if len(title) > 50:
        title = title[:49] + "…"
    meta = []
    if t["priority"]:
        meta.append(_PRIO_LABEL[t["priority"]])
    if t["deadline"]:
        meta.append(f"📅 {t['deadline']}")
    proj = t["file"].replace("projects/", "").replace(".md", "") if t["file"] != "tasks.md" else "global"
    meta.append(f"@{proj}")
    return f"`{t['id']}` [{_STATUS_LABEL[t['status']]}] {title} · {' '.join(meta)}"


def _val(v) -> str:
    """Значення поля для журналу: порожнє → «—», список → через кому."""
    if v is None or v == [] or v == "":
        return "—"
    return ", ".join(v) if isinstance(v, list) else str(v)


def _fmt_rec(r: dict) -> str:
    ts = r.get("ts", "")[:16].replace("T", " ")
    if r.get("op") == "external":
        return f"{ts} · ⚠️ СТОРОННЯ ПРАВКА повз сервіс: {r.get('file')} (що саме — невідомо)"
    who = r.get("user", "?")
    head = f"{ts} · {who}@{r.get('client', '?')} · {r.get('tool', '?')} `{r.get('id', '?')}`"
    lines = [head]
    if r.get("op") == "delete":
        snap = r.get("snapshot") or {}
        lines.append(f"    🗑️ видалено: {snap.get('text', '')}")
        return "\n".join(lines)
    for field_, (was, now) in (r.get("changes") or {}).items():
        lines.append(f"    {field_}: {_val(was)} → {_val(now)}")
    return "\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), structured_output=False)
def list_tasks(
    status: str = "", project: str = "", priority: str = "", domain: str = "",
    query: str = "", limit: int = 50,
) -> str:
    """Список задач із planner (короткі рядки: id, статус, заголовок, пріоритет, дедлайн, проєкт).
    Без status задачі done не показуються (внизу — скільки приховано); status=done — лише вони.
    Фільтри опційні: status = backlog|todo|blockers|in_progress|in_review|testing|done; project = слаг проєкту або 'global';
    priority = asap|high|low; domain = тег без '#'; query = підрядок у заголовку чи тексті.
    status=todo — у ручному порядку колонки To Do (зверху те, що брати першим), не за пріоритетом.
    limit — скільки задач показати (за замовч. 50). Повний текст і links — get_task(id)."""
    try:
        tasks = store.list_tasks(
            status=_none(status),
            project=_none(project),
            priority=_none(priority),
            domain=_none(domain),
            query=_none(query),
        )
    except PlannerError as e:
        return f"⚠️ {e}"
    hidden = 0
    if not _none(status):
        hidden = sum(1 for t in tasks if t["status"] == "done")
        tasks = [t for t in tasks if t["status"] != "done"]
    notes = []
    if hidden:
        notes.append(f"(+{hidden} done приховано — `status=done`)")
    if not tasks:
        return " ".join(["Порожньо (за цими фільтрами задач немає)."] + notes)
    cap = max(1, min(int(limit or 50), 500))
    rest = len(tasks) - cap
    lines = [_fmt_short(t) for t in tasks[:cap]]
    if rest > 0:
        lines.append(f"(ще {rest} — звузь фільтр або збільш `limit`)")
    return "\n".join(lines + notes)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), structured_output=False)
def get_task(id: str) -> str:
    """Одна задача повністю: текст, пріоритет, дедлайн, energy, domains, проєкт, links, plan."""
    try:
        t = store.get_task(id.strip())
    except PlannerError as e:
        return f"⚠️ {e}"
    out = _fmt(t)
    if t["links"]:
        out += "\n🔗 " + ", ".join(t["links"])
    if t.get("plan"):
        out += "\n\n📝 План:\n" + t["plan"]
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), structured_output=False)
def board() -> str:
    """Зведення по колонках канбану (скільки задач у кожному статусі) + перевірка WIP."""
    try:
        all_t = store.list_tasks()
    except PlannerError as e:
        return f"⚠️ {e}"
    counts = {k: 0 for k in _STATUS_LABEL}
    for t in all_t:
        counts[t["status"]] += 1
    ip = counts["in_progress"]
    wip = f"{ip}/{MAX_IN_PROGRESS}" + (" ⚠️ перевищено" if ip > MAX_IN_PROGRESS else "")
    head = (
        f"📋 backlog: {counts['backlog']} · to do: {counts['todo']} · "
        f"blockers: {counts['blockers']} · in progress: {wip} · "
        f"in review: {counts['in_review']} · testing: {counts['testing']} · "
        f"done: {counts['done']} · усього: {len(all_t)}"
    )
    active = [
        f"{mark} {_STATUS_LABEL[st]}: `{t['id']}` {t.get('title') or t['text']}"
        for st, mark in (("in_progress", "▶"), ("in_review", "🔎"), ("testing", "🧪"))
        for t in all_t if t["status"] == st
    ]
    return "\n".join([head, *active])


@mcp.tool(annotations=_WRITE, structured_output=False)
def add_task(
    text: str,
    title: str,
    project: str = "",
    priority: str = "",
    deadline: str = "",
    energy: str = "",
    domains: str = "",
    links: str = "",
    plan: str = "",
) -> str:
    """Додати задачу. text і title — ОБОВʼЯЗКОВІ. project — слаг проєкту (порожньо = глобальний).
    title — КОРОТКИЙ заголовок (≤50 символів), саме він видно на канбані й у календарі:
    стисни суть задачі, а не обрізай текст посередині.
    priority = asap|high|low; deadline = РРРР-ММ-ДД; energy = S|M|L; domains/links — через кому/пробіл.
    Нова задача завжди потрапляє в backlog. text — ОДИН рядок (з переносом — помилка);
    багаторядковий опис — у plan (markdown; зберігається окремо, видно в get_task).\n\n    `links` — шлях нотатки, з якої виросла задача, напр. 'projects/task-manager/overview'.\n    ЗАВЖДИ проставляй його, коли задача походить із конкретної нотатки — без цього задача\n    втрачає свій контекст."""
    try:
        t = store.add_task(
            text,
            title=_none(title),
            project=_none(project),
            priority=_none(priority),
            deadline=_none(deadline),
            energy=_none(energy),
            domains=_split(domains),
            links=_split(links),
            plan=plan or None,
        )
    except PlannerError as e:
        return f"⚠️ {e}"
    return "✅ Додано: " + _fmt_short(t)


@mcp.tool(annotations=_SET, structured_output=False)
def move_task(id: str, status: str) -> str:
    """Перемістити задачу в колонку: status = backlog|todo|blockers|in_progress|in_review|testing|done.
    Переходи: у todo — лише з testing, in_progress, in_review або blockers (backlog → todo через
    MCP заборонено, це робить лише Влад на дашборді); у blockers — лише з backlog, todo або testing;
    у in_progress — лише з todo; у in_review — лише з in_progress; у testing — лише з in_progress,
    in_review або blockers; у backlog і done — звідки завгодно. З in_progress/in_review у todo
    задача стає першою в колонці, з testing/blockers — останньою.
    Перехід у in_progress стереже WIP-ліміт (≤15)."""
    try:
        t = store.move_task(id.strip(), status.strip(), via_mcp=True)
    except PlannerError as e:
        return f"⚠️ {e}"
    return "✅ Переміщено: " + _fmt_short(t)


@mcp.tool(annotations=_SET, structured_output=False)
def set_priority(id: str, priority: str) -> str:
    """Змінити пріоритет: asap|high|low, або 'none' щоб зняти."""
    try:
        t = store.set_priority(id.strip(), _none(priority))
    except PlannerError as e:
        return f"⚠️ {e}"
    return "✅ Оновлено: " + _fmt_short(t)


@mcp.tool(annotations=_SET, structured_output=False)
def set_deadline(id: str, deadline: str) -> str:
    """Змінити дедлайн: РРРР-ММ-ДД, або 'none' щоб зняти."""
    try:
        t = store.set_deadline(id.strip(), _none(deadline))
    except PlannerError as e:
        return f"⚠️ {e}"
    return "✅ Оновлено: " + _fmt_short(t)


@mcp.tool(annotations=_DESTRUCTIVE, structured_output=False)
def edit_task(
    id: str,
    text: str = "",
    title: str = "",
    status: str = "",
    priority: str = "",
    deadline: str = "",
    energy: str = "",
    domains: str = "",
    links: str = "",
    project: str = "",
    plan: str = "",
) -> str:
    """Змінити БУДЬ-ЯКІ поля задачі за один виклик (універсальний редактор).

    Передавай лише те, що змінюєш — порожнє поле означає «не чіпати».
    title — короткий заголовок (≤50 символів) для канбану/календаря; єдине поле, яке
    НЕ можна очистити через 'none' (він обовʼязковий).
    Щоб ОЧИСТИТИ поле, передай 'none'. Значення: status = backlog|todo|blockers|in_progress|in_review|testing|done
    (переходи: у todo — лише з testing, in_progress, in_review або blockers; backlog → todo через
    MCP заборонено, це робить лише Влад на дашборді; у blockers — лише з backlog, todo або testing;
    у in_progress — лише з todo; у in_review — лише з in_progress; у testing — лише з in_progress,
    in_review або blockers; у backlog і done — звідки завгодно);
    priority = asap|high|low; deadline = РРРР-ММ-ДД; energy = S|M|L; domains/links — через
    кому/пробіл; project = слаг проєкту або 'global' (фізично переносить задачу між файлами).
    text — ОДИН рядок (з переносом — помилка); plan — багаторядковий markdown-план задачі
    (окремий файл, не рядок задачі; 'none' прибирає; його бачить get_task).

    `links` — шлях нотатки, з якої виросла задача, напр. 'projects/task-manager/overview'.
    Саме цим задача зшивається зі своїм контекстом, тож проставляй його завжди, коли задача
    виросла з конкретної нотатки. Перехід у in_progress стереже WIP-ліміт (≤15)."""
    try:
        t = store.edit_task(
            id.strip(),
            text=_opt(text),
            title=_opt(title),
            status=_opt(status),
            priority=_opt(priority),
            deadline=_opt(deadline),
            energy=_opt(energy),
            domains=_opt_list(domains),
            links=_opt_list(links),
            project=_opt(project),
            plan=_plan_opt(plan),
            via_mcp=True,
        )
    except PlannerError as e:
        return f"⚠️ {e}"
    return "✅ Оновлено: " + _fmt_short(t)


@mcp.tool(annotations=_DESTRUCTIVE, structured_output=False)
def delete_task(id: str) -> str:
    """Видалити задачу за id (безповоротно з файлу planner)."""
    try:
        t = store.delete_task(id.strip())
    except PlannerError as e:
        return f"⚠️ {e}"
    return "🗑️ Видалено:\n" + _fmt(t)


@mcp.tool(annotations=_WRITE, structured_output=False)
def create_project(slug: str, name: str, repo: str = "") -> str:
    """Заглушка."""
    return ""


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), structured_output=False)
def list_projects() -> str:
    """Заглушка."""
    return ""


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), structured_output=False)
def history(id: str = "", limit: int = 20) -> str:
    """Журнал змін задач: хто, коли і ЩО САМЕ змінив. Найновіші — зверху.

    id — показати історію однієї задачі (порожньо = усі зміни підряд);
    limit — скільки записів (за замовч. 20).

    Позначки: `add` створено · `edit` змінено (видно було→стало по кожному полю) ·
    `delete` видалено (зі знімком, бо відновити більше нізвідки) ·
    ⚠️ `стороння правка` — файл змінили повз сервіс (Obsidian/редактор), полів не знаємо.
    Підпис `юзер@клієнт`: хто зробив зміну і звідки (web | mac | claude | chatgpt | phone | local)."""
    try:
        recs = store.history.read(_none(id), max(1, min(int(limit or 20), 200)))
    except (PlannerError, ValueError) as e:
        return f"⚠️ {e}"
    if not recs:
        return "Журнал порожній (за цим фільтром змін не було)."
    return "\n".join(_fmt_rec(r) for r in recs)


# --- Транспорти ---------------------------------------------------------------
def _transport_security(port: int):
    from mcp.server.transport_security import TransportSecuritySettings

    def _csv(name: str) -> list[str]:
        return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]

    return TransportSecuritySettings(
        allowed_hosts=[
            "127.0.0.1", f"127.0.0.1:{port}", "localhost", f"localhost:{port}",
            *_csv("TASKS_ALLOWED_HOSTS"),
        ],
        allowed_origins=["*", *_csv("TASKS_ALLOWED_ORIGINS")],
    )


def run_http() -> None:
    """HTTP (streamable-http) + bearer. Тільки tailnet — назовні НЕ виставляти."""
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    token = os.environ.get("TASKS_TOKEN")
    if not token:
        sys.exit("TASKS_TOKEN не задано. Створи task-manager/.env з TASKS_TOKEN=<секрет>.")

    host = os.environ.get("TASKS_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("TASKS_HTTP_PORT", "8792"))
    bearer = "Bearer "

    async def auth(request, call_next):
        # Bearer вимагаємо лише на MCP-ендпоінті. Дашборд (/task-manager, /api/*) браузер відкриває
        # без заголовків — його захищає мережа: цей інстанс доступний тільки з tailnet
        # (перевірено: з домашньої LAN порт не відкривається). Тому інстанс НЕ МОЖНА
        # виставляти за Funnel — там ці маршрути стали б публічними.
        path = request.url.path
        if path == TASKS_HTTP_PATH or path.startswith(TASKS_HTTP_PATH + "/"):
            authz = request.headers.get("authorization", "")
            ok = authz.startswith(bearer) and hmac.compare_digest(authz[len(bearer):], token)
            if not ok:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    app = mcp.streamable_http_app(
        host=host, streamable_http_path=TASKS_HTTP_PATH,
        transport_security=_transport_security(port),
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=auth)
    uvicorn.run(app, host=host, port=port)


def run_oauth() -> None:
    """HTTP + OAuth (публічний шлях за Caddy/Funnel). Auth робить сам MCP SDK."""
    import uvicorn

    host = os.environ.get("TASKS_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("TASKS_HTTP_PORT", "8793"))
    if not (os.environ.get("TASKS_OAUTH_PASSWORD_HASH") or os.environ.get("TASKS_OAUTH_PASSWORD")):
        sys.exit(
            "Пароль не задано. Згенеруй хеш pbkdf2_sha256 "
            "(tasks_oauth.hash_password) і задай TASKS_OAUTH_PASSWORD_HASH"
        )
    app = mcp.streamable_http_app(
        host=host, streamable_http_path=TASKS_HTTP_PATH,
        transport_security=_transport_security(port),
    )
    from tasks_oauth import ResourceGuard

    app.add_middleware(ResourceGuard, resource=_RESOURCE)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    if _OAUTH:
        run_oauth()
    elif os.environ.get("TASKS_TRANSPORT") == "http" or (
        len(sys.argv) > 1 and sys.argv[1] == "--http"
    ):
        run_http()
    else:
        mcp.run()
