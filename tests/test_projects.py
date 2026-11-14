"""Реєстр проєктів (задача 29cf3f): `projects.json`, стор і MCP-тули.

Запуск: uv run python -m unittest tests.test_projects -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _support import ServerCase, make_planner  # noqa: E402
from planner_store import PlannerError, PlannerStore  # noqa: E402


class StoreProjectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_planner(Path(self._tmp.name))
        self.file = self.root / "projects.json"
        self.store = PlannerStore(self.root)

    def test_no_projects_at_all_is_an_empty_list(self):
        self.assertEqual(self.store.list_projects(), [])
        self.assertFalse(self.file.exists())

    def test_created_project_is_listed_with_name_and_repo(self):
        created = self.store.create_project("alpha", "Alpha Project", "acme/alpha")
        self.assertEqual(created, {"slug": "alpha", "name": "Alpha Project", "repo": "acme/alpha"})
        self.assertEqual(
            self.store.list_projects(),
            [{"slug": "alpha", "name": "Alpha Project", "repo": "acme/alpha"}],
        )

    def test_created_project_is_in_vocab_even_without_tasks(self):
        self.store.create_project("alpha", "Alpha Project", "acme/alpha")
        self.assertEqual(self.store.vocab()["projects"], ["alpha", "global"])

    def test_registry_is_a_sidecar_file_that_survives_restart(self):
        self.store.create_project("alpha", "Alpha Project", "acme/alpha")
        self.assertTrue(self.file.exists())
        self.assertEqual(
            json.loads(self.file.read_text(encoding="utf-8")),
            {"alpha": {"name": "Alpha Project", "repo": "acme/alpha"}},
        )
        self.assertEqual(
            PlannerStore(self.root).list_projects(),
            [{"slug": "alpha", "name": "Alpha Project", "repo": "acme/alpha"}],
        )

    def test_registry_file_is_not_a_task_file(self):
        self.store.create_project("alpha", "Alpha Project", "acme/alpha")
        self.store.add_task("Задача", title="Задача")  # повний _load() над усім planner
        self.assertEqual([t["file"] for t in self.store.list_tasks()], ["tasks.md"])
        self.assertNotIn("external", [r["op"] for r in self.store.history.read(None, 50)])

    def test_duplicate_slug_is_refused_and_registry_is_unchanged(self):
        self.store.create_project("alpha", "Alpha Project", "acme/alpha")
        self.assertTrue(self.file.exists())
        before = self.file.read_text(encoding="utf-8")
        with self.assertRaises(PlannerError):
            self.store.create_project("alpha", "Other Name", "acme/other")
        self.assertEqual(self.file.read_text(encoding="utf-8"), before)
        self.assertEqual(
            self.store.list_projects(),
            [{"slug": "alpha", "name": "Alpha Project", "repo": "acme/alpha"}],
        )

    def test_invalid_slug_is_refused_and_nothing_is_written(self):
        for bad in ("", "  ", "-alpha", "Alpha", "my project", "my_project", "a/b", "альфа", "global"):
            with self.subTest(slug=bad):
                with self.assertRaises(PlannerError):
                    self.store.create_project(bad, "Name", "")
                self.assertFalse(self.file.exists())

    def test_valid_slugs_are_accepted(self):
        for ok in ("a", "alpha", "alpha-2", "2fa", "a-b-c"):
            with self.subTest(slug=ok):
                self.assertEqual(self.store.create_project(ok, "Name", "")["slug"], ok)

    def test_empty_name_is_refused(self):
        for bad in ("", "   "):
            with self.subTest(name=bad):
                with self.assertRaises(PlannerError):
                    self.store.create_project("alpha", bad, "")
                self.assertFalse(self.file.exists())

    def test_repo_not_owner_slash_name_is_refused_and_nothing_is_written(self):
        for bad in ("acme", "acme/", "/alpha", "acme/alpha/extra", "acme alpha", "acme/al pha",
                    "https://github.com/acme/alpha", "git@github.com:acme/alpha.git"):
            with self.subTest(repo=bad):
                with self.assertRaises(PlannerError):
                    self.store.create_project("alpha", "Alpha", bad)
                self.assertFalse(self.file.exists())

    def test_empty_repo_is_allowed(self):
        self.assertEqual(
            self.store.create_project("alpha", "Alpha", ""),
            {"slug": "alpha", "name": "Alpha", "repo": ""},
        )
        self.assertEqual(self.store.create_project("beta", "Beta", "   ")["repo"], "")

    def test_owner_name_with_dots_dashes_and_underscores_is_a_valid_repo(self):
        created = self.store.create_project("alpha", "Alpha", "acme-corp/web_app.v2")
        self.assertEqual(created["repo"], "acme-corp/web_app.v2")

    def test_project_that_exists_only_as_a_task_file_is_listed_with_empty_name_and_repo(self):
        self.store.add_task("Задача", title="Задача", project="home")
        self.assertEqual(self.store.list_projects(), [{"slug": "home", "name": "", "repo": ""}])

    def test_registry_entry_wins_over_its_task_file_and_is_listed_once(self):
        self.store.add_task("Задача", title="Задача", project="alpha")
        self.store.create_project("alpha", "Alpha Project", "acme/alpha")
        self.assertEqual(
            self.store.list_projects(),
            [{"slug": "alpha", "name": "Alpha Project", "repo": "acme/alpha"}],
        )

    def test_list_is_sorted_by_slug_and_never_contains_global(self):
        self.store.add_task("Глобальна", title="Глобальна")
        self.store.add_task("Задача", title="Задача", project="home")
        self.store.create_project("beta", "Beta", "")
        self.store.create_project("alpha", "Alpha", "")
        self.assertEqual([p["slug"] for p in self.store.list_projects()], ["alpha", "beta", "home"])

    def test_task_with_unregistered_project_works_as_before(self):
        t = self.store.add_task("Задача", title="Задача", project="ghost")
        self.assertEqual(t["file"], "projects/ghost.md")
        self.assertEqual([x["id"] for x in self.store.list_tasks(project="ghost")], [t["id"]])
        self.assertIn("ghost", self.store.vocab()["projects"])
        self.assertFalse(self.file.exists())  # реєстр не створюється й не доповнюється сам

    def test_task_can_move_between_unregistered_and_registered_projects(self):
        t = self.store.add_task("Задача", title="Задача", project="ghost")
        self.store.create_project("alpha", "Alpha", "")
        moved = self.store.edit_task(t["id"], project="alpha")
        self.assertEqual(moved["file"], "projects/alpha.md")
        self.assertEqual(self.store.edit_task(t["id"], project="ghost")["file"], "projects/ghost.md")

    def test_broken_registry_file_is_an_error_not_an_empty_list(self):
        self.file.write_text("{oops", encoding="utf-8")
        with self.assertRaises(PlannerError):
            self.store.list_projects()
        self.file.write_text("[]", encoding="utf-8")
        with self.assertRaises(PlannerError):
            self.store.list_projects()


class ToolProjectTests(ServerCase):
    def setUp(self):
        super().setUp()
        (self.root / "projects.json").unlink(missing_ok=True)

    def test_no_projects(self):
        self.assertEqual(self.call("list_projects"), "Проєктів немає.")

    def test_created_project_is_listed_with_name_and_repo(self):
        out = self.call("create_project", slug="alpha", name="Alpha Project", repo="acme/alpha")
        self.assertTrue(out.startswith("✅"), out)
        listed = self.call("list_projects")
        for part in ("`alpha`", "Alpha Project", "acme/alpha"):
            self.assertIn(part, listed)
        self.assertIn("alpha", self.tm.store.vocab()["projects"])

    def test_repo_is_optional(self):
        out = self.call("create_project", slug="alpha", name="Alpha Project")
        self.assertTrue(out.startswith("✅"), out)
        self.assertEqual(self.tm.store.list_projects(),
                         [{"slug": "alpha", "name": "Alpha Project", "repo": ""}])

    def test_duplicate_slug_is_a_warning_and_registry_is_unchanged(self):
        self.call("create_project", slug="alpha", name="Alpha Project", repo="acme/alpha")
        out = self.call("create_project", slug="alpha", name="Other", repo="acme/other")
        self.assertTrue(out.startswith("⚠️"), out)
        self.assertEqual(self.tm.store.list_projects(),
                         [{"slug": "alpha", "name": "Alpha Project", "repo": "acme/alpha"}])

    def test_invalid_slug_is_a_warning(self):
        for bad in ("", "-alpha", "Alpha", "my project"):
            with self.subTest(slug=bad):
                out = self.call("create_project", slug=bad, name="Name", repo="")
                self.assertTrue(out.startswith("⚠️"), out)
        self.assertEqual(self.tm.store.list_projects(), [])

    def test_invalid_repo_is_a_warning(self):
        out = self.call("create_project", slug="alpha", name="Name", repo="not-a-repo")
        self.assertTrue(out.startswith("⚠️"), out)
        self.assertEqual(self.tm.store.list_projects(), [])

    def test_project_that_exists_only_as_a_task_file_is_listed_without_name_and_repo(self):
        self.add(project="home")
        self.assertIn("`home`", self.call("list_projects"))
        self.assertEqual(self.tm.store.list_projects(), [{"slug": "home", "name": "", "repo": ""}])

    def test_add_task_with_unregistered_project_works_as_before(self):
        out = self.call("add_task", text="Задача", title="Задача", project="ghost")
        self.assertTrue(out.startswith("✅"), out)
        self.assertIn("@ghost", out)
        self.assertIn("@ghost", self.call("list_tasks", project="ghost"))

    def test_tool_descriptions_do_not_mention_the_brain(self):
        tools = {t.name: t for t in asyncio.run(self.tm.mcp.list_tools())}
        for name in ("create_project", "list_projects"):
            with self.subTest(tool=name):
                self.assertIn(name, tools)
                self.assertNotRegex((tools[name].description or "").casefold(),
                                    r"brain|мозк|мозок|brain_start|rules")


if __name__ == "__main__":
    unittest.main()
