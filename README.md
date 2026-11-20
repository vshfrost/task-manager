# task-manager 📋

MCP-сервер керування задачами над каталогом **planner**: markdown-файли — джерело правди. Дає MCP-тули
(23) для AI-клієнтів, веб-дашборд (канбан + календар), журнал змін і три транспорти: stdio, HTTP + bearer,
HTTP + OAuth.

## Швидкий старт
```bash
uv sync
uv run python -m unittest discover -s tests   # тести (офлайн, без секретів)
TASKS_PLANNER_ROOT=<planner root> uv run task_manager.py   # MCP-сервер (stdio)
```
Режими HTTP, змінні оточення й розгортання — у документації нижче. Потрібен MCP SDK 2.x.

## Документація
- [docs/format.md](docs/format.md) — формат задачі, статуси, переходи, WIP, плани, шаблони
- [docs/tools.md](docs/tools.md) — MCP-тули
- [docs/dashboard.md](docs/dashboard.md) — дашборд, REST API, безпека
- [docs/history.md](docs/history.md) — журнал змін
- [docs/oauth.md](docs/oauth.md) — транспорти, OAuth, підключення ChatGPT і Claude
- [docs/config.md](docs/config.md) — змінні оточення
- [docs/deploy.md](docs/deploy.md) — розгортання
- [docs/development.md](docs/development.md) — тести, CI, міграції

Повний індекс — [docs/README.md](docs/README.md). Ліцензія — [LICENSE](LICENSE).
