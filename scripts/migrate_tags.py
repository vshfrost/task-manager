"""
migrate_tags — міграція тегів (#домен) на англійську + стартові кольори маркерів.
================================================================================

Теги — це `#тег` у рядках задач `tasks.md`, `projects/*.md` і шаблонів
`projects/<слаг>/template.md`. Скрипт перейменовує їх за мапою і сіє дефолтні кольори
маркерів у `tag-colors.json` (існуючі кольори НІКОЛИ не перезаписує).

За замовчуванням — dry-run: друкує кожен тег із кількістю задач і його заміну; нічого
не пише. Запис лише з `--apply`. Тег із не-ASCII символами без пари в мапі → помилка,
нічого не записується. Якщо після заміни в задачі лишається двічі той самий тег
(`#розробка #dev` → `#dev #dev`), лишається один.

Зміни пишуться в `.history` (op `edit`, tool `migrate_tags`), як інші правки стору.
Рядки без змін не чіпаються. Повторний запуск безпечний.

    uv run python scripts/migrate_tags.py /srv/planner --map mapping.json            # dry-run
    uv run python scripts/migrate_tags.py /srv/planner --map mapping.json --apply
    # корінь planner: аргумент, інакше TASKS_PLANNER_ROOT, інакше ~/AI/planner
    # mapping.json: {"розробка": "dev", "побут": "home"}  (теги без '#')
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner_history import HistoryLog, diff, sha256_text  # noqa: E402
from planner_model import Task, parse_file  # noqa: E402
from planner_store import PlannerStore  # noqa: E402

TOOL = "migrate_tags"
# Дефолтні кольори маркерів (тег без '#' → #RRGGBB).
MARKER_COLORS = {
    "to-agent": "#22c55e",   # зелений
    "to-admin": "#f97316",   # помаранчевий
    "dev": "#3b82f6",        # синій
    "infra": "#8b5cf6",      # фіолетовий
    "design": "#ec4899",     # рожевий
    "blocked": "#ef4444",    # червоний
}
_NEW_TAG_RE = re.compile(r"^[^\s#]+$")


class MigrationError(Exception):
    """Міграцію не можна виконати (нічого не записано)."""


def _files(root: Path) -> list[str]:
    """Файли з задачами: tasks.md, projects/*.md і шаблони projects/*/template.md."""
    rels = [r for r in ["tasks.md"] if (root / r).is_file()]
    proj = root / "projects"
    if proj.is_dir():
        rels += [f"projects/{p.name}" for p in sorted(proj.glob("*.md"))]
        rels += [f"projects/{p.parent.name}/template.md" for p in sorted(proj.glob("*/template.md"))]
    return rels


def _new_tags(tags: list[str], mapping: dict[str, str]) -> list[str]:
    """Тег → нова назва; дублі після заміни прибираються, порядок зберігається."""
    out: list[str] = []
    for t in tags:
        t = mapping.get(t, t)
        if t not in out:
            out.append(t)
    return out


def _render(task: Task) -> str:
    line = task.render()
    # Рядок без ^id лишаємо без нього: id допече стор, а шаблони id не мають.
    return line if task.had_id else re.sub(r"\s\^[A-Za-z0-9]+$", "", line)


def migrate(root, mapping: dict[str, str] | None = None, apply: bool = False) -> dict:
    """Звіт: {"tags": {тег: кількість задач}, "changes": кількість змінених задач,
    "colors_added": {тег: колір}}. Без `apply` нічого не пишеться."""
    root = Path(root)
    mapping = mapping or {}
    for old, new in mapping.items():
        if not (isinstance(old, str) and isinstance(new, str) and _NEW_TAG_RE.match(new)):
            raise MigrationError(f"некоректна пара мапи: {old!r} → {new!r}")

    parsed = {rel: (root / rel).read_text(encoding="utf-8") for rel in _files(root)}
    tags: dict[str, int] = {}
    for rel, raw in parsed.items():
        for kind, t in parse_file(raw, rel):
            if kind == "task":
                for d in set(t.domains):  # type: ignore[union-attr]
                    tags[d] = tags.get(d, 0) + 1
    unmapped = sorted(t for t in tags if not t.isascii() and t not in mapping)
    if unmapped:
        raise MigrationError("не-ASCII теги без пари в мапі: " + ", ".join(unmapped))

    # (файл, новий текст, [(id, зміни)]) лише для файлів із правками
    edits: list[tuple[str, str, list[tuple[str, dict]]]] = []
    changes = 0
    for rel, raw in parsed.items():
        lines, recs = raw.split("\n"), []  # блок = рядок, 1:1
        for i, (kind, t) in enumerate(parse_file(raw, rel)):
            if kind != "task":
                continue
            new = _new_tags(t.domains, mapping)  # type: ignore[union-attr]
            if new == t.domains:  # type: ignore[union-attr]
                continue
            before = t.to_dict()  # type: ignore[union-attr]
            t.domains = new  # type: ignore[union-attr]
            lines[i] = _render(t)  # type: ignore[arg-type]
            recs.append((t.id if t.had_id else "-", diff(before, t.to_dict())))  # type: ignore[union-attr]
        if recs:
            changes += len(recs)
            edits.append((rel, "\n".join(lines), recs))

    store = PlannerStore(root)
    colors = store.tag_colors()
    colors_added = {t: c for t, c in MARKER_COLORS.items() if t not in colors}

    if apply:
        history = HistoryLog(root)
        state = history.load_state()
        for rel, text, recs in edits:
            path = root / rel
            known = state.get(rel)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            for task_id, ch in recs:
                history.record("edit", tool=TOOL, task_id=task_id, file=rel, changes=ch)
            # Хеш власного запису — щоб стор не вважав це зовнішньою правкою
            # (лише якщо до того файл був «чистий» або невідомий).
            if known is None or known == sha256_text(parsed[rel]):
                if not rel.endswith("/template.md"):
                    history.update_hashes({rel: sha256_text(text)})
        for tag, color in colors_added.items():
            store.set_tag_color(tag, color)

    return {"tags": tags, "changes": changes, "colors_added": colors_added}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Міграція тегів на англійську (dry-run за замовчуванням).")
    ap.add_argument("root", nargs="?", default=os.environ.get(
        "TASKS_PLANNER_ROOT", str(Path.home() / "AI" / "planner")))
    ap.add_argument("--map", dest="map_file", help='JSON {"старий": "новий"}, теги без #')
    ap.add_argument("--apply", action="store_true", help="записати зміни (інакше dry-run)")
    args = ap.parse_args(argv[1:])
    try:
        mapping = json.loads(Path(args.map_file).read_text(encoding="utf-8")) if args.map_file else {}
        if not isinstance(mapping, dict):
            raise MigrationError("мапа має бути JSON-обʼєктом")
        report = migrate(args.root, mapping, apply=args.apply)
    except (MigrationError, OSError, ValueError) as e:
        print(f"ПОМИЛКА: {e}\nНічого не записано.", file=sys.stderr)
        return 1
    for tag, n in sorted(report["tags"].items()):
        print(f"#{tag}  задач: {n}  →  " + (f"#{mapping[tag]}" if tag in mapping else "без змін"))
    for tag, color in report["colors_added"].items():
        print(f"колір {tag}: {color}")
    mode = "ЗАПИСАНО" if args.apply else "dry-run, нічого не записано (додай --apply)"
    print(f"{args.root}: змінено задач: {report['changes']}; {mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
