"""comment_task: коментар у розділ `## Elf` плану (стор і MCP-тул).

Блок коментаря: `- РРРР-ММ-ДД <перший рядок>`, наступні рядки — з відступом у 2 пробіли
(щоб рядок `## x` у тексті не став заголовком розділу плану).

Запуск: uv run python -m unittest tests.test_comment -v
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase, make_planner  # noqa: E402
from planner_store import PlannerError, PlannerStore  # noqa: E402

TODAY = date.today().isoformat()


class CommentStoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_planner(Path(self._tmp.name))
        self.s = PlannerStore(self.root)

    def task(self, plan=None) -> str:
        return self.s.add_task("Fixture", title="Fixture", plan=plan)["id"]

    def plan(self, tid: str) -> str | None:
        return self.s.get_task(tid).get("plan")


class CommentPlanTests(CommentStoreCase):
    def test_creates_elf_section_when_no_plan(self):
        tid = self.task()
        self.s.comment_task(tid, "looks fine")
        self.assertEqual(self.plan(tid), f"## Elf\n- {TODAY} looks fine\n")

    def test_creates_elf_section_after_existing_plan(self):
        tid = self.task(plan="# Plan\n- step\n")
        self.s.comment_task(tid, "looks fine")
        self.assertEqual(self.plan(tid), f"# Plan\n- step\n\n## Elf\n- {TODAY} looks fine\n")

    def test_appends_to_existing_elf_section(self):
        tid = self.task(plan="## Elf\n- 2026-01-01 older\n")
        self.s.comment_task(tid, "newer")
        self.assertEqual(self.plan(tid), f"## Elf\n- 2026-01-01 older\n- {TODAY} newer\n")

    def test_multiline_comment_indents_following_lines(self):
        tid = self.task()
        self.s.comment_task(tid, "first\nsecond\nthird")
        self.assertEqual(self.plan(tid), f"## Elf\n- {TODAY} first\n  second\n  third\n")

    def test_text_is_stripped(self):
        tid = self.task()
        self.s.comment_task(tid, "\n  first\nsecond  \n\n")
        self.assertEqual(self.plan(tid), f"## Elf\n- {TODAY} first\n  second\n")

    def test_heading_line_in_text_creates_no_section(self):
        tid = self.task()
        self.s.comment_task(tid, "first\n## x\nthird")
        plan = self.plan(tid)
        self.assertEqual(plan, f"## Elf\n- {TODAY} first\n  ## x\n  third\n")
        self.assertEqual(re.findall(r"^#{1,6} .*$", plan, re.M), ["## Elf"])

    def test_heading_line_in_text_keeps_next_section_outside_elf(self):
        tid = self.task(plan="## Elf\n- 2026-01-01 older\n\n## Notes\nkeep me\n")
        self.s.comment_task(tid, "a\n## x\nb")
        self.assertEqual(
            self.plan(tid),
            f"## Elf\n- 2026-01-01 older\n- {TODAY} a\n  ## x\n  b\n\n## Notes\nkeep me\n",
        )

    def test_rest_of_plan_intact(self):
        old = "# Plan\n- [ ] step  \n\n## Elf\n- 2026-01-01 older\n\n## Notes\ntext\n"
        tid = self.task(plan=old)
        self.s.comment_task(tid, "new")
        self.assertEqual(
            self.plan(tid),
            f"# Plan\n- [ ] step  \n\n## Elf\n- 2026-01-01 older\n- {TODAY} new\n\n## Notes\ntext\n",
        )

    def test_returns_task_with_updated_plan(self):
        tid = self.task()
        got = self.s.comment_task(tid, "note")
        self.assertEqual((got["id"], got.get("plan")), (tid, f"## Elf\n- {TODAY} note\n"))

    def test_task_fields_unchanged(self):
        tid = self.task()
        before = {k: v for k, v in self.s.get_task(tid).items() if k != "plan"}
        self.s.comment_task(tid, "note")
        after = {k: v for k, v in self.s.get_task(tid).items() if k != "plan"}
        self.assertEqual(after, before)


class CommentValidationTests(CommentStoreCase):
    def test_empty_or_whitespace_text_is_error(self):
        tid = self.task()
        for text in ("", "   ", "\n\t \n"):
            with self.subTest(text=text):
                with self.assertRaises(PlannerError):
                    self.s.comment_task(tid, text)
        self.assertIsNone(self.plan(tid))

    def test_4000_chars_ok_4001_error(self):
        tid = self.task()
        self.s.comment_task(tid, "я" * 4000)
        with self.assertRaises(PlannerError):
            self.s.comment_task(tid, "я" * 4001)
        self.assertEqual(self.plan(tid), f"## Elf\n- {TODAY} {'я' * 4000}\n")

    def test_unknown_id_is_error(self):
        with self.assertRaises(PlannerError):
            self.s.comment_task("zzzzzz", "note")
        self.assertFalse((self.root / "plans").exists() and any((self.root / "plans").iterdir()))


class CommentHistoryTests(CommentStoreCase):
    def test_comment_is_recorded_in_history(self):
        tid = self.task()
        self.s.comment_task(tid, "first\nsecond")
        recs = [r for r in self.s.history.read(tid, 50) if r.get("tool") == "comment_task"]
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["op"], recs[0]["id"]), ("edit", tid))
        self.assertEqual(recs[0]["changes"]["elf"], [None, "first\nsecond"])

    def test_rejected_comment_is_not_recorded(self):
        tid = self.task()
        with self.assertRaises(PlannerError):
            self.s.comment_task(tid, " ")
        self.assertEqual([r for r in self.s.history.read(tid, 50) if r.get("tool") == "comment_task"], [])


class CommentToolTests(ServerCase):
    def test_tool_adds_comment_and_confirms(self):
        tid = self.add()
        out = self.call("comment_task", id=tid, text="first\nsecond")
        self.assertTrue(out.startswith("✅"), out)
        self.assertIn(f"`{tid}`", out)
        self.assertEqual(self.tm.store.get_task(tid).get("plan"), f"## Elf\n- {TODAY} first\n  second\n")

    def test_tool_errors_are_warnings(self):
        tid = self.add()
        for args in ({"id": tid, "text": "  "}, {"id": tid, "text": "x" * 4001},
                     {"id": "zzzzzz", "text": "note"}):
            with self.subTest(args=list(args.values())[1][:5]):
                self.assertTrue(self.call("comment_task", **args).startswith("⚠️"))
        self.assertNotIn("plan", self.tm.store.get_task(tid))

    def test_tool_recorded_in_history_output(self):
        tid = self.add()
        self.call("comment_task", id=tid, text="note")
        self.assertIn("comment_task", self.call("history", id=tid))


if __name__ == "__main__":
    unittest.main()
