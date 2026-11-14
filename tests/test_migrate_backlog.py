"""Міграція журналу scripts/migrate_backlog.py: статус `todo` → `backlog` у .history/*.jsonl.

Запуск: uv run python -m unittest discover -s tests -p 'test_migrate_backlog.py' -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from migrate_backlog import migrate  # noqa: E402

ADD = {"ts": "2026-09-01T10:00:00+02:00", "op": "add", "tool": "add_task", "id": "aaaaaa",
       "file": "tasks.md", "client": "web", "user": "vlad",
       "changes": {"status": [None, "todo"], "text": [None, "todo list"]}}
EDIT = {"ts": "2026-09-02T10:00:00+02:00", "op": "edit", "tool": "move_task", "id": "aaaaaa",
        "file": "tasks.md", "client": "claude", "user": "vlad",
        "changes": {"status": ["todo", "in_progress"]}}
BACK = {"ts": "2026-09-03T10:00:00+02:00", "op": "edit", "tool": "move_task", "id": "aaaaaa",
        "file": "tasks.md", "client": "claude", "user": "vlad",
        "changes": {"status": ["testing", "todo"]}}
DELETE = {"ts": "2026-09-04T10:00:00+02:00", "op": "delete", "tool": "delete_task", "id": "bbbbbb",
          "file": "tasks.md", "client": "web", "user": "vlad",
          "snapshot": {"id": "bbbbbb", "status": "todo", "text": "x"}}
UNTOUCHED = {"ts": "2026-09-05T10:00:00+02:00", "op": "edit", "tool": "set_priority", "id": "cccccc",
             "file": "tasks.md", "client": "web", "user": "vlad",
             "changes": {"status": ["testing", "done"], "text": ["todo", "todo-лист"]}}
EXTERNAL = {"ts": "2026-09-06T10:00:00+02:00", "op": "external", "tool": "-", "id": "-",
            "file": "tasks.md", "client": "local", "user": "vlad"}


def write(path: Path, recs) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class MigrateBacklogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.hist = self.root / ".history"
        self.hist.mkdir()

    def test_todo_status_becomes_backlog_in_changes_and_snapshot(self):
        write(self.hist / "2026-09.jsonl", [ADD, EDIT, BACK, DELETE])
        self.assertEqual(migrate(self.root), 4)
        add, edit, back, delete = read(self.hist / "2026-09.jsonl")
        self.assertEqual(add["changes"]["status"], [None, "backlog"])
        self.assertEqual(edit["changes"]["status"], ["backlog", "in_progress"])
        self.assertEqual(back["changes"]["status"], ["testing", "backlog"])
        self.assertEqual(delete["snapshot"]["status"], "backlog")

    def test_other_fields_and_records_are_untouched(self):
        write(self.hist / "2026-09.jsonl", [ADD, UNTOUCHED, EXTERNAL])
        self.assertEqual(migrate(self.root), 1)
        add, untouched, external = read(self.hist / "2026-09.jsonl")
        self.assertEqual(add["changes"]["text"], [None, "todo list"], "слово todo у тексті не чіпаємо")
        self.assertEqual({k: v for k, v in add.items() if k != "changes"},
                         {k: v for k, v in ADD.items() if k != "changes"})
        self.assertEqual(untouched, UNTOUCHED)
        self.assertEqual(external, EXTERNAL)

    def test_second_run_changes_nothing(self):
        f = self.hist / "2026-09.jsonl"
        write(f, [ADD, EDIT])
        self.assertEqual(migrate(self.root), 2)
        first = f.read_bytes()
        self.assertEqual(migrate(self.root), 0)
        self.assertEqual(f.read_bytes(), first)

    def test_all_month_files_are_migrated_and_state_file_is_left_alone(self):
        write(self.hist / "2026-09.jsonl", [ADD])
        write(self.hist / "2026-10.jsonl", [EDIT, UNTOUCHED])
        state = self.hist / ".state.json"
        state.write_text('{"tasks.md": "todo"}', encoding="utf-8")
        self.assertEqual(migrate(self.root), 2)
        self.assertEqual(state.read_text(encoding="utf-8"), '{"tasks.md": "todo"}')

    def test_task_markdown_is_not_touched(self):
        md = self.root / "tasks.md"
        md.write_text("# Tasks\n- [ ] x ^aaaaaa\n", encoding="utf-8")
        write(self.hist / "2026-09.jsonl", [ADD])
        migrate(self.root)
        self.assertEqual(md.read_text(encoding="utf-8"), "# Tasks\n- [ ] x ^aaaaaa\n")

    def test_missing_history_dir_is_zero_changes(self):
        self.hist.rmdir()
        self.assertEqual(migrate(self.root), 0)


if __name__ == "__main__":
    unittest.main()
