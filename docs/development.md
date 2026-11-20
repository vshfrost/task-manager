# Розробка

## Залежності й запуск

Проєкт керується через [uv](https://docs.astral.sh/uv/) (`package = false`, залежності зафіксовані в
`uv.lock`; вимога — Python ≥ 3.10, `.python-version` — 3.12).

```bash
uv sync                          # залежності разом із dev-групою (coverage)
uv run task_manager.py           # MCP-сервер (stdio)
```

Режими HTTP — `TASKS_TRANSPORT=http` / `oauth` або `--http` / `--oauth` ([oauth.md](oauth.md)); змінні —
[config.md](config.md).

> Потрібен **MCP SDK 2.x** (`mcp[cli]>=2.1`): у 2.0 `FastMCP` перейменовано на `MCPServer`, і код написано
> під нього.

## Тести

Усі тести офлайн, без секретів і живих сервісів: працюють на тимчасовому planner (`tests/_support.py`).

```bash
uv run python -m unittest discover -s tests                              # усі тести
uv run python -m unittest discover -s tests -p 'test_env_example.py' -v  # один файл
uv run coverage run -m unittest discover -s tests && uv run coverage report   # покриття
```

Покриття міряється за рядками й гілками (`branch = true`); `coverage report` завершується помилкою, якщо
покриття нижче **80 %** (`fail_under` у `pyproject.toml`). У вимірювання не входять `tests/*`, `scripts/*`
і `.venv/*`.

### Карта тестів

| Файл | Що перевіряє |
|---|---|
| `_support.py` | спільне: тимчасовий planner, імпорт сервера з потрібним env, ASGI-клієнт без мережі |
| `test_store.py` | модель і стор: парсер, WIP, редагування й перенос, журнал, шаблони, запобіжник дашборда |
| `test_statuses.py` | статуси `todo` / `blockers` / `in_review`: парсинг, round-trip, без ліміту, фільтр |
| `test_transitions.py` | матриця переходів для стору, MCP і веб-API; WIP при `todo → in_progress` |
| `test_blockers_api.py` | колонка blockers і переходи крізь MCP та дашборд |
| `test_todo_order.py` | ручний порядок To Do в сторі |
| `test_todo_order_api.py` | порядок To Do крізь MCP-тули й веб-API |
| `test_plan_store.py` | план задачі (`plans/<id>.md`) у сторі |
| `test_plan_api.py` | поле `plan` через MCP і веб-API; `text` лише в один рядок |
| `test_comment.py` | `comment_task`: коментар у розділ `## Elf` плану |
| `test_projects.py` | реєстр проєктів `projects.json`: стор і MCP-тули |
| `test_projects_api.py` | реєстр проєктів: веб-API і форма «Новий проєкт» |
| `test_tag_colors.py` | кольори тегів: стор, API, розмітка дашборда |
| `test_tools.py` | MCP-тули через `mcp.call_tool`: формат, фільтри, межі, помилки |
| `test_json_api.py` | `format="json"` у `list_tasks` / `get_task` |
| `test_web_api.py` | API дашборда й bearer-гейт MCP-шляху |
| `test_elf_work.py` | робочий цикл Elf: `elf_claim` / `elf_release` / `elf_block` |
| `test_elf_review.py` | рев'ю-цикл Elf: `elf_submit` / `elf_ci` / `elf_review_next` / `elf_merged` / `elf_return` |
| `test_elf_audit.py` | `elf_audit` і токен `elf` |
| `test_elf_token.py` | обмежений токен `TASKS_ELF_TOKEN` на bearer-інстансі |
| `test_oauth_server.py` | OAuth-інстанс: `/login` і блокування, discovery, анотації, `ResourceGuard` |
| `test_oauth_flow.py` | OAuth-сервер: resource, scope, PKCE, refresh, чистка токенів, паролі |
| `test_oauth_audit.py` | журнал `OAUTH_AUDIT` |
| `test_redirect_allowlist.py` | allowlist `redirect_uri` |
| `test_state_dir.py` | `TASKS_STATE_DIR`: де лежить стан OAuth і з якими правами |
| `test_env_example.py` | `.env.example`: лише `KEY=value`, без `export`, ліміт 15 |
| `test_migrate_tags.py` | `scripts/migrate_tags.py` |
| `test_migrate_backlog.py` | `scripts/migrate_backlog.py` |

## CI

Workflow `.github/workflows/ci.yml` запускає `scripts/ci.sh` на GitHub (`push` у `main`, pull request,
ручний запуск; секретів не використовує). Скрипт виконує по черзі:

1. `bash -n` і `shellcheck` для трекованих shell-файлів (у CI `shellcheck` обовʼязковий);
2. `uv sync --frozen`;
3. перевірку синтаксису всіх трекованих `.py`-файлів;
4. тести з покриттям (`coverage run` і `coverage report` із порогом `fail_under`).

## Скрипти міграції

Обидва скрипти приймають корінь planner першим аргументом, інакше беруть `TASKS_PLANNER_ROOT`
(значення за замовчуванням — [config.md](config.md)). Повторний запуск нічого не міняє.

### `scripts/migrate_tags.py`

Перейменовує `#теги` у `tasks.md`, `projects/*.md` і шаблонах `projects/<слаг>/template.md` за мапою й
сіє дефолтні кольори маркерів у `tag-colors.json` (`to-agent` — зелений, `to-admin` — помаранчевий, `dev` —
синій, `infra` — фіолетовий, `design` — рожевий, `blocked` — червоний); вже заданий колір не
перезаписується. Зміни йдуть у журнал (`tool: migrate_tags`). За замовчуванням — dry-run.

```bash
# mapping.json: {"старий-тег": "new-tag"}   (теги без '#')
uv run python scripts/migrate_tags.py <planner root> --map mapping.json          # dry-run: теги, кількість задач, заміни
uv run python scripts/migrate_tags.py <planner root> --map mapping.json --apply  # запис
```

- Тег із не-ASCII символами без пари в мапі → помилка, нічого не записується (також у dry-run).
- Якщо після заміни в задачі лишається двічі той самий тег, лишається один.
- Ключі наявного `tag-colors.json` не перейменовуються: колір старого тега треба задати новому окремо
  (дашборд або `POST /api/tag-color`).

### `scripts/migrate_backlog.py`

Одноразова міграція журналу: у `.history/*.jsonl` статус `todo` у `changes.status` і `snapshot.status`
замінюється на `backlog` (колонку `[ ]` перейменовано з `todo` на `backlog`, а ключ `todo` віддано новій
колонці). Задачі й `.state.json` не змінюються. Запускати один раз, **до** того, як `todo` почне означати
нову колонку.

```bash
uv run python scripts/migrate_backlog.py <planner root>
```
