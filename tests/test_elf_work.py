"""Робочий цикл Elf: claim / release / block (стор і MCP-тули `elf_*`).

Стан Elf — побічний файл `<planner>/elf-state.json` = {id: {"failures": N}}; рядок задачі
не змінюється. Теги — це домени задачі (`#to-agent`, `#claude`, `#codex`, `#blocked`).

Запуск: uv run python -m unittest tests.test_elf_work -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase, make_planner  # noqa: E402
from planner_store import MAX_IN_PROGRESS, PlannerError, PlannerStore  # noqa: E402

TODAY = date.today().isoformat()


class ElfCase(unittest.TestCase):
    """Стор над тимчасовим planner; задачі створюються одразу в To Do (стор це дозволяє)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_planner(Path(self._tmp.name))
        self.s = PlannerStore(self.root)

    def task(self, name, tags=("to-agent",), *, project=None, status="todo", plan=None) -> str:
        tid = self.s.add_task(
            name, title=name, domains=list(tags), project=project, plan=plan
        )["id"]
        if status != "backlog":
            self.s.move_task(tid, "todo")
            if status != "todo":
                self.s.move_task(tid, status)
        return tid

    def ids(self, status: str) -> list[str]:
        return [t["id"] for t in self.s.list_tasks(status=status)]

    def get(self, tid: str) -> dict:
        return self.s.get_task(tid)

    def failures(self, tid: str) -> int | None:
        """Лічильник невдач із побічного файлу; None — запису нема."""
        path = self.root / "elf-state.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get(tid, {}).get("failures")

    def blocker(self) -> dict:
        """Єдина задача в blockers."""
        blockers = self.s.list_tasks(status="blockers")
        self.assertEqual(len(blockers), 1)
        return blockers[0]

    def plan(self, tid: str) -> str | None:
        return self.get(tid).get("plan")

    def seed_failures(self, tid: str, n: int) -> None:
        (self.root / "elf-state.json").write_text(
            json.dumps({tid: {"failures": n}}), encoding="utf-8"
        )

    def claimed(self, agent="claude") -> dict:
        got = self.s.claim(agent)
        self.assertIsNotNone(got, "claim нічого не взяв")
        return got

    def history_of(self, tid: str, tool: str) -> list[dict]:
        return [r for r in self.s.history.read(tid, 50) if r.get("tool") == tool]


class ClaimTests(ElfCase):
    def test_empty_queue_returns_nothing_and_nothing_moves(self):
        self.assertIsNone(self.s.claim("claude"))
        backlog = self.task("У беклозі", status="backlog")
        other = self.task("Без тега", tags=("dev",))
        self.assertIsNone(self.s.claim("claude"))
        self.assertEqual(self.get(backlog)["status"], "backlog")
        self.assertEqual(self.get(other)["status"], "todo")

    def test_claims_top_todo_with_to_agent_and_matching_tag_into_in_progress(self):
        a = self.task("A", ("to-agent", "claude"))
        b = self.task("B", ("to-agent", "claude"))
        got = self.claimed("claude")
        self.assertEqual(got["id"], a)
        self.assertEqual(self.get(a)["status"], "in_progress")
        self.assertEqual(self.get(b)["status"], "todo")
        self.assertEqual(self.ids("todo"), [b])

    def test_top_means_manual_todo_order_not_creation_order(self):
        a = self.task("A", ("to-agent", "claude"), status="backlog")
        b = self.task("B", ("to-agent", "claude"), status="backlog")
        self.s.move_task(b, "todo")
        self.s.move_task(a, "todo")
        self.assertEqual(self.claimed("claude")["id"], b)

    def test_task_without_agent_tag_is_claimed_by_claude(self):
        t = self.task("Без агента", ("to-agent",))
        self.assertIsNone(self.s.claim("codex"))
        self.assertEqual(self.claimed("claude")["id"], t)

    def test_codex_task_is_not_claimed_by_claude(self):
        cx = self.task("Для codex", ("to-agent", "codex"))
        cl = self.task("Для claude", ("to-agent", "claude"))
        self.assertEqual(self.claimed("claude")["id"], cl)
        self.assertEqual(self.get(cx)["status"], "todo")
        self.assertIsNone(self.s.claim("claude"))

    def test_claude_task_is_not_claimed_by_codex(self):
        cl = self.task("Для claude", ("to-agent", "claude"))
        cx = self.task("Для codex", ("to-agent", "codex"))
        self.assertEqual(self.claimed("codex")["id"], cx)
        self.assertEqual(self.get(cl)["status"], "todo")
        self.assertIsNone(self.s.claim("codex"))

    def test_blocked_task_is_skipped(self):
        blocked = self.task("Заблокована", ("to-agent", "claude", "blocked"))
        free = self.task("Вільна", ("to-agent", "claude"))
        self.assertEqual(self.claimed("claude")["id"], free)
        self.assertEqual(self.get(blocked)["status"], "todo")
        self.assertIsNone(self.s.claim("claude"))

    def test_task_without_to_agent_is_skipped(self):
        human = self.task("Для людини", ("claude",))
        robot = self.task("Для агента", ("to-agent", "claude"))
        self.assertEqual(self.claimed("claude")["id"], robot)
        self.assertEqual(self.get(human)["status"], "todo")

    def test_claim_returns_title_text_plan_and_failures(self):
        tid = self.task("Зробити X", ("to-agent", "claude"), plan="# План\n- крок 1\n")
        self.seed_failures(tid, 3)
        got = self.claimed("claude")
        self.assertEqual(
            (got["id"], got["title"], got["text"], got["plan"], got["failures"]),
            (tid, "Зробити X", "Зробити X", "# План\n- крок 1\n", 3),
        )

    def test_fresh_task_has_zero_failures_and_no_plan(self):
        self.task("Нова", ("to-agent", "claude"))
        got = self.claimed("claude")
        self.assertEqual((got["failures"], got["plan"]), (0, None))

    def test_consecutive_claims_never_return_the_same_task(self):
        a = self.task("A", ("to-agent", "claude"))
        b = self.task("B", ("to-agent", "claude"))
        first, second = self.claimed("claude"), self.claimed("claude")
        self.assertEqual((first["id"], second["id"]), (a, b))
        self.assertIsNone(self.s.claim("claude"))

    def test_concurrent_claims_never_return_the_same_task(self):
        made = {self.task(f"T{i}", ("to-agent", "claude")) for i in range(4)}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.s.claim("claude"), range(8)))
        got = [r["id"] for r in results if r is not None]
        self.assertEqual(len(got), 4)
        self.assertEqual(set(got), made)

    def test_claim_respects_the_wip_limit_and_moves_nothing(self):
        for i in range(MAX_IN_PROGRESS):
            self.s.add_task(f"W{i}", title=f"W{i}", status="in_progress")
        t = self.task("Чекає", ("to-agent", "claude"))
        with self.assertRaisesRegex(PlannerError, "WIP"):
            self.s.claim("claude")
        self.assertEqual(self.get(t)["status"], "todo")

    def test_claim_rejects_unknown_agent(self):
        t = self.task("Задача", ("to-agent", "claude"))
        with self.assertRaises(PlannerError):
            self.s.claim("gemini")
        self.assertEqual(self.get(t)["status"], "todo")


class ReleaseTests(ElfCase):
    def claimed_task(self, name="Задача", tags=("to-agent", "claude"), **kw) -> str:
        return self.task(name, tags, status="in_progress", **kw)

    def test_release_puts_task_first_in_todo(self):
        a, b, c = (self.task(n, ("to-agent", "claude")) for n in "ABC")
        self.s.move_task(a, "in_progress")
        self.s.move_task(b, "in_progress")
        self.assertEqual(self.ids("todo"), [c])
        self.s.release(b, "помилка", "claude")
        self.assertEqual(self.ids("todo"), [b, c])
        self.s.release(a, "помилка", "claude")
        self.assertEqual(self.ids("todo"), [a, b, c])
        self.assertEqual(self.get(a)["status"], "todo")

    def test_release_adds_one_failure_each_time(self):
        tid = self.claimed_task()
        self.s.release(tid, "перша", "claude")
        self.assertEqual(self.failures(tid), 1)
        self.s.move_task(tid, "in_progress")
        self.s.release(tid, "друга", "claude")
        self.assertEqual(self.failures(tid), 2)

    def test_release_leaves_task_tags_and_line_unchanged(self):
        tid = self.claimed_task(tags=("to-agent", "claude", "dev"))
        before = self.get(tid)
        self.s.release(tid, "причина", "claude")
        after = self.get(tid)
        self.assertEqual(after["status"], "todo")
        self.assertEqual(after["domains"], before["domains"])
        self.assertEqual((after["text"], after["title"]), (before["text"], before["title"]))

    def test_reason_is_recorded_in_history(self):
        tid = self.claimed_task()
        self.s.release(tid, "нема мережі", "claude")
        recs = self.history_of(tid, "elf_release")
        self.assertEqual(len(recs), 1)
        self.assertIn("нема мережі", json.dumps(recs[0], ensure_ascii=False))

    def test_reason_creates_elf_section_in_plan_when_there_is_no_plan(self):
        tid = self.claimed_task()
        self.s.release(tid, "нема мережі", "claude")
        self.assertEqual(
            self.plan(tid), f"## Elf\n- {TODAY} release (claude): нема мережі\n"
        )

    def test_reason_adds_elf_section_after_existing_plan(self):
        tid = self.claimed_task(plan="# План\n- крок 1\n")
        self.s.release(tid, "тайм-аут", "codex")
        self.assertEqual(
            self.plan(tid),
            f"# План\n- крок 1\n\n## Elf\n- {TODAY} release (codex): тайм-аут\n",
        )

    def test_second_release_appends_to_the_same_elf_section(self):
        tid = self.claimed_task()
        self.s.release(tid, "раз", "claude")
        self.s.move_task(tid, "in_progress")
        self.s.release(tid, "два", "claude")
        self.assertEqual(
            self.plan(tid),
            f"## Elf\n- {TODAY} release (claude): раз\n- {TODAY} release (claude): два\n",
        )

    def test_line_goes_to_the_end_of_elf_section_before_the_next_heading(self):
        old = "## Elf\n- старий запис\n\n## Нотатки\nтекст\n"
        tid = self.claimed_task(plan=old)
        self.s.release(tid, "новий", "claude")
        self.assertEqual(
            self.plan(tid),
            f"## Elf\n- старий запис\n- {TODAY} release (claude): новий\n\n## Нотатки\nтекст\n",
        )

    def test_multiline_reason_is_collapsed_to_one_line(self):
        tid = self.claimed_task()
        self.s.release(tid, "рядок 1\nрядок 2", "claude")
        self.assertEqual(
            self.plan(tid), f"## Elf\n- {TODAY} release (claude): рядок 1 рядок 2\n"
        )

    def test_fifth_failure_blocks_automatically(self):
        tid = self.claimed_task()
        self.seed_failures(tid, 4)
        res = self.s.release(tid, "знову впало", "claude")
        self.assertEqual(self.failures(tid), 5)
        domains = self.get(tid)["domains"]
        self.assertIn("blocked", domains)
        self.assertNotIn("to-agent", domains)
        blockers = self.s.list_tasks(status="blockers")
        self.assertEqual(len(blockers), 1)
        self.assertIn("5 невдач: знову впало", blockers[0]["text"])
        self.assertEqual((res["blocker"] or {}).get("id"), blockers[0]["id"])
        self.assertIn(f"- {TODAY} block (claude): 5 невдач: знову впало", self.plan(tid) or "")

    def test_fourth_failure_does_not_block(self):
        tid = self.claimed_task()
        self.seed_failures(tid, 3)
        res = self.s.release(tid, "ще раз", "claude")
        self.assertEqual(self.failures(tid), 4)
        self.assertIsNone(res.get("blocker", "відсутній"))
        self.assertEqual(self.s.list_tasks(status="blockers"), [])
        self.assertIn("to-agent", self.get(tid)["domains"])
        self.assertNotIn("blocked", self.get(tid)["domains"])

    def test_release_unknown_id_is_an_error(self):
        with self.assertRaises(PlannerError):
            self.s.release("nope00", "причина", "claude")

    def test_release_task_not_in_progress_is_an_error_and_changes_nothing(self):
        tid = self.task("У todo", ("to-agent", "claude"))
        with self.assertRaises(PlannerError):
            self.s.release(tid, "причина", "claude")
        self.assertEqual(self.get(tid)["status"], "todo")
        self.assertFalse((self.root / "elf-state.json").exists())
        self.assertIsNone(self.get(tid).get("plan"))

    def test_release_requires_a_reason_and_a_known_agent(self):
        tid = self.claimed_task()
        with self.assertRaises(PlannerError):
            self.s.release(tid, "  ", "claude")
        with self.assertRaises(PlannerError):
            self.s.release(tid, "причина", "gemini")
        self.assertEqual(self.get(tid)["status"], "in_progress")



if __name__ == "__main__":
    unittest.main()
