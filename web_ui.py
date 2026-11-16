"""
web_ui — веб-дашборд задач (канбан + календар) над тим самим PlannerStore.
==========================================================================

⚠️ БЕЗПЕКА — прочитай перед тим, як щось тут міняти.

Дашборд НЕ має власної авторизації. Захист — виключно мережевий: він реєструється
ТІЛЬКИ на bearer-інстансі (`:8792`), який фаєрвол пускає лише з tailscale0.
Перевірено емпірично 2026-09-06 з машини в тій самій домашній LAN:
    <lan-ip>:8792  (LAN)     → з'єднання не встановлюється
    <tailnet-ip>:8792  (tailnet) → 401
Тобто з домашнього Wi-Fi порт не видно, тільки з tailnet.

Звідси головне правило: `register()` НЕ МОЖНА викликати на OAuth-інстансі (`:8793`).
Той інстанс стоїть за Caddy/Funnel, і будь-який його маршрут стає ПУБЛІЧНИМ —
дашборд без пароля виїхав би в інтернет. Тому виклик захищений прапорцем у
`task_manager.py` (`if not _OAUTH`), а тут — ще й assert'ом.

Рішення прийнято свідомо (2026-09-06): альтернативою була публікація за Funnel із
саморобною cookie-сесією. Відхилено, бо саморобна сесійна авторизація — найбільше
джерело реальних дірок у таких системах, а Tailscale дає криптографічний доступ
без жодного рядка нашого коду. Ціна — щоб відкрити дашборд, пристрій має бути в tailnet.

Шлях сторінки — `/task-manager` (2026-09-06; було `/ui`). Старий `/ui` лишено як
302-редирект, щоб не ламати збережені закладки.
"""
from __future__ import annotations

import functools

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from planner_history import as_client
from planner_store import UNSET, PlannerError

# Джерело змін у журналі. Без цього все з цього інстансу підписувалось би `mac` —
# і клік у дашборді не відрізнявся б від MCP-виклику з Claude Code.
WEB_CLIENT = "web"

# Шлях сторінки дашборда (див. шапку модуля).
UI_PATH = "/task-manager"


def _as_web(fn):
    """Позначає всі записи журналу, зроблені в межах запиту, джерелом `web`."""

    @functools.wraps(fn)
    async def wrapper(request: Request):
        with as_client(WEB_CLIENT):
            return await fn(request)

    return wrapper


def _opt(v):
    """Три стани поля (як в edit_task): відсутнє → UNSET; 'none' → None; інакше значення."""
    if v is None or v == "":
        return UNSET
    if isinstance(v, str) and v.strip().lower() in ("none", "-", "—"):
        return None
    return v.strip() if isinstance(v, str) else v


def _opt_list(v):
    if v is None or v == "":
        return UNSET
    if isinstance(v, list):
        return [str(x).lstrip("#") for x in v]
    if v.strip().lower() in ("none", "-", "—"):
        return []
    return [x.strip().lstrip("#") for x in v.replace(",", " ").split() if x.strip()]


def _plan_opt(v):
    """План з тіла запиту: відсутній → UNSET; рядок — як є (без обрізання: markdown
    зберігається байт-у-байт; '' чи 'none' стор трактує як «прибрати»)."""
    if v is None:
        return UNSET
    if not isinstance(v, str):
        raise PlannerError("plan має бути рядком")
    return v


def register(mcp, store, *, is_oauth: bool) -> None:
    """Вішає маршрути дашборда на Starlette-застосунок MCP-сервера.

    is_oauth=True → НЕ реєструємо нічого (див. блок безпеки в шапці модуля)."""
    assert not is_oauth, (
        "web_ui.register() на OAuth-інстансі: дашборд не має авторизації і за Funnel "
        "став би публічним. Реєструвати лише на bearer-інстансі (tailnet)."
    )

    def _err(e: Exception, code: int = 400) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=code)

    async def _body(request: Request) -> dict:
        # Битий або не-обʼєктний JSON — помилка клієнта (400), а не падіння сервера (500).
        try:
            d = await request.json()
        except ValueError:
            raise PlannerError("тіло запиту — не JSON")
        if not isinstance(d, dict):
            raise PlannerError("тіло запиту має бути JSON-обʼєктом")
        return d

    @mcp.custom_route(UI_PATH, methods=["GET"])
    @_as_web
    async def ui_page(request: Request):
        return HTMLResponse(PAGE)

    @mcp.custom_route("/ui", methods=["GET"])
    async def ui_legacy(request: Request):
        # Стара адреса зі збережених закладок → нова сторінка.
        return RedirectResponse(UI_PATH, status_code=302)

    @mcp.custom_route("/api/tasks", methods=["GET"])
    @_as_web
    async def api_tasks(request: Request):
        q = request.query_params
        try:
            tasks = store.list_tasks(
                status=q.get("status") or None,
                project=q.get("project") or None,
                priority=q.get("priority") or None,
                domain=q.get("domain") or None,
            )
        except PlannerError as e:
            return _err(e)
        vocab = store.vocab()
        return JSONResponse(
            {
                "tasks": tasks,
                "projects": vocab["projects"],
                "domains": vocab["domains"],
                "links": vocab["links"],
                "templates": store.templates(),
                "tag_colors": store.tag_colors(),
                "title_max": _title_max(),
                "wip_limit": _wip_limit(),
            }
        )

    @mcp.custom_route("/api/task", methods=["GET"])
    @_as_web
    async def api_task(request: Request):
        # Одна задача з планом: у списку /api/tasks плану нема (не читати файл на кожну картку).
        try:
            t = store.get_task((request.query_params.get("id") or "").strip())
        except PlannerError as e:
            return _err(e, 404)
        return JSONResponse({"task": t})

    @mcp.custom_route("/api/tag-colors", methods=["GET"])
    @_as_web
    async def api_tag_colors(request: Request):
        try:
            return JSONResponse({"tag_colors": store.tag_colors()})
        except PlannerError as e:
            return _err(e)

    @mcp.custom_route("/api/tag-color", methods=["POST"])
    @_as_web
    async def api_tag_color(request: Request):
        # {"tag": "дім", "color": "#RRGGBB"}; "color": null знімає колір.
        try:
            d = await _body(request)
            if "color" not in d:
                raise PlannerError("не вказано color (#RRGGBB або null)")
            colors = store.set_tag_color(d.get("tag"), d["color"])
        except PlannerError as e:
            return _err(e)
        return JSONResponse({"tag_colors": colors})

    @mcp.custom_route("/api/projects", methods=["GET"])
    @_as_web
    async def api_projects(request: Request):
        try:
            return JSONResponse({"projects": store.list_projects()})
        except PlannerError as e:
            return _err(e)

    @mcp.custom_route("/api/project", methods=["POST"])
    @_as_web
    async def api_project(request: Request):
        # {"slug": "alpha", "name": "Alpha", "repo": "owner/name" | ""}
        try:
            d = await _body(request)
            project = store.create_project(d.get("slug"), d.get("name"), d.get("repo") or "")
        except PlannerError as e:
            return _err(e)
        return JSONResponse({"project": project})

    @mcp.custom_route("/api/task/add", methods=["POST"])
    @_as_web
    async def api_add(request: Request):
        try:
            d = await _body(request)
            plan = _plan_opt(d.get("plan"))
            t = store.add_task(
                (d.get("text") or "").strip(),
                title=(d.get("title") or None),
                project=d.get("project") or None,
                priority=d.get("priority") or None,
                deadline=d.get("deadline") or None,
                energy=d.get("energy") or None,
                domains=_as_list(d.get("domains")),
                links=_as_list(d.get("links")),
                plan=None if plan is UNSET else plan,
            )
        except PlannerError as e:
            return _err(e)
        return JSONResponse({"task": t})

    @mcp.custom_route("/api/task/edit", methods=["POST"])
    @_as_web
    async def api_edit(request: Request):
        try:
            d = await _body(request)
        except PlannerError as e:
            return _err(e)
        tid = (d.get("id") or "").strip()
        if not tid:
            return _err(ValueError("не вказано id"))
        try:
            t = store.edit_task(
                tid,
                text=_opt(d.get("text")),
                title=_opt(d.get("title")),
                status=_opt(d.get("status")),
                priority=_opt(d.get("priority")),
                deadline=_opt(d.get("deadline")),
                energy=_opt(d.get("energy")),
                domains=_opt_list(d.get("domains")),
                links=_opt_list(d.get("links")),
                project=_opt(d.get("project")),
                plan=_plan_opt(d.get("plan")),
                position=d.get("position"),      # 1-based місце в To Do; перевіряє стор
            )
        except PlannerError as e:
            return _err(e)
        return JSONResponse({"task": t})

    @mcp.custom_route("/api/task/delete", methods=["POST"])
    @_as_web
    async def api_delete(request: Request):
        try:
            d = await _body(request)
            t = store.delete_task((d.get("id") or "").strip())
        except PlannerError as e:
            return _err(e)
        return JSONResponse({"task": t})

    @mcp.custom_route("/api/history", methods=["GET"])
    @_as_web
    async def api_history(request: Request):
        q = request.query_params
        try:
            limit = max(1, min(int(q.get("limit") or 30), 200))
        except ValueError:
            return _err(ValueError("limit має бути числом"))
        return JSONResponse({"records": store.history.read(q.get("id") or None, limit)})


def _as_list(v):
    if not v:
        return []
    if isinstance(v, list):
        return [str(x).lstrip("#") for x in v]
    return [x.strip().lstrip("#") for x in str(v).replace(",", " ").split() if x.strip()]


def _wip_limit() -> int:
    from planner_store import MAX_IN_PROGRESS

    return MAX_IN_PROGRESS


def _title_max() -> int:
    from planner_model import TITLE_MAX

    return TITLE_MAX


# --- Сторінка ----------------------------------------------------------------
# Один самодостатній HTML: без збірки, без npm, без зовнішніх CDN (сторінка живе
# в tailnet і має відкриватись навіть без інтернету). Дані задач вставляються в DOM
# ЛИШЕ через textContent — тож текст задачі не може виконатись як розмітка (XSS).
PAGE = r"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Задачі</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#fff; --ink:#1c1f24; --muted:#6b7280; --line:#e3e6ea;
    --accent:#3b6ef5; --done:#12a150; --wip:#e8890c; --danger:#d33;
    --backlog:#8b93a1; --testing:#7b5cd6; --todo:#2f9bb5; --review:#c9569a; --blockers:#c0392b;
  }
  @media (prefers-color-scheme:dark){
    :root{ --bg:#14161a; --panel:#1c1f24; --ink:#e8eaed; --muted:#9aa3af; --line:#2b3038; }
  }
  *{box-sizing:border-box}
  /* .board має display:grid, який за специфічністю перебиває [hidden] від браузера,
     тож без !important перемикання вкладок не ховає канбан (спіймано на верифікації). */
  [hidden]{display:none!important}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
         padding:12px 16px;border-bottom:1px solid var(--line);background:var(--panel);
         position:sticky;top:0;z-index:20}
  h1{font-size:16px;margin:0;font-weight:650}
  .grow{flex:1}
  .tabs{display:flex;gap:4px;background:var(--bg);padding:3px;border-radius:9px}
  .tabs button{border:0;background:none;color:var(--muted);padding:6px 14px;
               border-radius:7px;cursor:pointer;font-size:13px;font-weight:600}
  .tabs button.on{background:var(--panel);color:var(--ink);box-shadow:0 1px 3px #0002}
  .stat{color:var(--muted);font-size:12.5px}
  .stat b{color:var(--ink)}
  .stat .over{color:var(--danger);font-weight:700}
  button.primary{background:var(--accent);color:#fff;border:0;padding:7px 14px;
                 border-radius:8px;cursor:pointer;font-weight:600;font-size:13px}
  input,select,textarea{background:var(--bg);color:var(--ink);border:1px solid var(--line);
        border-radius:8px;padding:7px 9px;font:inherit;width:100%}
  #tpl{width:auto;max-width:230px;cursor:pointer}
  main{padding:16px}

  /* --- Канбан --- */
  /* stretch (а не start) — колонки тягнуться до найвищої, тож зона дропу однакова
     в усіх, і картку з низу довгої колонки можна кинути в порожню сусідню. */
  .board{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:12px;align-items:stretch}
  @media(max-width:900px){ .board{grid-template-columns:1fr} }
  .col{background:var(--panel);border:1px solid var(--line);border-radius:12px;
       padding:10px;min-height:220px;display:flex;flex-direction:column;min-width:0}
  .col.drop{outline:2px dashed var(--accent);outline-offset:-4px}
  .col h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;margin:2px 4px 10px;
          color:var(--muted);display:flex;justify-content:space-between}
  /* items тягнеться на всю решту висоти колонки — інакше дроп ловився б лише
     по картках, а не по порожньому місцю під ними. */
  .col .items{flex:1;min-height:40px}
  .card{background:var(--bg);border:1px solid var(--line);border-left:3px solid var(--backlog);
        border-radius:9px;padding:9px 10px;margin-bottom:8px;cursor:grab;min-width:0}
  .card:active{cursor:grabbing}
  .card.s-todo{border-left-color:var(--todo)}
  .card.s-in_review{border-left-color:var(--review)}
  .card.s-blockers{border-left-color:var(--blockers)}
  .card.p-asap{border-left-color:var(--danger)}
  .card.p-high{border-left-color:var(--wip)}
  .card.dragging{opacity:.4}
  .card .title{font-weight:600;word-break:break-word}
  /* Повний текст — другорядний і завжди обрізаний двома рядками: картка не має
     розповзатись від довгого формулювання (заголовок тепер несе суть). */
  .card .txt{color:var(--muted);font-size:12.5px;margin-top:3px;word-break:break-word;
             display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px;font-size:11.5px;color:var(--muted)}
  .chip{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:1px 8px}
  .chip.id{font-family:ui-monospace,monospace;user-select:text;cursor:text}
  .chip.late{background:#d3332218;color:var(--danger);border-color:#d3332255}
  /* Колір тега (налаштування 🎨): задається змінною --tag-color лише коли він є. */
  .chip.tagged{background:color-mix(in srgb,var(--tag-color) 18%,transparent);
               border-color:var(--tag-color);color:var(--ink)}
  .tagrow{display:flex;align-items:center;gap:10px;padding:4px 0}
  .tagrow .name{flex:1;word-break:break-word}
  .tagrow input[type=color]{width:38px;height:26px;padding:0;border:1px solid var(--line);
                            border-radius:6px;background:none;cursor:pointer}
  .tagrow button{background:none;border:1px solid var(--line);color:var(--muted);
                 border-radius:6px;padding:2px 8px;cursor:pointer}

  /* --- Календар --- */
  .cal{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px}
  .calhead{display:flex;align-items:center;gap:10px;margin-bottom:10px}
  /* minmax(0,1fr) — інакше довгий рядок задачі роздуває колонку і ламає сітку. */
  .grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}
  .dow{font-size:11px;color:var(--muted);text-align:center;text-transform:uppercase}
  .day{min-height:88px;border:1px solid var(--line);border-radius:8px;padding:5px;
       background:var(--bg);min-width:0;overflow:hidden}
  .day.dim{opacity:.4}
  .day.today{outline:2px solid var(--accent);outline-offset:-2px}
  .day .n{font-size:11px;color:var(--muted)}
  .ev{font-size:11.5px;background:var(--panel);border:1px solid var(--line);
      border-radius:6px;padding:2px 5px;margin-top:3px;cursor:pointer;max-width:100%;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ev.done{opacity:.45;text-decoration:line-through}

  /* --- Модалка --- */
  dialog{border:0;border-radius:14px;padding:0;background:var(--panel);color:var(--ink);
         width:min(560px,94vw)}
  dialog::backdrop{background:#0008}
  .dlg{padding:16px}
  .dlg h3{margin:0 0 12px;font-size:15px}
  .row{display:grid;grid-template-columns:110px 1fr;gap:8px;align-items:center;margin-bottom:8px}
  .row.top{align-items:start}
  .row label{display:flex;align-items:center;gap:5px}
  .hint{display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;
        border-radius:50%;border:1px solid var(--line);color:var(--muted);font-size:10px;
        cursor:help;user-select:none}
  .counter{font-size:11px;color:var(--muted);text-align:right;margin-top:2px}
  .req{color:var(--danger)}
  input.bad{border-color:var(--danger)}
  .counter.over{color:var(--danger)}
  .actions{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
  .actions .del{background:none;border:1px solid var(--line);color:var(--danger);
                border-radius:8px;padding:7px 12px;cursor:pointer;margin-right:auto}
  .actions .ghost{background:none;border:1px solid var(--line);color:var(--ink);
                  border-radius:8px;padding:7px 12px;cursor:pointer}
  .hist{margin-top:14px;border-top:1px solid var(--line);padding-top:10px;
        max-height:190px;overflow:auto;font-size:12px;color:var(--muted)}
  .hist div{margin-bottom:5px}
  .err{background:#d3332218;color:var(--danger);border:1px solid #d3332255;
       border-radius:8px;padding:8px 10px;margin-bottom:10px;display:none}

  /* --- Пікер тегів (домени / звʼязки) --- */
  .picker{position:relative;display:flex;flex-wrap:wrap;gap:4px;background:var(--bg);
          border:1px solid var(--line);border-radius:8px;padding:4px 6px;min-height:34px}
  .picker.focus{border-color:var(--accent)}
  .picker .tag{display:inline-flex;align-items:center;gap:4px;background:var(--panel);
        border:1px solid var(--line);border-radius:20px;padding:1px 4px 1px 8px;font-size:12px}
  .picker .tag b{font-weight:600}
  .picker .tag button{border:0;background:none;color:var(--muted);cursor:pointer;
        font-size:13px;line-height:1;padding:0 3px}
  .picker input{border:0;background:none;padding:2px;flex:1;min-width:90px;outline:none}
  .menu{position:absolute;top:100%;left:0;right:0;z-index:5;margin-top:4px;max-height:170px;
        overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;
        box-shadow:0 6px 20px #0003}
  .menu div{padding:6px 10px;cursor:pointer;font-size:13px}
  .menu div.on,.menu div:hover{background:var(--bg)}
  .menu div.empty{color:var(--muted);cursor:default}

  /* --- Тост --- */
  #toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);
         background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:10px;
         font-size:13px;font-weight:600;opacity:0;pointer-events:none;transition:.25s;z-index:60}
  #toast.on{opacity:1;transform:translateX(-50%) translateY(0)}

  /* --- Дофамін: нагорода за done --- */
  #cheer{position:fixed;inset:0;z-index:80;pointer-events:none;display:grid;place-items:center}
  #cheer canvas{position:absolute;inset:0;width:100%;height:100%}
  #cheer .badge{position:relative;text-align:center;padding:22px 30px;border-radius:20px;
        background:var(--panel);border:1px solid var(--line);box-shadow:0 20px 60px #0004;
        animation:pop .45s cubic-bezier(.2,1.6,.4,1) both}
  #cheer .big{font-size:52px;line-height:1;animation:spin .9s ease-out both}
  #cheer .word{font-size:20px;font-weight:750;margin-top:8px}
  #cheer .who{font-size:13px;color:var(--muted);margin-top:4px;max-width:320px}
  #cheer.out{animation:fade .45s ease-in forwards}
  @keyframes pop{from{transform:scale(.5);opacity:0}to{transform:scale(1);opacity:1}}
  @keyframes spin{from{transform:rotate(-25deg) scale(.3)}to{transform:rotate(0) scale(1)}}
  @keyframes fade{to{opacity:0}}
  @media (prefers-reduced-motion:reduce){
    #cheer .badge,#cheer .big{animation:none}
  }
</style>
</head>
<body>
<header>
  <h1>📋 Задачі</h1>
  <div class="tabs">
    <button id="tabBoard" class="on">Канбан</button>
    <button id="tabCal">Календар</button>
  </div>
  <div class="stat" id="stat"></div>
  <div class="grow"></div>
  <button class="ghost" id="tagsBtn" title="Кольори тегів"
          style="background:none;border:1px solid var(--line);color:inherit;border-radius:8px;padding:6px 10px;cursor:pointer">🎨</button>
  <button class="ghost" id="projBtn" title="Новий проєкт"
          style="background:none;border:1px solid var(--line);color:inherit;border-radius:8px;padding:6px 10px;cursor:pointer">📁</button>
  <select id="tpl" title="Шаблони задач: створити типову задачу в один клік"></select>
  <button class="primary" id="addBtn">+ Задача</button>
</header>
<main>
  <div id="viewBoard" class="board"></div>
  <div id="viewCal" class="cal" hidden>
    <div class="calhead">
      <button class="ghost" id="prevM" style="background:none;border:1px solid var(--line);color:inherit;border-radius:8px;padding:5px 10px;cursor:pointer">‹</button>
      <b id="monthLbl"></b>
      <button class="ghost" id="nextM" style="background:none;border:1px solid var(--line);color:inherit;border-radius:8px;padding:5px 10px;cursor:pointer">›</button>
      <span class="stat">показані задачі з дедлайном</span>
    </div>
    <div class="grid" id="dows"></div>
    <div class="grid" id="days" style="margin-top:6px"></div>
  </div>
</main>

<dialog id="dlg"><form class="dlg" method="dialog" id="form">
  <h3 id="dlgTitle">Задача</h3>
  <div class="err" id="err"></div>
  <div class="row top"><label>Заголовок <b class="req" title="Обовʼязкове поле">*</b></label><div>
    <input id="f_title" maxlength="50" required placeholder="Коротко, до 50 символів">
    <div class="counter" id="titleCount"></div></div></div>
  <div class="row top"><label>Текст</label><textarea id="f_text" rows="2"></textarea></div>
  <div class="row top"><label>План
      <span class="hint" title="Багаторядковий markdown (кроки, «Фідбек»). Зберігається окремо від рядка задачі. Порожній — плану нема.">?</span>
    </label><textarea id="f_plan" rows="8" placeholder="Markdown"></textarea></div>
  <div class="row"><label>Проєкт
      <span class="hint" title="Новий проєкт створюється тільки через AI (MCP task-manager). Тут — вибір із наявних.">?</span>
    </label><select id="f_project"></select></div>
  <div class="row" id="statusRow"><label>Статус</label><select id="f_status">
    <option value="backlog">backlog</option><option value="todo">to do</option>
    <option value="blockers">blockers</option><option value="in_progress">in progress</option><option value="in_review">in review</option>
    <option value="testing">testing</option><option value="done">done</option></select></div>
  <div class="row"><label>Пріоритет</label><select id="f_priority">
    <option value="">—</option><option value="asap">🔺 asap</option>
    <option value="high">⏫ high</option><option value="low">🔽 low</option></select></div>
  <div class="row"><label>Дедлайн</label><input id="f_deadline" type="date"></div>
  <div class="row"><label>Енергія</label><select id="f_energy">
    <option value="">—</option><option>S</option><option>M</option><option>L</option></select></div>
  <div class="row top"><label>Домени
      <span class="hint" title="Обери наявний тег або впиши новий і натисни Enter.">?</span>
    </label><div id="f_domains"></div></div>
  <div class="row top"><label>Звʼязки
      <span class="hint" title="Тільки з наявних: звʼязки ведуть у нотатки мозку, нові додає AI.">?</span>
    </label><div id="f_links"></div></div>
  <div class="actions">
    <button type="button" class="del" id="delBtn" hidden>Видалити</button>
    <button type="button" class="ghost" id="cancelBtn">Скасувати</button>
    <button type="button" class="primary" id="saveBtn">Зберегти</button>
  </div>
  <div class="hist" id="hist" hidden></div>
</form></dialog>

<dialog id="tagsDlg"><form class="dlg" method="dialog">
  <h3>Кольори тегів</h3>
  <div class="err" id="tagsErr"></div>
  <div id="tagsList"></div>
  <div class="actions"><button type="button" class="ghost" id="tagsClose">Закрити</button></div>
</form></dialog>

<dialog id="projDlg"><form class="dlg" method="dialog">
  <h3>Новий проєкт</h3>
  <div class="err" id="projErr"></div>
  <div class="row"><label>Слаг</label><input id="p_slug" placeholder="my-project (a-z, 0-9, -)"></div>
  <div class="row"><label>Назва</label><input id="p_name"></div>
  <div class="row"><label>Репозиторій</label><input id="p_repo" placeholder="owner/name (необовʼязково)"></div>
  <div class="actions">
    <button type="button" class="ghost" id="projCancel">Скасувати</button>
    <button type="button" class="primary" id="projSave">Створити</button>
  </div>
</form></dialog>

<div id="toast"></div>

<script>
const COLS = [["backlog","backlog"],["todo","to do"],["blockers","blockers"],["in_progress","in progress"],["in_review","in review"],["testing","testing"],["done","done"]];
const PRIO = {asap:"🔺",high:"⏫",low:"🔽"};
let TASKS = [], WIP = 15, TITLE_MAX = 50, VOCAB = {projects:[],domains:[],links:[]}, TEMPLATES = [], TAG_COLORS = {};
let editing = null, calMonth = new Date(), planLoaded = true;

const $ = s => document.querySelector(s);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({error:"погана відповідь сервера"}));
  if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
  return j;
};
const today = () => new Date().toISOString().slice(0,10);
const titleOf = t => t.title || t.text;

async function load(){
  const j = await api("api/tasks");
  TASKS = j.tasks; WIP = j.wip_limit || 15; TITLE_MAX = j.title_max || 50;
  VOCAB = {projects: j.projects || [], domains: j.domains || [], links: j.links || []};
  TEMPLATES = j.templates || [];
  TAG_COLORS = j.tag_colors || {};
  renderStat(); renderBoard(); renderCal(); renderTemplates();
}

function renderStat(){
  const c = {backlog:0,todo:0,blockers:0,in_progress:0,in_review:0,testing:0,done:0};
  TASKS.forEach(t => c[t.status]++);
  const over = c.in_progress > WIP;
  $("#stat").innerHTML = "";
  const mk = (label, val, cls) => {
    const s = document.createElement("span");
    s.textContent = label + ": ";
    const b = document.createElement("b");
    b.textContent = val; if (cls) b.className = cls;
    s.appendChild(b); s.append("  ");
    return s;
  };
  $("#stat").append(
    mk("backlog", c.backlog),
    mk("to do", c.todo),
    mk("blockers", c.blockers),
    mk("in progress", c.in_progress + "/" + WIP, over ? "over" : ""),
    mk("in review", c.in_review),
    mk("testing", c.testing),
    mk("done", c.done)
  );
}

function projOf(t){ return t.file === "tasks.md" ? "global" : t.file.replace("projects/","").replace(".md",""); }

// --- Шаблони: швидке створення типової задачі в один клік ---
function renderTemplates(){
  const sel = $("#tpl");
  sel.innerHTML = "";
  const head = document.createElement("option");
  head.value = ""; head.textContent = TEMPLATES.length ? "⚡ Шаблон…" : "⚡ Шаблонів нема";
  sel.appendChild(head);
  const byProj = {};
  TEMPLATES.forEach((t,i) => { (byProj[t.project] = byProj[t.project] || []).push([i,t]); });
  Object.keys(byProj).sort().forEach(p => {
    const g = document.createElement("optgroup"); g.label = "@" + p;
    byProj[p].forEach(([i,t]) => {
      const o = document.createElement("option");
      o.value = String(i); o.textContent = t.title;
      g.appendChild(o);
    });
    sel.appendChild(g);
  });
  sel.disabled = !TEMPLATES.length;
}

async function fromTemplate(i){
  const tpl = TEMPLATES[i];
  if (!tpl) return;
  try{
    await api("api/task/add", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        text: tpl.text, title: tpl.title, project: tpl.project,
        priority: tpl.priority || "", energy: tpl.energy || "",
        domains: tpl.domains || [], links: tpl.links || [],
      })});
    await load();
    toast("➕ " + tpl.title);
  } catch(e){ alert(e.message); }
}

let toastTimer = null;
function toast(msg){
  const el = $("#toast");
  el.textContent = msg; el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), 2200);
}

function card(t){
  const d = document.createElement("div");
  d.className = "card s-" + t.status + (t.priority ? " p-" + t.priority : "");
  d.draggable = true; d.dataset.id = t.id;
  const h = document.createElement("div"); h.className = "title";
  h.textContent = titleOf(t);                  // textContent, не innerHTML → XSS неможливий
  d.appendChild(h);
  if (t.title && t.text && t.text !== t.title){
    const p = document.createElement("div"); p.className = "txt";
    p.textContent = t.text; p.title = t.text;
    d.appendChild(p);
  }
  const m = document.createElement("div"); m.className = "meta";
  const chip = (txt, cls, color) => { const c = document.createElement("span");
    c.className = "chip" + (cls ? " " + cls : ""); c.textContent = txt;
    if (color){ c.classList.add("tagged"); c.style.setProperty("--tag-color", color); }
    m.appendChild(c); };
  chip(t.id, "id");
  if (t.priority) chip(PRIO[t.priority] + " " + t.priority);
  if (t.deadline) chip("📅 " + t.deadline, (t.deadline < today() && t.status !== "done") ? "late" : "");
  if (t.energy) chip("⚡" + t.energy);
  (t.domains||[]).forEach(x => chip("#" + x, "", TAG_COLORS[x]));
  chip("@" + projOf(t));
  (t.links||[]).forEach(l => chip("🔗 " + l));
  d.appendChild(m);
  d.addEventListener("dragstart", e => { e.dataTransfer.setData("text/plain", t.id); d.classList.add("dragging"); });
  d.addEventListener("dragend", () => d.classList.remove("dragging"));
  d.addEventListener("click", () => openEdit(t));
  return d;
}

function renderBoard(){
  const b = $("#viewBoard"); b.innerHTML = "";
  COLS.forEach(([key,label]) => {
    const col = document.createElement("div"); col.className = "col"; col.dataset.status = key;
    const h = document.createElement("h2");
    const items = TASKS.filter(t => t.status === key);
    const l = document.createElement("span"); l.textContent = label;
    const n = document.createElement("span"); n.textContent = items.length;
    h.append(l, n); col.appendChild(h);
    const box = document.createElement("div"); box.className = "items";
    items.forEach(t => box.appendChild(card(t)));
    col.appendChild(box);
    col.addEventListener("dragover", e => { e.preventDefault(); col.classList.add("drop"); });
    col.addEventListener("dragleave", e => { if (!col.contains(e.relatedTarget)) col.classList.remove("drop"); });
    col.addEventListener("drop", async e => {
      e.preventDefault(); col.classList.remove("drop");
      const id = e.dataTransfer.getData("text/plain");
      const t = TASKS.find(x => x.id === id);
      if (!t || (t.status === key && key !== "todo")) return;
      const body = {id, status:key};
      if (key === "todo"){      // місце скидання в To Do: скільки інших карток лежить вище курсора
        const above = [...box.querySelectorAll(".card")].filter(c => c.dataset.id !== id &&
          e.clientY >= c.getBoundingClientRect().top + c.offsetHeight / 2).length;
        body.position = above + 1;
      }
      try {
        await api("api/task/edit", {method:"POST", headers:{"Content-Type":"application/json"},
              body: JSON.stringify(body)});
        if (key === "done") celebrate(titleOf(t));
        await load();
      }
      catch(err){ alert(err.message); }          // WIP-ліміт і заборонені переходи прилітають саме сюди
    });
    b.appendChild(col);
  });
}

function renderCal(){
  const dows = ["пн","вт","ср","чт","пт","сб","нд"];
  $("#dows").innerHTML = "";
  dows.forEach(d => { const e = document.createElement("div"); e.className = "dow"; e.textContent = d; $("#dows").appendChild(e); });
  const y = calMonth.getFullYear(), m = calMonth.getMonth();
  $("#monthLbl").textContent = calMonth.toLocaleDateString("uk-UA", {month:"long", year:"numeric"});
  const first = new Date(y, m, 1), start = (first.getDay() + 6) % 7;
  const days = $("#days"); days.innerHTML = "";
  const cells = 42;
  for (let i = 0; i < cells; i++){
    const date = new Date(y, m, 1 - start + i);
    const iso = new Date(date.getTime() - date.getTimezoneOffset()*60000).toISOString().slice(0,10);
    const cell = document.createElement("div");
    cell.className = "day" + (date.getMonth() !== m ? " dim" : "") + (iso === today() ? " today" : "");
    const n = document.createElement("div"); n.className = "n"; n.textContent = date.getDate();
    cell.appendChild(n);
    TASKS.filter(t => t.deadline === iso).forEach(t => {
      const e = document.createElement("div");
      e.className = "ev" + (t.status === "done" ? " done" : "");
      // У клітинку дня влазить лише короткий заголовок; решту показує tooltip і картка.
      e.textContent = (PRIO[t.priority] || "") + " " + titleOf(t);
      e.title = t.text;
      e.addEventListener("click", () => openEdit(t));
      cell.appendChild(e);
    });
    days.appendChild(cell);
  }
}

// --- Пікер тегів -------------------------------------------------------------
// Один компонент на два випадки: домени (можна дописати новий) і звʼязки
// (тільки вибір із наявних — вони ведуть у нотатки мозку, які цей сервіс не бачить).
function makePicker(host, opts){
  const state = {values: [], options: [], creatable: !!opts.creatable};
  host.className = "picker";
  const inp = document.createElement("input");
  inp.placeholder = opts.placeholder || "";
  const menu = document.createElement("div"); menu.className = "menu"; menu.hidden = true;
  host.append(inp, menu);

  function draw(){
    host.querySelectorAll(".tag").forEach(el => el.remove());
    state.values.forEach(v => {
      const tag = document.createElement("span"); tag.className = "tag";
      const b = document.createElement("b"); b.textContent = (opts.prefix || "") + v;
      const x = document.createElement("button"); x.type = "button"; x.textContent = "×";
      x.onclick = () => { state.values = state.values.filter(z => z !== v); draw(); };
      tag.append(b, x); host.insertBefore(tag, inp);
    });
  }
  function add(v){
    v = (v || "").trim().replace(/^#/, "");
    if (!v) return;
    if (!state.creatable && !state.options.includes(v)) return;
    if (!state.values.includes(v)) state.values.push(v);
    // Меню лишається відкритим: це мультиселект, зазвичай обирають кілька підряд.
    inp.value = ""; draw(); showMenu();
  }
  function hideMenu(){ menu.hidden = true; }
  function showMenu(){
    const q = inp.value.trim().toLowerCase().replace(/^#/, "");
    const list = state.options.filter(o => !state.values.includes(o) && o.toLowerCase().includes(q));
    menu.innerHTML = "";
    if (!list.length){
      const e = document.createElement("div"); e.className = "empty";
      e.textContent = state.creatable ? "Enter — додати новий" : "нічого не знайдено";
      menu.appendChild(e);
    } else {
      list.slice(0, 40).forEach((o, i) => {
        const d = document.createElement("div");
        d.textContent = o; if (i === 0) d.className = "on";
        d.onmousedown = ev => { ev.preventDefault(); add(o); };
        menu.appendChild(d);
      });
    }
    menu.hidden = false;
  }
  // focus НЕ досить: після вибору фокус лишається в інпуті, тож повторний клік по полю
  // не давав жодної події і меню більше не відкривалось (баг, спіймано в бою).
  // Тому слухаємо ще й click — і на самому інпуті, і на контейнері.
  inp.addEventListener("focus", () => { host.classList.add("focus"); showMenu(); });
  inp.addEventListener("click", showMenu);
  inp.addEventListener("input", showMenu);
  inp.addEventListener("blur", () => {
    host.classList.remove("focus");
    // Дописаний, але не підтверджений текст не губимо — але тільки там, де можна створювати.
    if (state.creatable && inp.value.trim()) add(inp.value);
    inp.value = ""; hideMenu();
  });
  inp.addEventListener("keydown", e => {
    if (e.key === "Enter"){
      e.preventDefault();
      const first = menu.querySelector("div:not(.empty)");
      if (state.creatable && inp.value.trim()) add(inp.value);
      else if (first) add(first.textContent);
    } else if (e.key === "Backspace" && !inp.value && state.values.length){
      state.values.pop(); draw(); showMenu();
    } else if (e.key === "Escape"){ hideMenu(); }
  });
  host.addEventListener("click", e => {
    if (e.target === host || e.target === inp){ inp.focus(); showMenu(); }
  });

  return {
    set(values, options){ state.values = (values||[]).slice(); state.options = (options||[]).slice(); draw(); },
    get(){ return state.values.slice(); },
  };
}

const pickDomains = makePicker($("#f_domains"), {creatable:true, prefix:"#", placeholder:"тег + Enter"});
const pickLinks = makePicker($("#f_links"), {creatable:false, placeholder:"обери зі списку"});

// --- Дофамінова нагорода за done --------------------------------------------
const CHEERS = ["Красава!", "Є!", "Зроблено 💪", "Так тримати!", "Мінус одна!", "Вогонь 🔥", "Красунчик!"];
function celebrate(name){
  const old = document.getElementById("cheer"); if (old) old.remove();
  const wrap = document.createElement("div"); wrap.id = "cheer";
  const cv = document.createElement("canvas");
  const badge = document.createElement("div"); badge.className = "badge";
  const big = document.createElement("div"); big.className = "big"; big.textContent = "🎉";
  const word = document.createElement("div"); word.className = "word";
  word.textContent = CHEERS[Math.floor(Math.random()*CHEERS.length)];
  const who = document.createElement("div"); who.className = "who"; who.textContent = name || "";
  badge.append(big, word, who); wrap.append(cv, badge); document.body.appendChild(wrap);

  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let raf = 0;
  if (!reduce){
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const W = cv.width = innerWidth * dpr, H = cv.height = innerHeight * dpr;
    const ctx = cv.getContext("2d");
    const colors = ["#3b6ef5","#12a150","#e8890c","#d33","#7b5cd6","#f5c73b"];
    // Три салюти (два з нижніх кутів + центральний) — щоб нагорода накривала ВЕСЬ екран,
    // а не тільки плашку посередині.
    const cannons = [
      {x: 0.04*W, y: 1.02*H, a: -1.05, n: 90},
      {x: 0.96*W, y: 1.02*H, a: -2.09, n: 90},
      {x: 0.50*W, y: 0.62*H, a: -1.57, n: 80},
    ];
    const parts = [];
    cannons.forEach(c => {
      for (let i = 0; i < c.n; i++){
        const ang = c.a + (Math.random()-.5)*0.9;
        const sp = (16 + Math.random()*22) * dpr;
        parts.push({
          x: c.x + (Math.random()-.5)*40*dpr, y: c.y + (Math.random()-.5)*40*dpr,
          vx: Math.cos(ang)*sp, vy: Math.sin(ang)*sp,
          s: (4 + Math.random()*7)*dpr, rot: Math.random()*6.28, vr: (Math.random()-.5)*.4,
          c: colors[Math.floor(Math.random()*colors.length)],
        });
      }
    });
    const t0 = performance.now();
    const tick = now => {
      const age = now - t0;
      ctx.clearRect(0,0,W,H);
      parts.forEach(p => {
        p.vy += 0.42*dpr; p.vx *= 0.995; p.x += p.vx; p.y += p.vy; p.rot += p.vr;
        ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot);
        ctx.globalAlpha = Math.max(0, 1 - age/2400);
        ctx.fillStyle = p.c; ctx.fillRect(-p.s/2, -p.s/2, p.s, p.s*0.6);
        ctx.restore();
      });
      if (age < 2400) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
  }
  setTimeout(() => { wrap.classList.add("out"); }, reduce ? 900 : 1900);
  setTimeout(() => { cancelAnimationFrame(raf); wrap.remove(); }, reduce ? 1350 : 2400);
}

// --- модалка ---
function fillProjects(cur){
  const sel = $("#f_project"); sel.innerHTML = "";
  const list = VOCAB.projects.slice();
  if (cur && !list.includes(cur)) list.push(cur);   // проєкт задачі має бути видимий завжди
  list.forEach(p => {
    const o = document.createElement("option");
    o.value = p; o.textContent = p === "global" ? "global (без проєкту)" : p;
    sel.appendChild(o);
  });
  sel.value = cur || "global";
}

// Заголовок обовʼязковий, але друкувати його з нуля щоразу — зайве тертя.
// Тому з тексту пропонуємо чернетку, і лише поки поле порожнє (написане руками не чіпаємо).
function suggestTitle(text){
  let t = (text || "").replace(/\s+/g, " ").trim();
  for (const sep of [" (", " — ", " – ", ": "]){
    const head = t.split(sep)[0];
    if (head.length >= 12 && head.length < t.length) t = head;
  }
  t = t.replace(/[\[\]^#]/g, "").trim();
  if (t.length <= TITLE_MAX) return t;
  const cut = t.slice(0, TITLE_MAX - 1).replace(/\s+\S*$/, "").replace(/[ ,.;:—–-]+$/, "");
  return (cut || t.slice(0, TITLE_MAX - 1)) + "…";
}

function updCounter(){
  const n = $("#f_title").value.length;
  const c = $("#titleCount");
  c.textContent = n + "/" + TITLE_MAX;
  c.className = "counter" + (n > TITLE_MAX ? " over" : "");
}

function openEdit(t){
  editing = t;
  $("#dlgTitle").textContent = t ? "Задача " + t.id : "Нова задача";
  $("#err").style.display = "none";
  $("#f_title").maxLength = TITLE_MAX;
  $("#f_title").value = t ? (t.title || "") : "";
  $("#f_text").value = t ? t.text : "";
  // План не приходить у списку задач — тягнемо окремо; поки не завантажений, його не чіпаємо.
  $("#f_plan").value = "";
  planLoaded = !t;
  $("#f_plan").disabled = !!t;
  if (t) loadPlan(t.id);
  fillProjects(t ? projOf(t) : "global");
  $("#f_status").value = t ? t.status : "backlog";
  $("#statusRow").style.display = t ? "" : "none";   // нові задачі завжди в backlog
  $("#f_priority").value = t && t.priority ? t.priority : "";
  $("#f_deadline").value = t && t.deadline ? t.deadline : "";
  $("#f_energy").value = t && t.energy ? t.energy : "";
  pickDomains.set(t ? (t.domains||[]) : [], VOCAB.domains);
  pickLinks.set(t ? (t.links||[]) : [], VOCAB.links);
  updCounter();
  $("#delBtn").hidden = !t;
  $("#hist").hidden = !t;
  if (t) loadHist(t.id);
  $("#dlg").showModal();
}

async function loadPlan(id){
  try{
    const j = await api("api/task?id=" + encodeURIComponent(id));
    if (!editing || editing.id !== id) return;
    $("#f_plan").value = j.task.plan || "";
    planLoaded = true; $("#f_plan").disabled = false;
  } catch(e){ $("#f_plan").placeholder = "план недоступний: " + e.message; }
}

async function loadHist(id){
  const h = $("#hist"); h.textContent = "історія…";
  try{
    const j = await api("api/history?limit=8&id=" + encodeURIComponent(id));
    h.innerHTML = "";
    if (!j.records.length){ h.textContent = "змін ще не було"; return; }
    j.records.forEach(r => {
      const d = document.createElement("div");
      const when = (r.ts||"").slice(0,16).replace("T"," ");
      let line = when + " · " + (r.user||"?") + "@" + (r.client||"?") + " · " + (r.tool||"");
      if (r.op === "external") line = when + " · ⚠️ стороння правка (" + r.file + ")";
      const changes = Object.entries(r.changes || {})
        .map(([k,v]) => k + ": " + fmtV(v[0]) + " → " + fmtV(v[1])).join("; ");
      d.textContent = line + (changes ? " — " + changes : "");
      h.appendChild(d);
    });
  } catch(e){ h.textContent = "історія недоступна: " + e.message; }
}
const fmtV = v => (v === null || v === undefined || (Array.isArray(v) && !v.length) || v === "") ? "—"
                : (Array.isArray(v) ? v.join(",") : String(v).length > 60 ? String(v).slice(0,60) + "…" : String(v));

async function save(){
  const titleEl = $("#f_title");
  if (!titleEl.value.trim()){
    titleEl.classList.add("bad"); titleEl.focus();
    $("#err").textContent = "Заголовок обовʼязковий — саме він видно на канбані й у календарі";
    $("#err").style.display = "block";
    return;
  }
  titleEl.classList.remove("bad");
  const wasDone = editing && editing.status === "done";
  const domains = pickDomains.get(), links = pickLinks.get();
  const body = {
    title: titleEl.value.trim(),
    text: $("#f_text").value.trim(),
    project: $("#f_project").value.trim() || "global",
    status: $("#f_status").value,
    priority: $("#f_priority").value || "none",
    deadline: $("#f_deadline").value || "none",
    energy: $("#f_energy").value || "none",
    domains: domains.length ? domains : "none",
    links: links.length ? links : "none",
  };
  // План шлемо лише якщо він завантажений (інакше порожнє поле стерло б існуючий план);
  // порожній = «none» (прибрати), для add — просто не передаємо.
  if (planLoaded) body.plan = $("#f_plan").value.trim() ? $("#f_plan").value : "none";
  try{
    if (editing){ body.id = editing.id; await api("api/task/edit", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)}); }
    else { await api("api/task/add", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(cleanAdd(body))}); }
    $("#dlg").close();
    if (body.status === "done" && !wasDone) celebrate(body.title === "none" ? body.text : body.title);
    await load();
  } catch(e){ $("#err").textContent = e.message; $("#err").style.display = "block"; }
}
// для add «none» означає «не задано», а не «очистити»
function cleanAdd(b){
  const o = {...b};
  ["priority","deadline","energy","domains","links","plan"].forEach(k => { if (o[k] === "none") o[k] = ""; });
  return o;
}

async function del(){
  if (!editing || !confirm("Видалити задачу безповоротно?")) return;
  try{
    await api("api/task/delete", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({id: editing.id})});
    $("#dlg").close(); await load();
  } catch(e){ $("#err").textContent = e.message; $("#err").style.display = "block"; }
}

// --- Кольори тегів: усі теги з довідника + ті, що мають колір, але вже без задач ---
async function saveTagColor(body){
  try {
    const j = await api("api/tag-color", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)});
    TAG_COLORS = j.tag_colors; renderBoard(); renderTagList();
  } catch(e){ const el = $("#tagsErr"); el.textContent = e.message; el.style.display = "block"; }
}
function renderTagList(){
  const host = $("#tagsList"); host.innerHTML = "";
  $("#tagsErr").style.display = "none";
  const tags = [...new Set([...VOCAB.domains, ...Object.keys(TAG_COLORS)])].sort();
  if (!tags.length) host.textContent = "Тегів ще немає.";
  tags.forEach(tag => {
    const row = document.createElement("div"); row.className = "tagrow";
    const name = document.createElement("span"); name.className = "name"; name.textContent = "#" + tag;
    const inp = document.createElement("input"); inp.type = "color";
    inp.value = TAG_COLORS[tag] || "#888888";
    inp.onchange = () => saveTagColor({tag, color: inp.value});
    row.append(name, inp);
    if (TAG_COLORS[tag]){
      const x = document.createElement("button"); x.type = "button"; x.textContent = "✕";
      x.title = "Прибрати колір";
      x.onclick = () => saveTagColor({tag, color: null});
      row.appendChild(x);
    }
    host.appendChild(row);
  });
}
$("#tagsBtn").onclick = () => { renderTagList(); $("#tagsDlg").showModal(); };
$("#tagsClose").onclick = () => $("#tagsDlg").close();

// --- Новий проєкт: слаг, назва, репозиторій → реєстр проєктів ---
async function createProject(){
  const err = $("#projErr"); err.style.display = "none";
  try {
    await api("api/project", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({slug: $("#p_slug").value, name: $("#p_name").value, repo: $("#p_repo").value})});
    $("#projDlg").close(); await load();
  } catch(e){ err.textContent = e.message; err.style.display = "block"; }
}
$("#projBtn").onclick = () => {
  ["#p_slug", "#p_name", "#p_repo"].forEach(id => $(id).value = "");
  $("#projErr").style.display = "none"; $("#projDlg").showModal();
};
$("#projSave").onclick = createProject;
$("#projCancel").onclick = () => $("#projDlg").close();
$("#addBtn").onclick = () => openEdit(null);
$("#saveBtn").onclick = save;
$("#delBtn").onclick = del;
$("#cancelBtn").onclick = () => $("#dlg").close();
$("#f_title").addEventListener("input", () => { $("#f_title").classList.remove("bad"); updCounter(); });
$("#f_text").addEventListener("input", e => {
  if ($("#f_title").value.trim()) return;      // написане руками — святе
  $("#f_title").value = suggestTitle(e.target.value); updCounter();
});
$("#tpl").onchange = e => { const v = e.target.value; e.target.value = ""; if (v !== "") fromTemplate(+v); };
$("#prevM").onclick = () => { calMonth.setMonth(calMonth.getMonth()-1); renderCal(); };
$("#nextM").onclick = () => { calMonth.setMonth(calMonth.getMonth()+1); renderCal(); };
$("#tabBoard").onclick = () => { $("#tabBoard").classList.add("on"); $("#tabCal").classList.remove("on");
  $("#viewBoard").hidden = false; $("#viewCal").hidden = true; };
$("#tabCal").onclick = () => { $("#tabCal").classList.add("on"); $("#tabBoard").classList.remove("on");
  $("#viewCal").hidden = false; $("#viewBoard").hidden = true; };

load().catch(e => document.body.insertAdjacentHTML("afterbegin",
  "<p style='padding:16px;color:#d33'>Не вдалось завантажити задачі: " + e.message + "</p>"));
</script>
</body>
</html>
"""
