"""Кольори тегів (задача ab7e9e): сховище `tag-colors.json` і веб-API дашборда.

Запуск: uv run python -m unittest discover -s tests -p 'test_tag_colors.py' -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _support import make_planner  # noqa: E402
from planner_store import PlannerError, PlannerStore  # noqa: E402
from test_web_api import WebCase  # noqa: E402


class StoreTagColorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_planner(Path(self._tmp.name))
        self.file = self.root / "tag-colors.json"
        self.store = PlannerStore(self.root)

    def test_no_file_means_no_colors(self):
        self.assertEqual(self.store.tag_colors(), {})

    def test_saved_color_is_stored_in_planner_root_and_survives_restart(self):
        self.store.set_tag_color("дім", "#12ab34")
        self.assertTrue(self.file.exists())
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8")), {"дім": "#12ab34"})
        self.assertEqual(PlannerStore(self.root).tag_colors(), {"дім": "#12ab34"})

    def test_hash_prefix_of_tag_is_ignored(self):
        self.store.set_tag_color("#побут", "#ff0000")
        self.assertEqual(self.store.tag_colors(), {"побут": "#ff0000"})

    def test_setting_again_replaces_the_color_and_keeps_other_tags(self):
        self.store.set_tag_color("a", "#111111")
        self.store.set_tag_color("b", "#222222")
        self.store.set_tag_color("a", "#333333")
        self.assertEqual(self.store.tag_colors(), {"a": "#333333", "b": "#222222"})

    def test_none_removes_the_color(self):
        self.store.set_tag_color("a", "#111111")
        self.store.set_tag_color("b", "#222222")
        self.store.set_tag_color("a", None)
        self.assertEqual(self.store.tag_colors(), {"b": "#222222"})

    def test_non_hex_color_is_refused_and_nothing_is_written(self):
        for bad in ("red", "#12345", "#1234567", "#GGGGGG", "123456", "", "rgb(1,2,3)"):
            with self.subTest(color=bad):
                with self.assertRaises(PlannerError):
                    self.store.set_tag_color("a", bad)
                self.assertFalse(self.file.exists())

    def test_empty_tag_is_refused(self):
        for bad in ("", "  ", "#"):
            with self.subTest(tag=bad):
                with self.assertRaises(PlannerError):
                    self.store.set_tag_color(bad, "#111111")
        self.assertFalse(self.file.exists())

    def test_color_for_a_tag_without_tasks_is_allowed(self):
        self.store.add_task("Задача", title="Задача", domains=["дім"])
        self.store.set_tag_color("порожній", "#abcdef")
        self.assertEqual(self.store.tag_colors(), {"порожній": "#abcdef"})

    def test_colors_file_is_not_a_task_file(self):
        self.store.add_task("Задача", title="Задача", domains=["дім"])
        self.store.set_tag_color("дім", "#12ab34")
        self.assertTrue(self.file.exists())
        before = self.file.read_text(encoding="utf-8")
        tasks = self.store.list_tasks()
        self.assertEqual([t["file"] for t in tasks], ["tasks.md"])
        self.store.add_task("Друга", title="Друга")  # повний _load() над усім planner
        self.assertEqual(self.file.read_text(encoding="utf-8"), before)
        self.assertEqual(self.store.vocab()["domains"], ["дім"])
        self.assertNotIn("external", [r["op"] for r in self.store.history.read(None, 50)])


class ApiTagColorTests(WebCase):
    def setUp(self):
        super().setUp()
        (self.root / "tag-colors.json").unlink(missing_ok=True)

    def set_color(self, tag, color):
        return self.post("/api/tag-color", {"tag": tag, "color": color})

    def test_saved_color_is_returned_by_the_api(self):
        self.add(domains=["дім"])
        self.assertEqual(self.set_color("дім", "#12ab34").status_code, 200)
        self.assertEqual(self.get("/api/tag-colors").json(), {"tag_colors": {"дім": "#12ab34"}})
        d = self.get("/api/tasks").json()
        self.assertEqual(d.get("tag_colors"), {"дім": "#12ab34"})
        self.assertEqual(d["tasks"][0]["domains"], ["дім"])

    def test_hash_prefixed_tag_is_the_same_tag(self):
        self.assertEqual(self.set_color("#дім", "#12ab34").status_code, 200)
        self.assertEqual(self.get("/api/tag-colors").json(), {"tag_colors": {"дім": "#12ab34"}})

    def test_tag_without_color_has_no_color_in_the_payload(self):
        self.add(domains=["побут", "дім"])
        self.set_color("дім", "#12ab34")
        d = self.get("/api/tasks").json()
        self.assertEqual(d.get("tag_colors"), {"дім": "#12ab34"})
        self.assertNotIn("побут", d.get("tag_colors", {}))
        self.assertEqual(d["domains"], ["дім", "побут"])

    def test_no_colors_at_all_is_an_empty_map(self):
        self.assertEqual(self.get("/api/tasks").json().get("tag_colors"), {})
        self.assertEqual(self.get("/api/tag-colors").json(), {"tag_colors": {}})

    def test_non_hex_color_is_400_and_nothing_is_written(self):
        for bad in ("red", "#12345", "#1234567", "#GGGGGG", "123456", "", 123, ["#111111"]):
            with self.subTest(color=bad):
                r = self.set_color("дім", bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("error", r.json())
                self.assertFalse((self.root / "tag-colors.json").exists())

    def test_missing_color_or_tag_is_400(self):
        self.assertEqual(self.post("/api/tag-color", {"tag": "дім"}).status_code, 400)
        self.assertEqual(self.post("/api/tag-color", {"color": "#111111"}).status_code, 400)
        self.assertEqual(self.set_color("", "#111111").status_code, 400)
        self.assertFalse((self.root / "tag-colors.json").exists())

    def test_null_removes_the_color(self):
        self.set_color("дім", "#12ab34")
        self.set_color("побут", "#000000")
        self.assertEqual(self.set_color("дім", None).status_code, 200)
        self.assertEqual(self.get("/api/tag-colors").json(), {"tag_colors": {"побут": "#000000"}})
        self.assertEqual(self.get("/api/tasks").json().get("tag_colors"), {"побут": "#000000"})

    def test_color_can_be_set_for_a_tag_without_tasks(self):
        self.assertEqual(self.get("/api/tasks").json()["domains"], [])
        self.assertEqual(self.set_color("порожній", "#abcdef").status_code, 200)
        self.assertEqual(self.get("/api/tag-colors").json(), {"tag_colors": {"порожній": "#abcdef"}})


class DashboardTagColorTests(WebCase):
    """Сторінка — один HTML із JS, браузера й node у тестах немає: перевіряємо контракт
    розмітки і скрипта (які поля payload читає, куди пише, що малює), а не піксели."""

    def page(self):
        r = self.get("/task-manager")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_settings_panel_lists_tags_with_a_color_picker(self):
        page = self.page()
        self.assertIn('id="tagsBtn"', page)  # кнопка відкриття налаштувань у шапці
        self.assertIn('id="tagsDlg"', page)  # панель зі списком тегів
        self.assertIn("VOCAB.domains", page)  # теги беруться з довідника
        self.assertIn('type = "color"', page)  # вибір кольору для кожного тега

    def test_page_reads_colors_from_the_payload_and_saves_through_the_api(self):
        page = self.page()
        self.assertIn("j.tag_colors", page)
        self.assertIn('api("api/tag-color"', page)
        self.assertIn("color: null", page)  # скидання кольору = null

    def test_tag_chip_uses_its_color_and_uncolored_tag_stays_neutral(self):
        page = self.page()
        self.assertIn('chip("#" + x, "", TAG_COLORS[x])', page)  # колір тега → чіп
        self.assertIn('setProperty("--tag-color"', page)  # колір ставиться лише коли він є
        self.assertIn("if (color)", page)


if __name__ == "__main__":
    unittest.main()
