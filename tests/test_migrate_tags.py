"""Міграція тегів на англійську (задача 1b4895): scripts/migrate_tags.py.

Запуск: uv run python -m unittest discover -s tests -p 'test_migrate_tags.py' -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import make_planner  # noqa: E402
from migrate_tags import MigrationError, migrate  # noqa: E402
from planner_history import HistoryLog  # noqa: E402
from planner_model import parse_task_line  # noqa: E402
from planner_store import PlannerStore  # noqa: E402

TASKS = (
    "# Tasks\n\n"
    "- [ ] Полагодити баг  #розробка #dev ^aaaaaa\n"
    "- [>] Купити хліб  #побут ^bbbbbb\n"
    "- [ ] Вже англійською  #infra ^cccccc\n"
)
PROJECT = "# Задачі — home\n\n- [ ] Кран  #побут #to-agent ^dddddd\n"
TEMPLATE = "- [ ] Щотижневий огляд  #розробка ^eeeeee\n"
MAP = {"розробка": "dev", "побут": "home"}


class MigrateTagsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_planner(Path(self._tmp.name))
        (self.root / "tasks.md").write_text(TASKS, encoding="utf-8")
        (self.root / "projects" / "home.md").write_text(PROJECT, encoding="utf-8")
        (self.root / "projects" / "home").mkdir()
        (self.root / "projects" / "home" / "template.md").write_text(TEMPLATE, encoding="utf-8")

    def snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def line(self, rel: str, task_id: str) -> str:
        for line in (self.root / rel).read_text(encoding="utf-8").splitlines():
            if line.endswith("^" + task_id):
                return line
        self.fail(f"{task_id} не знайдено у {rel}")

    def test_dry_run_reports_tag_counts_and_changes_nothing(self):
        before = self.snapshot()
        report = migrate(self.root, MAP)
        self.assertEqual(report["tags"],
                         {"розробка": 2, "dev": 1, "побут": 2, "infra": 1, "to-agent": 1})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / ".history").exists())
        self.assertFalse((self.root / "tag-colors.json").exists())

    def test_non_ascii_tag_missing_from_map_is_an_error_and_nothing_is_written(self):
        before = self.snapshot()
        with self.assertRaises(MigrationError) as cm:
            migrate(self.root, {"розробка": "dev"}, apply=True)
        self.assertIn("побут", str(cm.exception))
        self.assertEqual(self.snapshot(), before)

    def test_old_and_new_tag_in_one_task_collapse_to_a_single_new_tag(self):
        migrate(self.root, MAP, apply=True)
        line = self.line("tasks.md", "aaaaaa")
        self.assertEqual(parse_task_line(line).domains, ["dev"])
        self.assertEqual(line.count("#dev"), 1)
        self.assertNotIn("розробка", line)

    def test_tasks_in_tasks_md_and_project_files_are_migrated(self):
        migrate(self.root, MAP, apply=True)
        self.assertEqual(parse_task_line(self.line("tasks.md", "bbbbbb")).domains, ["home"])
        self.assertEqual(parse_task_line(self.line("projects/home.md", "dddddd")).domains,
                         ["home", "to-agent"])

    def test_project_templates_are_migrated_too(self):
        migrate(self.root, MAP, apply=True)
        tpl = (self.root / "projects" / "home" / "template.md").read_text(encoding="utf-8")
        self.assertEqual(parse_task_line(tpl.strip()).domains, ["dev"])
        self.assertNotIn("розробка", tpl)

    def test_changes_are_recorded_in_history(self):
        migrate(self.root, MAP, apply=True)
        recs = [r for r in HistoryLog(self.root).read(limit=50) if r["id"] == "aaaaaa"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["op"], "edit")
        self.assertEqual(recs[0]["tool"], "migrate_tags")
        self.assertEqual(recs[0]["file"], "tasks.md")
        self.assertEqual(recs[0]["changes"], {"domains": [["розробка", "dev"], ["dev"]]})

    def test_untouched_tasks_get_no_history_record(self):
        migrate(self.root, MAP, apply=True)
        ids = {r["id"] for r in HistoryLog(self.root).read(limit=50)}
        self.assertIn("aaaaaa", ids)
        self.assertNotIn("cccccc", ids)

    def test_migration_writes_are_not_reported_as_external_edits(self):
        PlannerStore(self.root).list_tasks()  # запамʼятати хеші файлів
        migrate(self.root, MAP, apply=True)
        PlannerStore(self.root).list_tasks()
        ops = [r["op"] for r in HistoryLog(self.root).read(limit=50)]
        self.assertIn("edit", ops)
        self.assertNotIn("external", ops)

    def test_existing_colors_are_kept_and_missing_marker_colors_are_added(self):
        (self.root / "tag-colors.json").write_text(
            json.dumps({"dev": "#111111", "custom": "#222222"}), encoding="utf-8")
        migrate(self.root, MAP, apply=True)
        colors = json.loads((self.root / "tag-colors.json").read_text(encoding="utf-8"))
        self.assertEqual(colors, {
            "dev": "#111111", "custom": "#222222",
            "to-agent": "#22c55e", "to-admin": "#f97316", "infra": "#8b5cf6",
            "design": "#ec4899", "blocked": "#ef4444",
        })

    def test_marker_colors_are_seeded_when_no_color_file_exists(self):
        migrate(self.root, MAP, apply=True)
        colors = json.loads((self.root / "tag-colors.json").read_text(encoding="utf-8"))
        self.assertEqual(colors["dev"], "#3b82f6")
        self.assertEqual(len(colors), 6)

    def test_second_apply_changes_nothing(self):
        migrate(self.root, MAP, apply=True)
        after_first = self.snapshot()
        migrate(self.root, MAP, apply=True)
        self.assertEqual(self.snapshot(), after_first)


if __name__ == "__main__":
    unittest.main()
