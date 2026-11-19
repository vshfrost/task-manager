# Конфігурація (змінні оточення)

Сервіс читає лише змінні оточення з префіксом `TASKS_`. `task_manager.py` на старті підвантажує файл
`.env` поруч із кодом (якщо встановлено `python-dotenv`; значення, уже задані в оточенні, не перекриваються).
Шаблон — `.env.example` у корені репозиторію. Під systemd змінні задаються через `EnvironmentFile`
([deploy.md](deploy.md)); цей формат **не відкидає коментар у кінці рядка** (`TASKS_WIP_LIMIT=4  # …`
дало б значення `4  # …`), тож у файлі лише `KEY=value`, коментарі — окремими рядками (стереже
`tests/test_env_example.py`).

Секрети (`TASKS_TOKEN`, `TASKS_ELF_TOKEN`, `TASKS_OAUTH_PASSWORD_HASH`, `TASKS_OAUTH_PASSWORD`) не
комітяться й не генеруються кодом: значення вводить власник сервісу.

## Усі змінні

| Змінна | За замовчуванням (з коду) | Призначення | У `.env.example` |
|---|---|---|---|
| `TASKS_PLANNER_ROOT` | `AI/planner` у домашній теці користувача (`Path.home() / "AI" / "planner"`) | корінь planner; `~` розкривається | так |
| `TASKS_WIP_LIMIT` | `15` | ліміт задач у `in_progress`; має бути цілим числом | так |
| `TASKS_STATE_DIR` | не задано → `.oauth_state.json` поруч із кодом | каталог стану OAuth: `$TASKS_STATE_DIR/oauth_state.json` | так (закоментовано) |
| `TASKS_TRANSPORT` | не задано → stdio | `http` — bearer-інстанс, `oauth` — OAuth-інстанс (аналог прапорців `--http` / `--oauth`) | **ні** |
| `TASKS_HTTP_HOST` | `127.0.0.1` | адреса, на якій слухає HTTP-режим | **ні** |
| `TASKS_HTTP_PORT` | `8792` у режимі `http`, `8793` у режимі `oauth` | порт HTTP-режиму | **ні** |
| `TASKS_HTTP_PATH` | `/tasks` | шлях MCP-ендпоінта | так (закоментовано) |
| `TASKS_TOKEN` | — (без нього bearer-інстанс не стартує) | bearer-секрет | так |
| `TASKS_ELF_TOKEN` | не задано → доступ `elf` вимкнено | другий, обмежений bearer для раннера Elf ([tools.md](tools.md#права-токена-elf)) | так |
| `TASKS_ISSUER_URL` | `http://localhost:8793` | публічний issuer OAuth, напр. `https://<host>/tasks` | так |
| `TASKS_RESOURCE_URL` | дорівнює issuer, якщо той закінчується на `/tasks`, інакше issuer + `/tasks` | resource для привʼязки токенів (RFC 8707) | так (закоментовано) |
| `TASKS_OAUTH_PASSWORD_HASH` | — | хеш пароля OAuth (`pbkdf2_sha256$…`); має пріоритет над відкритим паролем | так |
| `TASKS_OAUTH_PASSWORD` | — | відкритий пароль OAuth, запасний варіант лише коли хеш не заданий | **ні** |
| `TASKS_REDIRECT_ALLOWLIST` | не задано → типовий список у `tasks_oauth.py` | дозволені `redirect_uri` через кому ([oauth.md](oauth.md)) | **ні** |
| `TASKS_ALLOWED_HOSTS` | порожньо; завжди дозволені `127.0.0.1` і `localhost` (з портом і без) | додаткові значення заголовка `Host` через кому | так |
| `TASKS_ALLOWED_ORIGINS` | порожньо; завжди дозволено `*` | додаткові значення `Origin` через кому | так (закоментовано) |
| `TASKS_CLIENT` | `local` | мітка інстансу в [журналі змін](history.md) | так (закоментовано) |
| `TASKS_DEFAULT_USER` | `vlad` | користувач у журналі, коли токена нема (stdio, bearer) | **ні** |

Без `TASKS_OAUTH_PASSWORD_HASH` і `TASKS_OAUTH_PASSWORD` OAuth-інстанс не стартує.

Скрипти з `scripts/` теж читають `TASKS_PLANNER_ROOT`, коли корінь не передано аргументом
([development.md](development.md)).

## Хеш пароля OAuth

Рядок для `TASKS_OAUTH_PASSWORD_HASH` генерується в каталозі репозиторію, у середовищі з встановленими
залежностями (`tasks_oauth` імпортує MCP SDK); пароль вводиться інтерактивно:

```bash
uv run python -c "from getpass import getpass; from tasks_oauth import hash_password; print(hash_password(getpass()))"
```

Вивід має вигляд `pbkdf2_sha256$<ітерації>$<сіль hex>$<хеш hex>`.
