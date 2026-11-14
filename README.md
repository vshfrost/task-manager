# task-manager 📋

MCP-сервер керування задачами над доменом **`planner/`** (на сервері — `/srv/planner`; markdown = джерело правди).
Частина проєкту «Task Manager» (див. мозок: `projects/task-manager/overview.md`).

## Що це
- Парсер/серіалізатор planner-формату (round-trip: чіпає лише рядки-задачі, решту зберігає).
- MCP-тули для Claude: `list_tasks` (без done за замовч., короткі рядки, `query`/`limit`), `get_task`, `board`, `add_task`, `move_task` (правила переходів + WIP-гард),
  `set_priority`, `set_deadline`, **`edit_task`** (будь-які поля + перенос між проєктами),
  `delete_task`, **`history`** (журнал змін).
- Три транспорти: stdio · http+bearer (tailnet) · OAuth (публічно за Caddy/Funnel) — **Ф4 ✅**.
- Веб-дашборд (канбан + календар) — **Ф3 ✅**, `http://<host>:8792/task-manager` (лише tailnet).

## Веб-дашборд 🗂️
`http://<host>:8792/task-manager` — канбан (4 колонки, drag-and-drop) + календар за
дедлайнами, форма додавання, редагування будь-яких полів і історія змін просто в картці.
Один самодостатній HTML: без збірки, без npm, без зовнішніх CDN (працює і без інтернету).
Стара адреса `/ui` віддає 302 на нову (щоб не ламати закладки).

Що вміє форма (Ф7, 2026-09-06):
- **Заголовок** (≤50, **обовʼязковий**) — саме він видно на картці й у календарі; повний текст
  на картці обрізаний двома рядками, у календарі не показується взагалі (він і ламав верстку).
  Єдине поле, яке НЕ можна очистити через `none`. Щоб не було тертя, форма пропонує чернетку
  заголовка з тексту — поки поле порожнє; написане руками не чіпає.
- **Проєкт** — селект із наявних. Нового проєкту з дашборда не завести: їх заводить AI
  через MCP (тултіп у формі про це каже).
- **Домени** — теги: обираєш наявний або дописуєш новий і тиснеш Enter.
- **Кольори тегів** — кнопка 🎨 у хедері: список тегів (з довідника) і кольорпікер для кожного,
  «✕» знімає колір; теги на картках фарбуються в обраний колір, без кольору — нейтральні.
  Колір можна задати й тегу, під яким зараз немає задач. Деталі — у «Кольори тегів» нижче.
- **Звʼязки** — тільки вибір із наявних (вони ведуть у нотатки мозку, якого сервіс не бачить).
- **Шаблони** — селект «⚡ Шаблон…» у хедері: типова задача створюється в один клік.
- Перенос у **done** = повноекранне конфеті-свято (дофамін як нагорода; при
  `prefers-reduced-motion` — тільки плашка, без анімації).

### Шаблони задач
`planner/projects/<слаг>/template.md` — по файлу на проєкт, рядки в тому ж форматі, що й
задачі. Лежать у ПІДПАПЦІ проєкту, тому під керовані `projects/*.md` не підпадають і як
задачі не читаються (перевірено тестом). Щоб додати шаблон — просто допиши рядок.

### ⚠️ Безпека — головне
Дашборд **не має власної авторизації**. Захист виключно мережевий: він реєструється
**тільки на bearer-інстансі** (`:8792`), куди фаєрвол пускає лише з `tailscale0`.
Перевірено з машини в тій самій домашній LAN: `<lan-ip>:8792` → зʼєднання нема,
`<tailnet-ip>:8792` → відповідає.

**НЕ реєструвати на OAuth-інстансі** (`:8793`): він за Caddy/Funnel, і його маршрути
публічні — дашборд без пароля виїхав би в інтернет. Два запобіжники: умова `if not _OAUTH`
у `task_manager.py` і `assert` у `web_ui.register()` (на нього є тест).

Bearer-мідлварь вимагає токен лише на MCP-шляху `/tasks`; `/task-manager` і `/api/*` браузер
відкриває без заголовків.

## Журнал змін (аудит) 📜
Усі зміни задач пишуться в `planner/.history/YYYY-MM.jsonl` (append-only, по файлу на
місяць, нічого не чистимо). Читається тулом `history(id?, limit)`.

- `add` / `edit` / `delete` — з дифом «було → стало» по кожному полю; `delete` зберігає
  повний знімок задачі, бо операція незворотна.
- `external` — файл змінили **повз сервіс** (Obsidian, редактор). Стор звіряє sha256
  керованих файлів із тим, що записував сам. Полів не знаємо, але журнал чесно каже,
  що тут була стороння правка й він неповний.
- `client` — ЗВІДКИ прийшла зміна: `web` (веб-дашборд) · `mac` (MCP через bearer/tailnet) ·
  `claude` / `chatgpt` (MCP через OAuth — з `client_name`, яким клієнт зареєструвався по DCR) ·
  `phone` (OAuth-фолбек, якщо назви клієнта нема) · `local` (stdio). Базова мітка — `TASKS_CLIENT` у systemd-юніті,
  але вона описує ІНСТАНС, а один інстанс обслуговує і дашборд, і MCP. Тому веб-хендлери
  перекривають її через ContextVar (`as_client("web")`), інакше клік у дашборді не
  відрізнявся б від виклику з Claude Code — обидва були б `mac`.
- `user` — ХТО її зробив: `subject` з OAuth-токена, інакше `TASKS_DEFAULT_USER` (`vlad`).
  Сервіс поки однокористувацький, але поле пишеться вже зараз — щоб при появі другого
  пароля старі записи лишились коректними, а нові самі почали розрізняти людей.
  У виводі `history` це подано як `юзер@клієнт`, напр. `vlad@chatgpt`.
- Живе всередині `planner/`, тож автоматично потрапляє в restic-бекап.

## Ізоляція (важливо)
Сервіс має доступ **лише** до `planner/` — нічого іншого на хості він не бачить. На сервері обидва інстанси
працюють від юзера **`tasks`** у пісочниці systemd: бачать лише `/srv/planner` (запис, власник `tasks`) і
код (лише читання); решти файлової системи не бачать.

## Формат / схема
```
- [x] Текст  #домен ⏫ 📅 2026-08-31 ⚡M [[звʼязок]] 📌[Заголовок] ^id
```
- Статуси (порядок колонок): `[ ]` backlog · `[>]` to do · `[b]` blockers · `[/]` in progress · `[r]` in review · `[t]` testing · `[x]` done
- Переходи (стор, для `move_task`, `edit_task` і дашборда): у to do — лише з backlog, testing,
  in progress, in review або blockers; у blockers — лише з backlog, to do або testing; у in progress —
  лише з to do; у in review — лише з in progress; у testing — лише з in progress, in review або
  blockers; у backlog і done — звідки завгодно. Нові задачі завжди створюються в backlog.
  Виняток: **backlog → to do — лише з дашборда** (веб-API); через MCP (`move_task` і зміна статусу в
  `edit_task`, для будь-якого клієнта) це помилка. Решта переходів однакова в MCP і на дашборді.
  Колонка blockers стоїть на дашборді одразу після To Do; `board` рахує її.
- Пріоритети: `🔺` asap · `⏫` high · `🔽` low
- WIP-ліміт: ≤15 задач у `in progress` (по всьому planner); to do і in review без ліміту
- **Порядок To Do** — ручний, у `planner/todo-order.json` (список id; не файл задач): задачі To Do
  лежать у різних файлах проєктів, тож порядок рядків його не передає. Вхід у To Do без позиції —
  в кінець (з testing, blockers, backlog), а з in progress і in review — нагору (позиція 1); явна
  `position` перемагає це правило; на дашборді скидання картки на місце в колонці (і зі backlog, і з самої To Do) ставить
  її туди — `POST /api/task/edit` приймає `position` (1-based; більше за довжину — в кінець;
  <1 чи не ціле — помилка, нічого не пишеться; лише разом зі статусом `todo` або для задачі,
  що вже в To Do). Вихід із To Do і видалення прибирають id; задача To Do, якої нема у файлі
  (напр. поставлена в Obsidian), стає в кінець і не губиться. `list_tasks(status=todo)` (і колонка
  дашборда) віддає To Do в цьому порядку, а не за пріоритетом. `move_task` / `edit_task` у MCP
  `position` не мають — вхід у To Do за правилом вище (нагору з in progress/in review, інакше в кінець).
- `📌[…]` — короткий заголовок (≤50 символів) для канбану/календаря, **обовʼязковий** при
  створенні задачі (рядки без нього, зроблені руками в Obsidian, читаються без помилки)
- `^id` — стабільний адресний id (додається автоматично, Obsidian block-id)
- `text` — один рядок: перенос рядка в `add_task` / `edit_task` (MCP і дашборд) — помилка.
- **План** (`plan`) — багаторядковий markdown (кроки, секція «Фідбек» тощо) в окремому файлі
  `planner/plans/<id>.md`, а не в рядку задачі; формат рядка не змінюється. Стор читає лише
  `tasks.md` і `projects/*.md`, тож `- [ ]` усередині плану задачами не стають. Зберігається
  байт-у-байт. Задаєш: `add_task(plan=…)`, `edit_task(plan=…)` (порожній параметр — не чіпати,
  `none` — прибрати; у сторі й порожній текст прибирає), дашборд (поле «План» у діалозі, береться
  з `GET /api/task?id=…` — у списку `/api/tasks` плану нема). Читаєш: `get_task`. `delete_task`
  видаляє й файл плану. Зміна плану пишеться в журнал як поле `plan` (було → стало).

## Транспорти (патерн brain-access)
| Режим | Як увімкнути | Порт | Хто ходить |
|---|---|---|---|
| stdio | за замовчуванням | — | локальна розробка |
| http+bearer | `TASKS_TRANSPORT=http` або `--http` | 8792 | Mac (Claude Code) через tailnet |
| OAuth | `TASKS_TRANSPORT=oauth` або `--oauth` | 8793 | Claude-конектор (телефон/веб) і ChatGPT app, через Caddy/Funnel `/tasks` |

MCP-ендпоінт — шлях **`/tasks`** (як `/brain` і `/media`). Скоуп OAuth — `tasks`.

OAuth приймає лише дозволені `redirect_uri` (2026-09-30): `https://claude.ai/api/mcp/auth_callback`,
конектор ChatGPT `https://chatgpt.com/connector/oauth/L5rDATRQa6U6` і loopback (CLI-логін);
перевизначення — `TASKS_REDIRECT_ALLOWLIST` (через кому; `…/*` — один сегмент-ID, `loopback`).
Клієнти поза списком лишаються в стейті, але для сервера не існують. `/login` показує, хто просить
доступ, куди піде код і коли клієнта зареєстровано.

Журнал аудиту OAuth: `journalctl -u task-manager-oauth | grep OAUTH_AUDIT` — події `register`,
`register_rejected`, `login`, `login_failed`, `login_locked`, `refresh`: хто увійшов, який
клієнт (назва + перші 8 символів `client_id`) і куди йде код (хост + шлях, тобто ID конектора
ChatGPT). Без токенів, кодів і `rid`.

## ChatGPT app і Claude-конектор 🤖
Один і той самий OAuth-ендпоінт `https://<host>/tasks` для обох — DCR + PKCE, вхід паролем
(`TASKS_OAUTH_PASSWORD_HASH`).
- **ChatGPT** (web): Settings → Developer mode → Apps → **+** → Name `Planner`,
  URL `https://<host>/tasks`, Auth **OAuth** (DCR — реєструється сам) → форма пароля.
  Іконка: [`assets/task-manager-icon.png`](assets/task-manager-icon.png). Після зміни тулів —
  **Refresh** у налаштуваннях застосунку й новий чат.
- **Claude** (claude.ai / iOS): Settings → Connectors → Add custom connector → той самий URL.

**OAuth (як у brain-access):** токени привʼязані до `TASKS_RESOURCE_URL` (RFC 8707, за
замовч. = issuer) і scope рівно `tasks`; `ResourceGuard` перевіряє `resource` на `/token`;
refresh без розширення scope; прострочені й legacy-токени (без `resource`) вичищаються на
старті — після оновлення з версії без resource-binding конектори треба перелогінити один раз.
**Анотації тулів:** read-only — `board`, `list_tasks`, `get_task`, `history`; зміни — `add_task`,
`move_task`, `set_priority`, `set_deadline`; деструктивні (ChatGPT питає підтвердження) —
`edit_task` (очищення полів, перенос між файлами) і `delete_task`.
**Журнал:** правки з OAuth підписуються клієнтом токена — `vlad@chatgpt` / `vlad@claude`.

## Запуск
```bash
uv sync
uv run python -m unittest discover -s tests          # усі тести (88, офлайн, без секретів)
uv run coverage run -m unittest discover -s tests && uv run coverage report   # покриття, поріг 80%
uv run task_manager.py                    # MCP-сервер (stdio)
```

## Конфіг (env / .env)
Повний перелік — у `.env.example`. Головне:
- `TASKS_PLANNER_ROOT` — корінь planner (за замовч. `~/AI/planner`; на сервері — `/srv/planner`, задає конфіг інстансу).
- `TASKS_WIP_LIMIT` — ліміт in progress (за замовч. `15`).
- `TASKS_TOKEN` — bearer-секрет (генеруєш САМ, §8).
- `TASKS_ELF_TOKEN` — опційний другий bearer для раннера `elf` (генеруєш САМ, §8; порожньо = вимкнено).
  Лише на bearer-інстансі, лише з loopback (`127.0.0.1`/`::1`), лише тули `elf_*`, `list_tasks`,
  `get_task`, `board`; решта тулів і дашборд → 403; зміни в журналі з клієнтом `elf`.
  Задається в `/etc/task-manager/env` поруч із `TASKS_TOKEN` (`TASKS_ELF_TOKEN=<секрет>`), далі
  `sudo systemctl restart task-manager`. Раннер має ходити на `:8792` напряму: за reverse-проксі
  на тому ж хості перевірка loopback нічого не дає.
- `TASKS_OAUTH_PASSWORD_HASH` — хеш пароля OAuth (згенеруй: `python3 -c "from getpass import getpass; from tasks_oauth import hash_password; print(hash_password(getpass()))"` у теці репо; пароль вводиш сам).
- `TASKS_ISSUER_URL` — публічний issuer, напр. `https://<host>/tasks`.
- `TASKS_RESOURCE_URL` — resource для привʼязки токенів (за замовч. = issuer, якщо той закінчується на `/tasks`).

## Клієнти
Агенти (Claude Code, claude.ai, ChatGPT, Codex) підключаються через OAuth-конектор
`https://<host>/tasks` — токенів на Mac немає й синкати нічого не треба.
Bearer `:8792` лишається для веб-дашборда й прямих викликів у tailnet;
`TASKS_TOKEN` живе лише в `/etc/task-manager/env` на сервері.

## Деплой (systemd, юзер `tasks`)
- Юніти `task-manager` (bearer + дашборд, :8792) і `task-manager-oauth` (:8793) — від юзера `tasks`;
  юніт-файли ставиш сам.
- Код — цей чекаут (`~/AI/server/task-manager`, лише читання для `tasks`); venv — на **системному**
  Python (юзер `tasks` не бачить `~/.local/share/uv`):
```bash
cd ~/AI/server/task-manager && git pull --ff-only && uv sync --frozen --no-dev --python /usr/bin/python3
sudo systemctl restart task-manager task-manager-oauth
```
- Секрети — `/etc/task-manager/env` (root 0600, правити `sudoedit`); стан OAuth —
  `/var/lib/task-manager/oauth_state.json` (`TASKS_STATE_DIR`). `.env` у чекауті на сервері не лишається.
Health-чек:
```bash
curl -s -o /dev/null -w "bearer=%{http_code}\n" http://127.0.0.1:8792/tasks           # 401 = живий
curl -s -o /dev/null -w "oauth=%{http_code}\n"  http://127.0.0.1:8793/.well-known/oauth-authorization-server
curl -s -o /dev/null -w "public=%{http_code}\n" https://your-host.example.ts.net/.well-known/oauth-authorization-server/tasks
```

### Кольори тегів
`planner/tag-colors.json` — `{"<тег без #>": "#RRGGBB"}`. Це не файл задач: стор його не
читає як задачі, не журналює і не «запікає» в нього id. Формат кольору — лише `#RRGGBB`.
- `GET /api/tag-colors` → `{"tag_colors": {...}}`; той самий обʼєкт приходить у `GET /api/tasks`.
- `POST /api/tag-color` `{"tag": "дім", "color": "#12ab34"}` — поставити; `"color": null` —
  зняти. Не-hex (`red`, `#12345`, …) → 400, нічого не записується. Відповідь — усі кольори.
- Перейменування/міграція тегів — поза цією функцією (див. нижче).
- Тести: `tests/test_tag_colors.py`.

### Міграція тегів на англійську
`scripts/migrate_tags.py` перейменовує `#теги` у `tasks.md`, `projects/*.md` і шаблонах
`projects/<слаг>/template.md` за мапою та сіє дефолтні кольори маркерів (`to-agent` зелений,
`to-admin` помаранчевий, `dev` синій, `infra` фіолетовий, `design` рожевий, `blocked` червоний).
Вже заданий колір не перезаписується. Зміни йдуть у `.history` (`tool: migrate_tags`).
Запускати на проді вручну, спершу dry-run:
```bash
# mapping.json: {"розробка": "dev", "побут": "home"}   (теги без '#')
uv run python scripts/migrate_tags.py <planner root> --map mapping.json          # dry-run: теги, кількість задач, заміни
uv run python scripts/migrate_tags.py <planner root> --map mapping.json --apply  # запис
```
- Корінь planner — аргумент, інакше `TASKS_PLANNER_ROOT`, інакше `~/AI/planner`.
- Не-ASCII тег без пари в мапі → помилка, нічого не записується (також у dry-run).
- `#розробка #dev` в одній задачі → один `#dev`. Повторний запуск нічого не міняє.
- Ключі вже наявного `tag-colors.json` не перейменовуються: колір старого тега задай новому
  через дашборд або `POST /api/tag-color`.
- Тести: `tests/test_migrate_tags.py`.

## Файли
- `planner_model.py` — Task + парсер/рендер рядка, розбиття файлу на блоки (fence-safe).
- `planner_store.py` — читання/атомарний запис (flock), операції, правила переходів, WIP-гард.
- `task_manager.py` — MCP-сервер (MCPServer, SDK 2.x), тули + три транспорти.
- `tasks_oauth.py` — OAuth 2.1 AS (single-user, DCR + login-гейт, resource binding, `ResourceGuard`), клон `brain_oauth.py`.
- `tests/` — офлайн-тести за **ZOMBIES** (Zero/One/Many/Boundary/Interface/Exceptions/Simple),
  покриття рядків+гілок ≥80% (`fail_under` у `pyproject.toml`; станом на 2026-09-25 — 95%):
  - `test_store.py` — модель і стор (парсер, WIP, edit/перенос, журнал, шаблони);
  - `test_statuses.py` — статуси to do `[>]` / blockers `[b]` / in review `[r]`: парсинг, без ліміту, фільтр;
  - `test_todo_order.py` / `test_todo_order_api.py` — ручний порядок To Do: стор, MCP-тули, веб-API;
  - `test_transitions.py` — матриця переходів 7×7 для store і веб-API, WIP при to do → in progress;
  - `test_blockers_api.py` — blockers і переходи крізь MCP (backlog → to do заборонено) та дашборд;
  - `test_tools.py` — MCP-тули через `mcp.call_tool` (формат, фільтри, межі, помилки `⚠️`);
  - `test_web_api.py` — API дашборда + bearer-гейт `/tasks` на bearer-інстансі;
  - `test_oauth_server.py` — OAuth-інстанс: `/login` і лок, discovery, анотації, `ResourceGuard`, `vlad@chatgpt`;
  - `test_oauth_flow.py` — OAuth AS: resource/scope/PKCE/refresh, чистка токенів, паролі;
  - `test_tag_colors.py` — кольори тегів: стор, API, контракт розмітки дашборда;
  - `test_migrate_tags.py` — міграція тегів на англійську і сів кольорів маркерів;
  - `_support.py` — тимчасовий planner, імпорт сервера з env, ASGI-клієнт без мережі.
- `assets/task-manager-icon.png` — іконка для ChatGPT app / конектора.

> ⚠️ Потрібен **MCP SDK 2.x**: у 2.0 `FastMCP` перейменовано на `MCPServer`. Код мігровано 2026-09-06.
