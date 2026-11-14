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


class CiTests(ReviewCase):
    def test_green_ci_makes_the_task_appear_in_review_next(self):
        tid = self.in_review()
        self.assertEqual(self.review_ids("claude"), [])
        self.s.ci(tid, "green")
        self.assertEqual(self.state(tid).get("ci"), "green")
        self.assertEqual(self.review_ids("claude"), [tid])
        self.assertEqual(self.status(tid), "in_review")

    def test_red_ci_returns_task_to_the_top_of_todo_and_counts_a_failure(self):
        first = self.task("Інша", ("to-agent", "claude"))
        tid = self.in_review()
        self.s.ci(tid, "red")
        self.assertEqual(self.status(tid), "todo")
        self.assertEqual(self.ids("todo"), [tid, first])
        self.assertEqual(self.failures(tid), 1)
        self.assertIn(f"- {TODAY} ci (claude): CI red", self.elf_section(tid))
        self.assertEqual(self.review_ids("claude"), [])

    def test_red_ci_after_green_clears_the_green(self):
        tid = self.in_review(green=True)
        self.s.ci(tid, "red")
        self.assertEqual(self.claimed("claude")["id"], tid)
        self.s.submit(tid, "claude", PR2)
        self.assertEqual(self.review_ids("claude"), [])

    def test_ci_on_task_not_in_review_is_an_error_and_changes_nothing(self):
        working = self.in_progress()
        queued = self.task("У todo", ("to-agent", "dev", "claude"))
        for tid in (working, queued, "nope00"):
            with self.subTest(task=tid):
                for result in ("green", "red"):
                    with self.assertRaises(PlannerError):
                        self.s.ci(tid, result)
        self.assertEqual(self.status(working), "in_progress")
        self.assertEqual(self.status(queued), "todo")
        self.assertIsNone(self.failures(working))
        self.assertNotIn("ci", self.state(working))

    def test_invalid_result_is_an_error_and_changes_nothing(self):
        tid = self.in_review()
        for result in ("yellow", "", "GREEN", "passed"):
            with self.subTest(result=result):
                with self.assertRaises(PlannerError):
                    self.s.ci(tid, result)
        self.assertEqual(self.status(tid), "in_review")
        self.assertNotIn("ci", self.state(tid))
        self.assertIsNone(self.failures(tid))

    def test_fifth_failure_by_red_ci_blocks_the_task(self):
        tid = self.in_review()
        self.seed_failures(tid, 4)
        self.s.ci(tid, "red")
        self.assertEqual(self.failures(tid), 5)
        t = self.get(tid)
        self.assertEqual(t["status"], "todo")
        self.assertIn("blocked", t["domains"])
        self.assertNotIn("to-agent", t["domains"])
        blocker = self.blocker()
        self.assertIn("to-admin", blocker["domains"])
        self.assertIn(tid, blocker["text"])
        self.assertIn("5 невдач: CI red", blocker["text"])
        self.assertIn("CI red", self.elf_section(tid))

    def test_fourth_failure_by_red_ci_does_not_block(self):
        tid = self.in_review()
        self.seed_failures(tid, 3)
        self.s.ci(tid, "red")
        self.assertEqual(self.failures(tid), 4)
        self.assertEqual(self.s.list_tasks(status="blockers"), [])
        self.assertNotIn("blocked", self.get(tid)["domains"])


class ReviewNextTests(ReviewCase):
    def test_returns_only_green_in_review_tasks_of_that_agent_with_pr_url(self):
        mine = self.in_review("Моя", ("dev", "claude"), PR, green=True)
        self.in_review("Без CI", ("dev", "claude"), PR2)
        other = self.in_review("Чужа", ("dev", "codex"), PR2, green=True)
        self.assertEqual(
            self.s.review_next("claude"), [{"id": mine, "title": "Моя", "pr": PR}]
        )
        self.assertEqual(
            self.s.review_next("codex"), [{"id": other, "title": "Чужа", "pr": PR2}]
        )

    def test_other_statuses_are_not_listed(self):
        merged = self.in_review("Змерджена", green=True)
        self.s.merged(merged)
        returned = self.in_review("Повернена", green=True)
        self.s.send_back(returned, "правки", "claude")
        self.assertEqual((self.status(merged), self.status(returned)), ("testing", "todo"))
        self.assertEqual(self.review_ids("claude"), [])

    def test_empty_when_nothing_is_ready(self):
        self.assertEqual(self.s.review_next("claude"), [])
        self.assertEqual(self.s.review_next("codex"), [])

    def test_follows_in_review_order(self):
        a = self.in_review("A", green=True)
        b = self.in_review("B", green=True)
        c = self.in_review("C", green=True)
        self.assertEqual(self.ids("in_review"), [a, b, c])
        self.assertEqual(self.review_ids("claude"), [a, b, c])

    def test_untagged_task_counts_as_claude(self):
        tid = self.in_review("Без агента", ("to-agent", "dev"), green=True)
        self.assertEqual(self.review_ids("claude"), [tid])
        self.assertEqual(self.review_ids("codex"), [])

    def test_read_only_nothing_changes(self):
        tid = self.in_review(green=True)
        state, task = self.state(tid), self.get(tid)
        self.assertEqual(self.review_ids("claude"), [tid])
        self.assertEqual(self.state(tid), state)
        self.assertEqual(self.get(tid), task)

    def test_unknown_agent_is_an_error(self):
        with self.assertRaises(PlannerError):
            self.s.review_next("gemini")


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

    def test_ci_green_then_review_next_lists_id_title_and_pr(self):
        tid = self.reviewing("Зробити X")
        self.assertEqual(self.call("elf_review_next", agent="claude"), "nothing to review")
        out = self.call("elf_ci", id=tid, result="green")
        self.assertTrue(out.startswith("✅"), out)
        listed = self.call("elf_review_next", agent="claude")
        for part in (f"`{tid}`", "Зробити X", PR):
            self.assertIn(part, listed)
        self.assertEqual(self.call("elf_review_next", agent="codex"), "nothing to review")

    def test_ci_red_returns_task_to_todo_with_reason_in_plan(self):
        tid = self.reviewing()
        out = self.call("elf_ci", id=tid, result="red")
        self.assertTrue(out.startswith("✅"), out)
        self.assertIn("Невдач: 1", out)
        got = self.call("get_task", id=tid)
        self.assertIn("[to do]", got)
        self.assertIn("CI red", got)

    def test_ci_errors_for_wrong_status_and_invalid_result(self):
        working = self.working()
        reviewing = self.reviewing("Інша")
        self.assertTrue(self.call("elf_ci", id=working, result="green").startswith("⚠️"))
        self.assertTrue(self.call("elf_ci", id=reviewing, result="yellow").startswith("⚠️"))
        self.assertTrue(self.call("elf_ci", id="nope00", result="green").startswith("⚠️"))
        self.assertIn("[in review]", self.call("get_task", id=reviewing))


if __name__ == "__main__":
    unittest.main()
