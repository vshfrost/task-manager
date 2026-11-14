"""Реєстр проєктів (задача 29cf3f): веб-API і форма «Новий проєкт» на дашборді.

Запуск: uv run python -m unittest tests.test_projects_api -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_web_api import WebCase  # noqa: E402


class ApiProjectTests(WebCase):
    def setUp(self):
        super().setUp()
        (self.root / "projects.json").unlink(missing_ok=True)
        for f in (self.root / "projects").glob("*.md"):
            f.unlink()

    def create(self, slug, name="Alpha Project", repo="acme/alpha"):
        return self.post("/api/project", {"slug": slug, "name": name, "repo": repo})

    def listed(self):
        r = self.get("/api/projects")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_no_projects(self):
        self.assertEqual(self.listed(), {"projects": []})

    def test_created_project_is_200_and_listed(self):
        r = self.create("alpha")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["project"], {"slug": "alpha", "name": "Alpha Project", "repo": "acme/alpha"})
        self.assertEqual(
            self.listed(),
            {"projects": [{"slug": "alpha", "name": "Alpha Project", "repo": "acme/alpha"}]},
        )

    def test_created_project_is_in_the_board_payload_vocab(self):
        self.create("alpha")
        self.assertIn("alpha", self.get("/api/tasks").json()["projects"])

    def test_repo_is_optional(self):
        r = self.post("/api/project", {"slug": "alpha", "name": "Alpha Project"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["project"]["repo"], "")
        self.assertEqual(self.create("beta", repo="").status_code, 200)

    def test_task_file_only_project_is_listed_without_name_and_repo(self):
        self.add(project="home")
        self.assertEqual(
            self.listed(),
            {"projects": [{"slug": "home", "name": "", "repo": ""}]},
        )

    def test_duplicate_slug_is_400_with_message_and_registry_is_unchanged(self):
        self.assertEqual(self.create("alpha").status_code, 200)
        r = self.create("alpha", name="Other", repo="acme/other")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertTrue(r.json()["error"])
        self.assertEqual(
            self.listed(),
            {"projects": [{"slug": "alpha", "name": "Alpha Project", "repo": "acme/alpha"}]},
        )

    def test_invalid_slug_is_400_with_message_and_nothing_is_written(self):
        for bad in ("", "-alpha", "Alpha", "my project", None, 123):
            with self.subTest(slug=bad):
                r = self.create(bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertTrue(r.json()["error"])
        self.assertFalse((self.root / "projects.json").exists())

    def test_invalid_repo_is_400_with_message_and_nothing_is_written(self):
        for bad in ("acme", "acme/", "https://github.com/acme/alpha", 123):
            with self.subTest(repo=bad):
                r = self.create("alpha", repo=bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertTrue(r.json()["error"])
        self.assertFalse((self.root / "projects.json").exists())

    def test_missing_slug_or_name_is_400(self):
        self.assertEqual(self.post("/api/project", {"name": "Alpha"}).status_code, 400)
        self.assertEqual(self.post("/api/project", {"slug": "alpha"}).status_code, 400)
        self.assertFalse((self.root / "projects.json").exists())

    def test_non_object_body_is_400(self):
        self.assertEqual(self.post("/api/project", ["alpha"]).status_code, 400)


class DashboardProjectFormTests(WebCase):
    """Сторінка — один HTML із JS, браузера й node у тестах немає: перевіряємо контракт
    розмітки і скрипта (які поля шле форма, куди пише), а не піксели."""

    def page(self):
        r = self.get("/task-manager")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_header_has_a_button_and_a_dialog_with_slug_name_and_repo_fields(self):
        page = self.page()
        self.assertIn('id="projBtn"', page)
        self.assertIn('id="projDlg"', page)
        self.assertIn("Новий проєкт", page)
        for field in ('id="p_slug"', 'id="p_name"', 'id="p_repo"'):
            self.assertIn(field, page)

    def test_form_posts_to_the_api_and_reloads_the_board(self):
        page = self.page()
        self.assertIn('api("api/project"', page)
        self.assertIn("slug: $(\"#p_slug\").value", page)


if __name__ == "__main__":
    unittest.main()
