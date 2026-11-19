# Розгортання

Узагальнений опис: два процеси одного коду під systemd. Конкретні хости, шляхи й імена користувачів
залежать від середовища й тут позначені плейсхолдерами: `<host>` — публічне імʼя сервера, `<planner root>`
— каталог planner, `<code dir>` — чекаут репозиторію, `<state dir>` — каталог стану, `<env file>` — файл
змінних, `<service user>` — користувач сервісу.

## Два інстанси

| Інстанс | `TASKS_TRANSPORT` | Порт за замовчуванням | Що обслуговує |
|---|---|---|---|
| bearer | `http` | `8792` | MCP за bearer-токеном **і веб-дашборд** ([dashboard.md](dashboard.md)); лише довірена мережа |
| OAuth | `oauth` | `8793` | MCP для OAuth-клієнтів (Claude, ChatGPT); може бути за публічним reverse-проксі; дашборда немає |

Обидва слухають `127.0.0.1` (`TASKS_HTTP_HOST`) і працюють з тим самим `<planner root>`. Дашборд без
авторизації, тому bearer-інстанс **не можна** виставляти за публічний проксі, а OAuth-інстанс — це
єдиний, що публікується. Деталі — [dashboard.md](dashboard.md) і [oauth.md](oauth.md).

Конфіг reverse-проксі в репозиторії не зберігається. Код враховує, що проксі може зрізати зовнішній
префікс `/tasks` перед передачею запиту OAuth-інстансу: форма входу постить на відносну адресу `login`, а
`ResourceGuard` стежить за `/token` без префікса.

## Права сервісу

Сервісу потрібно:

- **читати** код (`<code dir>`) і його віртуальне середовище;
- **писати** у `<planner root>` (задачі, `plans/`, `.history/`, службові `*.json`) і в `<state dir>`
  (`oauth_state.json`; без `TASKS_STATE_DIR` — `.oauth_state.json` поруч із кодом).

Більше нічого на хості сервісу не потрібно, тож користувача сервісу варто обмежити саме цими шляхами.

## Юніти systemd

Секрети й конфіг — у `<env file>` (формат `KEY=value`, коментарі окремими рядками; доступ лише для
власника). Ілюстративний приклад юніта bearer-інстансу (для OAuth-інстансу — `TASKS_TRANSPORT=oauth` і
своя `TASKS_CLIENT`):

```ini
[Unit]
Description=task-manager (bearer)
After=network.target

[Service]
User=<service user>
WorkingDirectory=<code dir>
EnvironmentFile=<env file>
Environment=TASKS_TRANSPORT=http
Environment=TASKS_CLIENT=<мітка інстансу>
Environment=TASKS_PLANNER_ROOT=<planner root>
Environment=TASKS_STATE_DIR=<state dir>
ExecStart=<code dir>/.venv/bin/python task_manager.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Змінні й дефолти — [config.md](config.md). Для OAuth-інстансу обовʼязкові `TASKS_ISSUER_URL`,
`TASKS_ALLOWED_HOSTS=<host>` і пароль (`TASKS_OAUTH_PASSWORD_HASH`). `.env` у чекауті на сервері краще не
тримати: секрети живуть лише в `<env file>`.

## Оновлення

Віртуальне середовище сервісу будується на **системному** Python:

```bash
cd <code dir> && git pull --ff-only && uv sync --frozen --no-dev --python /usr/bin/python3
sudo systemctl restart <юніт bearer> <юніт oauth>
```

Чому `--python /usr/bin/python3`: інтерпретатор, яким керує `uv`, лежить у домашньому каталозі
користувача, що запускає `uv`, а користувач сервісу цього каталогу не бачить; venv на такому інтерпретаторі
не запуститься під сервісним користувачем. `--frozen` бере залежності з `uv.lock` без перерахунку,
`--no-dev` пропускає dev-залежності (coverage).

Після зміни набору MCP-тулів OAuth-клієнти (ChatGPT) варто оновити в налаштуваннях і почати новий чат.

## Health-check

```bash
# bearer-інстанс: 401 без токена = процес живий
curl -s -o /dev/null -w "bearer=%{http_code}\n" http://127.0.0.1:8792/tasks
# OAuth-інстанс, локально
curl -s -o /dev/null -w "oauth=%{http_code}\n"  http://127.0.0.1:8793/.well-known/oauth-authorization-server
# через публічний проксі
curl -s -o /dev/null -w "public=%{http_code}\n" https://<host>/.well-known/oauth-authorization-server/tasks
```

Журнал OAuth-інстансу: `journalctl -u <юніт oauth> | grep OAUTH_AUDIT` ([oauth.md](oauth.md)).
