"""
migrate_backlog — одноразова міграція журналу: статус `todo` → `backlog`.
=======================================================================

Перейменування колонки (cf9047): `[ ]` у задач тепер статус `backlog`, ключ `todo`
зарезервовано під нову колонку. У `.md` задач нічого міняти не треба (`[ ]` лишається
`[ ]`), а в журналі `<planner>/.history/*.jsonl` старі записи мають статус `todo` у
`changes.status` (пара [було, стало]) і `snapshot.status`. Скрипт переписує лише їх;
решту полів і записів, а також `.state.json` і самі задачі, не чіпає.

ЗАПУСКАТИ ОДИН РАЗ ДО деплою колонки `todo` (07bc93): після неї `todo` вже значить інше.
Повторний запуск безпечний (нічого не змінює).

    uv run python scripts/migrate_backlog.py /path/to/planner # корінь planner = аргумент
    uv run python scripts/migrate_backlog.py                  # інакше TASKS_PLANNER_ROOT, інакше ~/AI/planner
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

OLD, NEW = "todo", "backlog"


def _fix(rec: dict) -> bool:
    """Міняє todo → backlog у статусних полях запису. True, якщо щось змінилось."""
    changed = False
    pair = (rec.get("changes") or {}).get("status")
    if isinstance(pair, list):
        for i, v in enumerate(pair):
            if v == OLD:
                pair[i] = NEW
                changed = True
    snap = rec.get("snapshot")
    if isinstance(snap, dict) and snap.get("status") == OLD:
        snap["status"] = NEW
        changed = True
    return changed


def _migrate_file(path: Path) -> int:
    out, changed = [], 0
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        try:
            rec = json.loads(line)
        except ValueError:
            out.append(line)  # зіпсований рядок лишаємо як є
            continue
        if isinstance(rec, dict) and _fix(rec):
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            changed += 1
        out.append(line)
    if changed:
        # атомарно: тимчасовий файл поруч + rename, права оригіналу зберігаємо
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(out))
            os.chmod(tmp, path.stat().st_mode & 0o7777)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return changed


def migrate(root) -> int:
    """Переписує `<root>/.history/*.jsonl`; повертає кількість змінених записів."""
    hist = Path(root) / ".history"
    if not hist.is_dir():
        return 0
    return sum(_migrate_file(p) for p in sorted(hist.glob("*.jsonl")))


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] in ("-h", "--help")):
        print(__doc__)
        return 2
    root = argv[1] if len(argv) == 2 else os.environ.get(
        "TASKS_PLANNER_ROOT", str(Path.home() / "AI" / "planner"))
    print(f"{root}: змінено записів журналу: {migrate(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
