"""MCP-тули task-manager за ZOMBIES — через `mcp.call_tool`, як їх викликає клієнт.

Запуск: uv run python -m unittest tests.test_tools -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ServerCase  # noqa: E402


class ZeroTests(ServerCase):
    def test_empty_planner(self):
        self.assertEqual(self.call("list_tasks"), "Порожньо (за цими фільтрами задач немає).")
        self.assertEqual(
            self.call("board"),
            "📋 backlog: 0 · to do: 0 · blockers: 0 · in progress: 0/15 · in review: 0 · testing: 0 · done: 0 · усього: 0",
        )
        self.assertEqual(self.call("history"), "Журнал порожній (за цим фільтром змін не було).")

    def test_empty_text_rejected(self):
        self.assertTrue(self.call("add_task", text="   ", title="T").startswith("⚠️"))

    def test_empty_filter_means_all(self):
        self.add()
        self.assertIn("Задача", self.call("list_tasks", status="", project="", priority="", domain=""))

    def test_empty_edit_changes_nothing(self):
        tid = self.add(priority="low")
        out = self.call("edit_task", id=tid)
        self.assertIn("🔽 low", out)
        self.assertEqual(len(self.tm.store.history.read(tid, 10)), 1, "порожній edit не пишеться в журнал")


class OneTests(ServerCase):
    def test_one_task_lifecycle(self):
        tid = self.add(text="Купити фільтр для води", title="Фільтр", priority="high",
                       deadline="2026-10-01", energy="S", domains="#побут, дім", links="projects/home/overview")
        line = self.call("list_tasks")
        for part in (f"`{tid}`", "[backlog]", "Фільтр", "⏫ high", "📅 2026-10-01", "@global"):
            self.assertIn(part, line)
        full = self.call("get_task", id=tid)
        for part in ("Купити фільтр для води", "⚡S", "#побут #дім", "projects/home/overview"):
            self.assertIn(part, full)

        self.to_todo(tid)
        self.assertIn("[in progress]", self.call("move_task", id=tid, status="in_progress"))
        self.assertIn("🔺 asap", self.call("set_priority", id=tid, priority="asap"))
        self.assertIn("📅 2026-10-02", self.call("set_deadline", id=tid, deadline="2026-10-02"))
        self.assertIn("🗑️ Видалено", self.call("delete_task", id=tid))
        self.assertEqual(self.call("list_tasks"), "Порожньо (за цими фільтрами задач немає).")

    def test_title_equal_to_text_not_duplicated(self):
        self.add(text="Однаково", title="Однаково")
        self.assertNotIn("📌", self.call("list_tasks"))

    def test_project_task_shows_project(self):
        self.add(project="home")
        self.assertIn("@home", self.call("list_tasks", project="home"))


class ManyTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.a = self.add(text="A", title="A", priority="low", domains="побут")
        self.b = self.add(text="B", title="B", priority="asap", deadline="2026-10-05", project="home")
        self.c = self.add(text="C", title="C", priority="asap", deadline="2026-10-01")
        self.d = self.add(text="D", title="D")
        self.call("move_task", id=self.d, status="done")

    def test_filters_combine(self):
        self.assertEqual(self.call("list_tasks", priority="asap").count("\n"), 1)
        self.assertIn("`" + self.b, self.call("list_tasks", project="home"))
        self.assertNotIn("`" + self.b, self.call("list_tasks", project="global"))
        self.assertIn("`" + self.a, self.call("list_tasks", domain="#побут"))
        self.assertIn("`" + self.d, self.call("list_tasks", status="done"))
        self.assertEqual(self.call("list_tasks", status="done", priority="asap"),
                         "Порожньо (за цими фільтрами задач немає).")

    def test_sorted_by_priority_then_deadline(self):
        ids = [ln.split("`")[1] for ln in self.call("list_tasks").splitlines()]
        self.assertEqual(ids[:3], [self.c, self.b, self.a])

    def test_board_counts(self):
        self.to_todo(self.a)
        for status in ("in_progress", "testing"):
            self.call("move_task", id=self.a, status=status)
        self.assertEqual(
            self.call("board").splitlines()[0],
            "📋 backlog: 2 · to do: 0 · blockers: 0 · in progress: 0/15 · in review: 0 · testing: 1 · done: 1 · усього: 4",
        )

    def test_history_all_and_per_task(self):
        self.call("edit_task", id=self.a, text="A2", priority="none")
        all_recs = self.call("history")
        self.assertEqual(all_recs.count("vlad@local"), 6)
        one = self.call("history", id=self.a)
        self.assertEqual(one.count("vlad@local"), 2)
        self.assertIn("text: A → A2", one)
        self.assertIn("priority: low → —", one)

    def test_history_shows_delete_and_external(self):
        self.call("delete_task", id=self.c)
        self.assertIn("🗑️ видалено: C", self.call("history", id=self.c, limit=1))
        f = self.root / "tasks.md"
        f.write_text(f.read_text(encoding="utf-8") + "- [ ] руками ^ab0001\n", encoding="utf-8")
        self.call("list_tasks")
        self.assertIn("⚠️ СТОРОННЯ ПРАВКА повз сервіс: tasks.md", self.call("history", limit=1))


class BoundaryTests(ServerCase):
    def test_title_50_ok_51_rejected(self):
        self.add(title="я" * 50)
        self.assertIn("максимум 50", self.call("add_task", text="t", title="я" * 51))

    def test_wip_fifteen_ok_sixteenth_blocked(self):
        ids = [self.add() for _ in range(18)]
        for i in ids:                                                # у in_progress — лише з todo
            self.to_todo(i)
        for i in ids[:14]:                                           # 1..14: move
            self.assertIn("✅", self.call("move_task", id=i, status="in_progress"))
        self.assertIn("✅", self.call("edit_task", id=ids[14], status="in_progress"))  # 15: edit
        for out in (self.call("move_task", id=ids[15], status="in_progress"),
                    self.call("edit_task", id=ids[15], status="in_progress")):
            self.assertIn("WIP-ліміт", out)
            self.assertIn("15", out)
        # перенос уже in_progress задачі в in_progress — не новий слот
        self.assertIn("✅", self.call("move_task", id=ids[0], status="in_progress"))

    def test_board_shows_limit_15_and_flags_only_above_it(self):
        f = self.root / "tasks.md"
        f.write_text("# Tasks\n" + "".join(f"- [/] t{i} ^aa{i:04d}\n" for i in range(15)), encoding="utf-8")
        line = self.call("board").splitlines()[0]
        self.assertIn("in progress: 15/15 ", line)
        self.assertNotIn("⚠️", line)
        f.write_text(f.read_text(encoding="utf-8") + "- [/] extra ^aa9999\n", encoding="utf-8")
        self.assertIn("in progress: 16/15 ⚠️ перевищено", self.call("board"))

    def test_instructions_and_tool_descriptions_name_limit_15_not_2(self):
        texts = [self.tm.mcp.instructions or ""]
        texts += [t.description for t in asyncio.run(self.tm.mcp.list_tools())
                  if t.name in ("move_task", "edit_task")]
        for text in texts:
            self.assertIn("15", text)
            self.assertNotIn("capped at 2", text)
            self.assertNotIn("≤2", text)

    def test_deadline_calendar_bounds(self):
        tid = self.add()
        self.assertIn("📅 2028-02-29", self.call("set_deadline", id=tid, deadline="2028-02-29"))
        for bad in ("2026-02-29", "2026-13-45", "2026-00-10", "2026-9-1", "31-08-2026"):
            with self.subTest(bad=bad):
                for out in (self.call("set_deadline", id=tid, deadline=bad),
                            self.call("edit_task", id=tid, deadline=bad),
                            self.call("add_task", text="x", title="x", deadline=bad)):
                    self.assertTrue(out.startswith("⚠️"), out)
                    # формат може бути правильним, а дата — неіснуючою: текст має казати про обидва
                    self.assertIn("реальною датою", out)

    def test_history_limit_clamped(self):
        tid = self.add()
        for p in ("asap", "high", "low"):
            self.call("set_priority", id=tid, priority=p)
        # 0 = «не задано» → дефолт 20; відʼємне зводиться до 1.
        self.assertEqual(self.call("history", limit=0).count("vlad@local"), 4)
        self.assertEqual(self.call("history", limit=-5).count("vlad@local"), 1)
        self.assertEqual(self.call("history", limit=2).count("vlad@local"), 2)
        self.assertEqual(self.call("history", limit=10_000).count("vlad@local"), 4)


class InterfaceTests(ServerCase):
    def test_add_task_has_no_status_parameter(self):
        tools = {t.name: t for t in asyncio.run(self.tm.mcp.list_tools())}
        self.assertNotIn("status", tools["add_task"].input_schema["properties"])

    def test_instructions_list_statuses_and_say_new_tasks_go_to_backlog(self):
        self.assertIn("backlog | todo | blockers | in_progress | in_review | testing | done", self.tm.INSTRUCTIONS)
        self.assertRegex(self.tm.INSTRUCTIONS, r"(?i)new tasks (always )?(go|land) (to|in|into) backlog")

    def test_instructions_state_the_transition_rules(self):
        for rule in ("into todo only from testing, in_progress, in_review or blockers",
                     "into blockers only from backlog, todo or testing",
                     "into in_progress only from todo",
                     "into in_review only from in_progress",
                     "into testing only from in_progress, in_review or blockers",
                     "into backlog and done from anywhere"):
            with self.subTest(rule=rule):
                self.assertIn(rule, self.tm.INSTRUCTIONS)
        self.assertIn("capped at 15", self.tm.INSTRUCTIONS)

    def test_status_tool_descriptions_list_seven_statuses_and_rules(self):
        tools = {t.name: " ".join(t.description.split()) for t in asyncio.run(self.tm.mcp.list_tools())}
        for name in ("list_tasks", "move_task", "edit_task"):
            with self.subTest(tool=name):
                self.assertIn("backlog|todo|blockers|in_progress|in_review|testing|done", tools[name])
        for name in ("move_task", "edit_task"):
            with self.subTest(tool=name):
                for rule in ("у todo — лише з testing, in_progress, in_review або blockers",
                             "backlog → todo через MCP заборонено",
                             "у blockers — лише з backlog, todo або testing",
                             "у in_progress — лише з todo", "у in_review — лише з in_progress",
                             "у testing — лише з in_progress, in_review або blockers",
                             "у backlog і done — звідки завгодно"):
                    self.assertIn(rule, tools[name])

    def test_tool_schemas(self):
        tools = {t.name: t for t in asyncio.run(self.tm.mcp.list_tools())}
        required = {
            "add_task": {"text", "title"}, "move_task": {"id", "status"},
            "set_priority": {"id", "priority"}, "set_deadline": {"id", "deadline"},
            "edit_task": {"id"}, "delete_task": {"id"}, "list_tasks": set(),
            "board": set(), "history": set(), "get_task": {"id"},
            "elf_claim": {"agent"}, "elf_release": {"id", "reason", "agent"},
            "elf_block": {"id", "reason", "agent"},
            "create_project": {"slug", "name"}, "list_projects": set(),
        }
        self.assertEqual(set(tools), set(required))
        for name, req in required.items():
            with self.subTest(tool=name):
                self.assertEqual(set(tools[name].input_schema.get("required", [])), req)
                self.assertTrue(tools[name].description)

    def test_instructions_are_only_about_tasks(self):
        # Конектор описує лише свою роботу: жодних згадок мозку чи чужих правил.
        texts = {"INSTRUCTIONS": self.tm.INSTRUCTIONS}
        texts.update({t.name: t.description for t in asyncio.run(self.tm.mcp.list_tools())})
        for name, text in texts.items():
            with self.subTest(source=name):
                self.assertNotRegex(text.casefold(), r"brain|мозк|мозок|brain_start|rules")
        self.assertIn("task manager", self.tm.INSTRUCTIONS)

    def test_clear_markers(self):
        tid = self.add(priority="high", deadline="2026-10-01", energy="M", domains="a b", links="x/y")
        for marker in ("none", "-", "—"):
            with self.subTest(marker=marker):
                self.call("edit_task", id=tid, priority=marker, deadline=marker,
                          energy=marker, domains=marker, links=marker)
                out = self.call("get_task", id=tid)
                for gone in ("⏫", "📅", "⚡", "#a", "x/y"):
                    self.assertNotIn(gone, out)
                self.call("edit_task", id=tid, priority="high", domains="a")

    def test_split_helpers(self):
        self.assertEqual(self.tm._split("#a, b  c,,"), ["a", "b", "c"])
        self.assertEqual(self.tm._split(""), [])
        self.assertIsNone(self.tm._none(None))
        self.assertIsNone(self.tm._none(" NONE "))
        self.assertEqual(self.tm._none(" x "), "x")
        self.assertIs(self.tm._opt(None), self.tm.UNSET)
        self.assertIs(self.tm._opt_list(""), self.tm.UNSET)
        self.assertEqual(self.tm._opt_list("-"), [])
        self.assertEqual(self.tm._val(["a", "b"]), "a, b")
        self.assertEqual(self.tm._val(""), "—")

    def test_project_move_and_back(self):
        tid = self.add()
        self.assertIn("@alpha", self.call("edit_task", id=tid, project="alpha"))
        self.assertIn("@global", self.call("edit_task", id=tid, project="global"))
        self.assertIn("file: projects/alpha.md → tasks.md", self.call("history", id=tid, limit=1))


class ExceptionTests(ServerCase):
    def test_unknown_id_everywhere(self):
        for tool, args in [("move_task", {"status": "done"}), ("set_priority", {"priority": "low"}),
                           ("set_deadline", {"deadline": "2026-10-01"}), ("edit_task", {"text": "x"}),
                           ("delete_task", {})]:
            with self.subTest(tool=tool):
                self.assertTrue(self.call(tool, id="zzzzzz", **args).startswith("⚠️"))

    def test_invalid_values(self):
        tid = self.add()
        cases = [
            ("move_task", {"id": tid, "status": "later"}),
            ("set_priority", {"id": tid, "priority": "urgent"}),
            ("edit_task", {"id": tid, "energy": "XL"}),
            ("edit_task", {"id": tid, "priority": "urgent"}),
            ("edit_task", {"id": tid, "status": "later"}),
            ("edit_task", {"id": tid, "text": "none"}),
            ("edit_task", {"id": tid, "title": "none"}),
            ("add_task", {"text": "x", "title": "x", "priority": "urgent"}),
            ("add_task", {"text": "x", "title": "x", "energy": "XL"}),
            ("add_task", {"text": "x", "title": ""}),
        ]
        for tool, args in cases:
            with self.subTest(tool=tool, args=args):
                self.assertTrue(self.call(tool, **args).startswith("⚠️"))
        self.assertIn("[backlog]", self.call("list_tasks"), "після відмов задача ціла")

    def test_new_task_lands_in_backlog(self):
        out = self.call("add_task", text="x", title="Нова")
        self.assertIn("[backlog]", out)
        self.assertEqual(self.call("list_tasks", status="backlog"), out.replace("✅ Додано: ", ""))

    def test_add_task_ignores_status_argument(self):
        # параметра status нема в схемі; зайвий аргумент клієнта не може обійти backlog
        out = asyncio.run(self.tm.mcp.call_tool(
            "add_task", {"text": "x", "title": "x", "status": "in_progress"})).content[0].text
        self.assertIn("[backlog]", out)

    def test_move_and_edit_accept_backlog(self):
        tid = self.add()
        self.to_todo(tid)
        for status in ("in_progress", "testing"):
            self.call("move_task", id=tid, status=status)
        self.assertIn("[backlog]", self.call("move_task", id=tid, status="backlog"))
        self.call("move_task", id=tid, status="done")
        self.assertIn("[backlog]", self.call("edit_task", id=tid, status="backlog"))

    def test_missing_required_argument_is_protocol_error(self):
        from mcp.server.mcpserver.exceptions import ToolError

        with self.assertRaisesRegex(ToolError, "title"):
            asyncio.run(self.tm.mcp.call_tool("add_task", {"text": "без заголовка"}))


class SimpleScenarioTests(ServerCase):
    def test_day_flow(self):
        tid = self.add(text="Помити посуд", title="Посуд", project="home", domains="побут")
        self.to_todo(tid)
        for status in ("in_progress", "in_review", "testing", "done"):
            self.call("move_task", id=tid, status=status)
        self.assertIn("done: 1", self.call("board"))
        tools = [ln.split(" · ")[2].split(" ")[0] for ln in self.call("history", id=tid).splitlines()
                 if " · " in ln]
        self.assertEqual(tools, ["move_task"] * 5 + ["add_task"])  # перший перехід — з дашборда/стору


if __name__ == "__main__":
    unittest.main()


class CompactOutputTests(ServerCase):
    """P1 (14a49b): короткий вивід — менше токенів у контексті клієнта."""

    HIDDEN = "(+{n} done приховано — `status=done`)"

    def test_list_line_has_title_and_meta_but_not_text(self):
        tid = self.add(text="Довгий повний опис задачі", title="Коротко", priority="high",
                       deadline="2026-10-01", energy="S", domains="побут", project="home")
        self.assertEqual(self.call("list_tasks"), f"`{tid}` [backlog] Коротко · ⏫ high 📅 2026-10-01 @home")

    def test_list_line_without_meta(self):
        tid = self.add(text="Опис", title="Назва")
        self.assertEqual(self.call("list_tasks"), f"`{tid}` [backlog] Назва · @global")

    def test_done_hidden_by_default_with_count(self):
        a, b = self.add(title="Жива"), self.add(title="Готова")
        self.call("move_task", id=b, status="done")
        self.assertEqual(self.call("list_tasks"),
                         f"`{a}` [backlog] Жива · @global\n" + self.HIDDEN.format(n=1))

    def test_explicit_status_done_lists_done_without_footer(self):
        self.add(title="Жива")
        b = self.add(title="Готова")
        self.call("move_task", id=b, status="done")
        self.assertEqual(self.call("list_tasks", status="done"), f"`{b}` [done] Готова · @global")

    def test_only_done_tasks_say_empty_but_mention_hidden(self):
        b = self.add()
        self.call("move_task", id=b, status="done")
        self.assertEqual(self.call("list_tasks"),
                         "Порожньо (за цими фільтрами задач немає). " + self.HIDDEN.format(n=1))

    def test_hidden_count_respects_other_filters(self):
        self.add(title="Глобальна")
        b = self.add(title="Домашня готова", project="home")
        self.call("move_task", id=b, status="done")
        self.assertNotIn("приховано", self.call("list_tasks", project="global"))
        self.assertEqual(self.call("list_tasks", project="home"),
                         "Порожньо (за цими фільтрами задач немає). " + self.HIDDEN.format(n=1))

    def test_get_task_shows_everything(self):
        tid = self.add(text="Купити фільтр для води", title="Фільтр", priority="high",
                       deadline="2026-10-01", energy="M", domains="побут", links="projects/home/overview",
                       project="home")
        out = self.call("get_task", id=tid)
        for part in (f"`{tid}`", "Фільтр", "Купити фільтр для води", "⏫ high", "📅 2026-10-01",
                     "⚡M", "#побут", "@home", "projects/home/overview"):
            self.assertIn(part, out)

    def test_get_task_unknown_id(self):
        self.assertTrue(self.call("get_task", id="zzzzzz").startswith("⚠️"))

    def test_query_matches_title_and_text_case_insensitive(self):
        a = self.add(text="купити воду", title="Фільтр")
        self.add(text="інше", title="Інше")
        self.assertEqual(self.call("list_tasks", query="ФІЛЬТР").count("`" + a), 1)
        self.assertEqual(self.call("list_tasks", query="воду").count("\n"), 0)
        self.assertEqual(self.call("list_tasks", query="воду").split("`")[1], a)
        self.assertEqual(self.call("list_tasks", query="немає такого"),
                         "Порожньо (за цими фільтрами задач немає).")

    def test_limit_cuts_list_and_reports_rest(self):
        for i in range(5):
            self.add(title=f"T{i}")
        lines = self.call("list_tasks", limit=2).splitlines()
        self.assertEqual(len([ln for ln in lines if ln.startswith("`")]), 2)
        self.assertEqual(lines[-1], "(ще 3 — звузь фільтр або збільш `limit`)")

    def test_default_limit_is_50(self):
        for i in range(51):
            self.add(title=f"T{i}")
        lines = self.call("list_tasks").splitlines()
        self.assertEqual(len([ln for ln in lines if ln.startswith("`")]), 50)
        self.assertEqual(lines[-1], "(ще 1 — звузь фільтр або збільш `limit`)")

    def test_limit_zero_means_default_negative_means_one(self):
        for i in range(3):
            self.add(title=f"T{i}")
        self.assertEqual(self.call("list_tasks", limit=0).count("\n"), 2)
        self.assertEqual(len([ln for ln in self.call("list_tasks", limit=-5).splitlines()
                              if ln.startswith("`")]), 1)

    def test_write_responses_are_one_line_without_text(self):
        text = "Дуже довгий опис, якого не повинно бути у відповіді запису"
        out = self.call("add_task", text=text, title="Назва")
        tid = out.split("`")[1]
        self.to_todo(tid)
        outs = [out,
                self.call("move_task", id=tid, status="in_progress"),
                self.call("edit_task", id=tid, priority="high"),
                self.call("set_priority", id=tid, priority="low"),
                self.call("set_deadline", id=tid, deadline="2026-10-01")]
        for o in outs:
            with self.subTest(out=o):
                self.assertNotIn("\n", o)
                self.assertNotIn(text, o)
                self.assertIn("Назва", o)
        self.assertEqual(outs[1], f"✅ Переміщено: `{tid}` [in progress] Назва · @global")

    def test_board_lists_titles_of_active_tasks_only(self):
        a, b, c = self.add(title="Робота"), self.add(title="Перевірка"), self.add(title="Колись")
        self.to_todo(a)
        self.to_todo(b)
        self.call("move_task", id=a, status="in_progress")
        for status in ("in_progress", "testing"):
            self.call("move_task", id=b, status=status)
        lines = self.call("board").splitlines()
        self.assertEqual(
            lines[0],
            "📋 backlog: 1 · to do: 0 · blockers: 0 · in progress: 1/15 · in review: 0 · testing: 1 · done: 0 · усього: 3",
        )
        self.assertEqual(lines[1:], [f"▶ in progress: `{a}` Робота", f"🧪 testing: `{b}` Перевірка"])

    def test_board_counts_todo_and_lists_in_review_rows(self):
        a, b, c, d = (self.add(title=t) for t in ("Далі", "Робота", "Рев'ю", "Перевірка"))
        for tid in (a, b, c, d):
            self.to_todo(tid)
        self.call("move_task", id=b, status="in_progress")
        for status in ("in_progress", "in_review"):
            self.call("move_task", id=c, status=status)
        for status in ("in_progress", "in_review", "testing"):
            self.call("move_task", id=d, status=status)
        lines = self.call("board").splitlines()
        self.assertEqual(
            lines[0],
            "📋 backlog: 0 · to do: 1 · blockers: 0 · in progress: 1/15 · in review: 1 · testing: 1 · done: 0 · усього: 4",
        )
        self.assertEqual(lines[1:], [f"▶ in progress: `{b}` Робота", f"🔎 in review: `{c}` Рев'ю",
                                     f"🧪 testing: `{d}` Перевірка"])

    def test_board_head_flags_wip_over_15_between_to_do_and_in_review(self):
        f = self.root / "tasks.md"
        f.write_text("# Tasks\n" + "".join(f"- [/] t{i} ^aa{i:04d}\n" for i in range(16)), encoding="utf-8")
        self.assertIn("to do: 0 · blockers: 0 · in progress: 16/15 ⚠️ перевищено · in review: 0", self.call("board"))
    def test_tools_return_plain_text_without_result_wrapper(self):
        for t in asyncio.run(self.tm.mcp.list_tools()):
            with self.subTest(tool=t.name):
                self.assertIsNone(t.output_schema)
        res = asyncio.run(self.tm.mcp.call_tool("board", {}))
        self.assertIsNone(res.structured_content)
