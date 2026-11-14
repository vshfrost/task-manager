"""Рев'ю-цикл Elf: submit / ci / review_next / merged / return (стор і MCP-тули `elf_*`).

Стан Elf — той самий побічний файл `<planner>/elf-state.json` = {id: {"failures": N, "pr": url,
"ci": "green"}}; рядок задачі не змінюється. Теги — домени задачі (`#dev`, `#claude`, `#codex`).

Запуск: uv run python -m unittest tests.test_elf_review -v
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase  # noqa: E402
from planner_store import PlannerError  # noqa: E402
from test_elf_work import ElfCase  # noqa: E402

TODAY = date.today().isoformat()
PR = "https://github.com/example/repo/pull/7"
PR2 = "https://github.com/example/repo/pull/8"


class ReviewCase(ElfCase):
    def in_progress(self, name="Задача", tags=("to-agent", "dev", "claude")) -> str:
        return self.task(name, tags, status="in_progress")

    def in_review(self, name="Задача", tags=("to-agent", "dev", "claude"), pr=PR, green=False) -> str:
        tid = self.in_progress(name, tags)
        self.s.submit(tid, "claude", pr)
        if green:
            self.s.ci(tid, "green")
        return tid

    def state(self, tid: str) -> dict:
        path = self.root / "elf-state.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8")).get(tid, {})

    def elf_section(self, tid: str) -> str:
        plan = self.plan(tid) or ""
        self.assertIn("## Elf", plan)
        return plan.split("## Elf", 1)[1]

    def status(self, tid: str) -> str:
        return self.get(tid)["status"]

    def review_ids(self, agent="claude") -> list[str]:
        return [r["id"] for r in self.s.review_next(agent)]


class SubmitTests(ReviewCase):
    def test_dev_task_with_pr_goes_to_in_review_and_pr_is_stored(self):
        tid = self.in_progress()
        self.s.submit(tid, "claude", PR)
        self.assertEqual(self.status(tid), "in_review")
        self.assertEqual(self.state(tid).get("pr"), PR)
        self.assertIn(f"- {TODAY} submit (claude): in_review, PR {PR}", self.elf_section(tid))

    def test_dev_task_without_pr_is_an_error_and_nothing_moves(self):
        tid = self.in_progress()
        for pr in ("", "   "):
            with self.subTest(pr=pr):
                with self.assertRaises(PlannerError):
                    self.s.submit(tid, "claude", pr)
        self.assertEqual(self.status(tid), "in_progress")
        self.assertNotIn("pr", self.state(tid))
        self.assertIsNone(self.plan(tid))

    def test_task_without_dev_tag_goes_to_testing(self):
        tid = self.in_progress("Без dev", ("to-agent", "claude"))
        self.s.submit(tid, "claude")
        self.assertEqual(self.status(tid), "testing")
        self.assertIn(f"- {TODAY} submit (claude): testing", self.elf_section(tid))

    def test_pr_of_a_task_without_dev_tag_is_optional_but_stored(self):
        tid = self.in_progress("Без dev", ("to-agent", "claude"))
        self.s.submit(tid, "claude", PR)
        self.assertEqual(self.status(tid), "testing")
        self.assertEqual(self.state(tid).get("pr"), PR)

    def test_task_not_in_progress_is_an_error_and_changes_nothing(self):
        todo = self.task("У todo", ("to-agent", "dev", "claude"))
        review = self.in_review("У рев'ю")
        for tid in (todo, review, "nope00"):
            with self.subTest(task=tid):
                with self.assertRaises(PlannerError):
                    self.s.submit(tid, "claude", PR2)
        self.assertEqual(self.status(todo), "todo")
        self.assertEqual(self.status(review), "in_review")
        self.assertEqual(self.state(review).get("pr"), PR)
        self.assertEqual(self.state(todo), {})

    def test_unknown_agent_is_an_error_and_nothing_moves(self):
        tid = self.in_progress()
        with self.assertRaises(PlannerError):
            self.s.submit(tid, "gemini", PR)
        self.assertEqual(self.status(tid), "in_progress")

    def test_resubmit_after_return_clears_old_ci(self):
        tid = self.in_review(green=True)
        self.s.send_back(tid, "потрібні правки", "claude")
        self.assertEqual(self.claimed("claude")["id"], tid)
        self.s.submit(tid, "claude", PR2)
        self.assertEqual(self.state(tid).get("pr"), PR2)
        self.assertEqual(self.review_ids("claude"), [])


class ReviewToolsTests(ServerCase):
    """Ті самі поведінки крізь MCP-інтерфейс (тексти відповідей, помилки як `⚠️`)."""

    def setUp(self):
        super().setUp()
        for name in ("elf-state.json", "todo-order.json"):
            (self.root / name).unlink(missing_ok=True)

    def working(self, name="Задача", domains="to-agent dev claude") -> str:
        tid = self.add(text=name, title=name, domains=domains)
        self.to_todo(tid)
        self.assertIn(f"`{tid}`", self.call("elf_claim", agent="claude"))
        return tid

    def reviewing(self, name="Задача", domains="to-agent dev claude", pr=PR, green=False) -> str:
        tid = self.working(name, domains)
        self.call("elf_submit", id=tid, agent="claude", pr=pr)
        if green:
            self.call("elf_ci", id=tid, result="green")
        return tid

    def test_submit_dev_task_reports_in_review_and_stores_the_pr(self):
        tid = self.working()
        out = self.call("elf_submit", id=tid, agent="claude", pr=PR)
        self.assertTrue(out.startswith("✅"), out)
        self.assertIn(PR, out)
        got = self.call("get_task", id=tid)
        self.assertIn("[in review]", got)
        self.assertIn(PR, got)

    def test_submit_dev_task_without_pr_is_an_error(self):
        tid = self.working()
        self.assertTrue(self.call("elf_submit", id=tid, agent="claude").startswith("⚠️"))
        self.assertTrue(self.call("elf_submit", id=tid, agent="claude", pr="").startswith("⚠️"))
        self.assertIn("[in progress]", self.call("get_task", id=tid))

    def test_submit_task_without_dev_tag_goes_to_testing(self):
        tid = self.working("Без dev", "to-agent claude")
        out = self.call("elf_submit", id=tid, agent="claude")
        self.assertTrue(out.startswith("✅"), out)
        self.assertIn("[testing]", self.call("get_task", id=tid))


if __name__ == "__main__":
    unittest.main()
