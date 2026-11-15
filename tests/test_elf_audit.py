"""elf_audit: задача-нагадування в backlog для аудиту PR Elf за тиждень (MCP-тул і токен elf).

Один тиждень = одна задача (ідемпотентно за заголовком): проєкт `elf`, тег `to-admin`, у плані
список PR. Некоректні вхідні дані — `⚠️`, нічого не пишеться.

Запуск: uv run python -m unittest tests.test_elf_audit -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase  # noqa: E402
from test_elf_token import ELF, TokenCase  # noqa: E402

WEEK = "2026-W46"
TITLE = "Аудит Elf тижня 2026-W46"
PR1 = "https://github.com/vshfrost/task-manager/pull/7"
PR2 = "https://github.com/vshfrost/elf/pull/12"


class AuditToolTests(ServerCase):
    def audit(self, week=WEEK, prs=(PR1, PR2)) -> str:
        return self.call("elf_audit", week=week, prs=list(prs))

    def tasks(self) -> list[dict]:
        return self.tm.store.list_tasks()

    def only(self) -> dict:
        found = self.tasks()
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_creates_backlog_task_with_title_tag_project_and_plan(self):
        out = self.audit()
        self.assertTrue(out.startswith("✅"), out)
        t = self.only()
        self.assertIn(f"`{t['id']}`", out)
        full = self.tm.store.get_task(t["id"])
        self.assertEqual(full["status"], "backlog")
        self.assertEqual(full["title"], TITLE)
        self.assertEqual(full["file"], "projects/elf.md")
        self.assertIn("to-admin", full["domains"])
        self.assertEqual(full["plan"].strip(), f"- {PR1}\n- {PR2}")
        self.assertIn("2 PR", full["text"])
        self.assertNotIn("\n", full["text"])

    def test_second_call_for_same_week_returns_same_id_and_adds_nothing(self):
        self.audit()
        first = self.only()
        out = self.audit(prs=(PR1,))
        self.assertIn("вже є", out)
        self.assertIn(f"`{first['id']}`", out)
        self.assertEqual([t["id"] for t in self.tasks()], [first["id"]])
        self.assertEqual(self.tm.store.get_task(first["id"])["plan"].strip(), f"- {PR1}\n- {PR2}")

    def test_existing_task_in_any_status_counts(self):
        self.audit()
        first = self.only()
        self.tm.store.move_task(first["id"], "done")
        self.assertIn("вже є", self.audit())
        self.assertEqual(len(self.tasks()), 1)

    def test_other_week_makes_a_new_task(self):
        self.audit()
        self.audit(week="2026-W47")
        self.assertEqual(len(self.tasks()), 2)

    def test_bad_week_is_an_error_and_writes_nothing(self):
        for week in ("2026-46", "2026-W5", "26-W46", "2026-W00", "2026-W54", "2026-W46 ", ""):
            with self.subTest(week=week):
                self.assertTrue(self.audit(week=week).startswith("⚠️"))
        self.assertEqual(self.tasks(), [])

    def test_bad_pr_url_is_an_error_and_writes_nothing(self):
        for bad in ("http://github.com/o/r/pull/1", "https://github.com/o/r/issues/1",
                    "https://github.com/o/r/pull/x", "https://evil.example/o/r/pull/1",
                    "https://github.com/o/pull/1", ""):
            with self.subTest(url=bad):
                self.assertTrue(self.audit(prs=(PR1, bad)).startswith("⚠️"))
        self.assertEqual(self.tasks(), [])

    def test_empty_list_is_an_error_and_writes_nothing(self):
        self.assertTrue(self.audit(prs=()).startswith("⚠️"))
        self.assertEqual(self.tasks(), [])

    def test_more_than_50_prs_is_an_error_and_writes_nothing(self):
        many = [f"https://github.com/o/r/pull/{i}" for i in range(1, 52)]
        self.assertTrue(self.audit(prs=many).startswith("⚠️"))
        self.assertEqual(self.tasks(), [])

    def test_exactly_50_prs_is_accepted(self):
        many = [f"https://github.com/o/r/pull/{i}" for i in range(1, 51)]
        self.assertTrue(self.audit(prs=many).startswith("✅"))

    def test_duplicates_are_collapsed_keeping_order(self):
        self.audit(prs=(PR2, PR1, PR2, PR1))
        t = self.only()
        full = self.tm.store.get_task(t["id"])
        self.assertEqual(full["plan"].strip(), f"- {PR2}\n- {PR1}")
        self.assertIn("2 PR", full["text"])


class AuditTokenTests(TokenCase):
    def test_elf_token_may_call_elf_audit(self):
        out = self.text(self.tool(ELF, "elf_audit", {"week": WEEK, "prs": [PR1]}))
        self.assertTrue(out.startswith("✅"), out)
        self.assertEqual([t["title"] for t in self.tm.store.list_tasks()], [TITLE])

    def test_elf_token_still_cannot_call_add_task(self):
        resp = self.tool(ELF, "add_task", {"text": "new", "title": "new"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.tm.store.list_tasks(), [])


if __name__ == "__main__":
    unittest.main()
