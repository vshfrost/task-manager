"""Локальні тести моделі+стору (без сервера).

Запуск: uv run python -m unittest discover -s tests -p 'test_store.py' -v
(або напряму: uv run python tests/test_store.py)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner_model import parse_file, render_file, parse_task_line, TITLE_MAX  # noqa: E402
from planner_store import PlannerStore, PlannerError, UNSET  # noqa: E402
from planner_history import as_client, current_client  # noqa: E402

def check(cond, msg):
    """Перевірка з людським описом: у unittest провал = AssertionError із цим описом."""
    if not cond:
        raise AssertionError(msg)


def test_parse_render_roundtrip():
    print("\n[parse/render]")
    line = "- [/] Зробити щось  #розробка #проєкт ⏫ 📅 2026-08-31 ⚡M [[foo]] ^ab12cd"
    t = parse_task_line(line)
    check(t is not None, "рядок розпарсено")
    check(t.status == "in_progress", "статус in_progress")
    check(t.text == "Зробити щось", f"текст без токенів → '{t.text}'")
    check(t.domains == ["розробка", "проєкт"], f"домени → {t.domains}")
    check(t.priority == "high", "пріоритет high")
    check(t.deadline == "2026-08-31", "дедлайн")
    check(t.energy == "M", "енергія M")
    check(t.links == ["foo"], "лінк")
    check(t.id == "ab12cd", "id збережено")
    check(t.render() == line, f"round-trip ідентичний:\n    {t.render()}")


def test_backlog_line_roundtrip():
    print("\n[[ ] = backlog]")
    line = "- [ ] Колись зробити  #побут ⚡S 📌[Колись] ^ab12cd"
    t = parse_task_line(line)
    check(t is not None and t.status == "backlog", f"'[ ]' парситься як backlog → {t and t.status}")
    check(t.render() == line, f"round-trip '[ ]' без змін:\n    {t.render()}")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
        added = PlannerStore(root).add_task("Нова", title="Нова")
        check(added["status"] == "backlog", f"add_task за замовч. → backlog, не {added['status']}")
        check("- [ ] Нова" in (root / "tasks.md").read_text(encoding="utf-8"), "у файлі лишається '[ ]'")


def test_fence_skipped():
    print("\n[код-блок не парситься]")
    md = "```\n- [ ] приклад у фенсі\n```\n- [ ] справжня  ^zz9999"
    blocks = parse_file(md)
    tasks = [t for k, t in blocks if k == "task"]
    check(len(tasks) == 1 and tasks[0].text == "справжня", f"лише 1 задача поза фенсом → {[t.text for t in tasks]}")


def test_store_ops():
    print("\n[стор: add/move/WIP/set/delete]")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "projects").mkdir()
        (root / "tasks.md").write_text("# Tasks\n\n## 🔥 Активні\n\n## ✅ Done\n", encoding="utf-8")
        s = PlannerStore(root)

        a = s.add_task("Alpha", title="Alpha", priority="high")
        b = s.add_task("Beta", title="Beta", project="torrserver", energy="L")
        check(a["id"] != b["id"], "унікальні id")
        check((root / "projects" / "torrserver.md").exists(), "створено файл проєкту")

        # вставка перед Done у tasks.md
        txt = (root / "tasks.md").read_text(encoding="utf-8")
        check("Alpha" in txt and txt.index("Alpha") < txt.index("## ✅ Done"), "Alpha перед секцією Done")

        # WIP-ліміт: за замовчуванням 15
        for x in (a, b):  # в in_progress — лише з todo
            s.move_task(x["id"], "todo")
            s.move_task(x["id"], "in_progress")
        for i in range(13):
            s.add_task(f"wip-{i}", title=f"wip-{i}", status="in_progress")
        check(s.count_in_progress() == 15, "15 в in_progress дозволено")
        c = s.add_task("Gamma", title="Gamma")
        s.move_task(c["id"], "todo")
        try:
            s.move_task(c["id"], "in_progress")
            check(False, "WIP-ліміт мав спрацювати")
        except PlannerError as e:
            check("15" in str(e), f"WIP-ліміт блокує 16-ту in_progress, помилка згадує 15 → {e}")

        # add одразу в in_progress теж блокується
        try:
            s.add_task("Delta", title="Delta", status="in_progress")
            check(False, "WIP при add мав спрацювати")
        except PlannerError as e:
            check("15" in str(e), f"WIP-ліміт блокує add у in_progress, помилка згадує 15 → {e}")

        # set + persist round-trip
        s.set_priority(b["id"], "asap")
        s.set_deadline(b["id"], "2026-09-01")
        reloaded = {t["id"]: t for t in s.list_tasks()}
        check(reloaded[b["id"]]["priority"] == "asap", "пріоритет збережено після перезавантаження")
        check(reloaded[b["id"]]["deadline"] == "2026-09-01", "дедлайн збережено")

        # invalid
        try:
            s.set_deadline(b["id"], "31-08-2026")
            check(False, "невалідну дату мали відхилити")
        except PlannerError:
            check(True, "невалідна дата відхилена")

        # delete
        s.delete_task(a["id"])
        ids = {t["id"] for t in s.list_tasks()}
        check(a["id"] not in ids, "задачу видалено")


def test_id_baking():
    print("\n[запікання id для мігрованих задач без ^id]")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "tasks.md").write_text("# T\n- [ ] без id\n", encoding="utf-8")
        s = PlannerStore(root)
        lst = s.list_tasks()
        check(len(lst) == 1 and lst[0]["id"], f"id присвоєно → {lst[0]['id']}")
        after = (root / "tasks.md").read_text(encoding="utf-8")
        check("^" in after, "^id записано у файл (запечено)")


def test_edit_task():
    print("\n[edit_task: часткове редагування, очищення, перенос між файлами]")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "tasks.md").write_text("# T\n", encoding="utf-8")
        s = PlannerStore(root)

        t = s.add_task("вихідна", title="вихідна", priority="low", deadline="2026-09-10",
                       domains=["розробка"], links=["a/b"])
        tid = t["id"]

        # 1) не передані поля лишаються недоторканими
        r = s.edit_task(tid, text="нова назва")
        check(r["text"] == "нова назва", "текст змінено")
        check(r["priority"] == "low", "пріоритет НЕ зачеплено (UNSET)")
        check(r["deadline"] == "2026-09-10", "дедлайн НЕ зачеплено (UNSET)")
        check(r["domains"] == ["розробка"], "домени НЕ зачеплено (UNSET)")

        # 2) кілька полів за один виклик
        r = s.edit_task(tid, priority="asap", energy="L", links=["projects/x/overview"])
        check(r["priority"] == "asap" and r["energy"] == "L", "кілька полів за раз")
        check(r["links"] == ["projects/x/overview"], "лінк перезаписано")

        # 3) None = очистити (на відміну від UNSET)
        r = s.edit_task(tid, deadline=None, priority=None)
        check(r["deadline"] is None and r["priority"] is None, "поля очищено через None")

        # 4) перенос між файлами
        r = s.edit_task(tid, project="alpha")
        check(r["file"] == "projects/alpha.md", "задачу перенесено у projects/alpha.md")
        check(tid not in (root / "tasks.md").read_text(encoding="utf-8"),
              "зі старого файлу прибрано")
        check(tid in (root / "projects" / "alpha.md").read_text(encoding="utf-8"),
              "у новому файлі зʼявилась")
        check(len([x for x in s.list_tasks() if x["id"] == tid]) == 1,
              "після переносу задача рівно одна (не роздвоїлась)")

        # 5) назад у глобальний
        r = s.edit_task(tid, project="global")
        check(r["file"] == "tasks.md", "повернення у глобальний список")

        # 6) валідація — до запису, стан не псується
        try:
            s.edit_task(tid, text="ще одна", status="кудись")
            check(False, "невалідний статус мали відхилити")
        except PlannerError:
            check(True, "невалідний статус відхилено")
        check(s.list_tasks()[0]["text"] != "ще одна",
              "після відмови текст НЕ змінився (валідація до запису)")

        # 7) WIP-ліміт діє і в edit_task
        for i in range(15):
            s.add_task(f"wip-{i}", title=f"wip-{i}", status="in_progress")
        s.edit_task(tid, status="todo")
        try:
            s.edit_task(tid, status="in_progress")
            check(False, "WIP-ліміт мав заблокувати 16-ту")
        except PlannerError as e:
            check("15" in str(e), f"WIP-ліміт блокує edit_task у in_progress, помилка згадує 15 → {e}")

        # 8) неіснуючий id
        try:
            s.edit_task("zzzzzz", text="x")
            check(False, "неіснуючий id мали відхилити")
        except PlannerError:
            check(True, "неіснуючий id відхилено")


def test_history():
    print("\n[журнал змін: add/edit/delete + детект ручних правок]")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "tasks.md").write_text("# T\n", encoding="utf-8")
        s = PlannerStore(root)

        t = s.add_task("журнальна", title="журнальна", priority="low")
        tid = t["id"]
        h = s.history.read(limit=10)
        check(h and h[0]["op"] == "add" and h[0]["id"] == tid, "add потрапив у журнал")
        check(h[0]["user"] == "vlad", f"user проставлено → {h[0]['user']}")
        check(h[0]["client"] in ("local", "mac", "phone"), f"client проставлено → {h[0]['client']}")
        check("text" in h[0]["changes"] and "priority" in h[0]["changes"],
              "у add записано непорожні поля")

        # edit: у журналі ЛИШЕ те, що змінилось
        s.edit_task(tid, priority="asap", links=["projects/x/overview"])
        h = s.history.read(task_id=tid, limit=1)
        ch = h[0]["changes"]
        check(h[0]["tool"] == "edit_task", "tool у записі = edit_task")
        check(set(ch) == {"priority", "links"}, f"у діфі лише змінені поля: {sorted(ch)}")
        check(ch["priority"] == ["low", "asap"], "діф зберігає було→стало")

        # порожній edit не смітить у журнал
        n_before = len(s.history.read(limit=100))
        s.edit_task(tid, priority="asap")
        check(len(s.history.read(limit=100)) == n_before, "edit без реальних змін НЕ логується")

        # move окремим тулом
        s.move_task(tid, "done")
        check(s.history.read(task_id=tid, limit=1)[0]["tool"] == "move_task", "move_task у журналі")

        # delete зі знімком
        s.delete_task(tid)
        h = s.history.read(task_id=tid, limit=1)
        check(h[0]["op"] == "delete" and h[0]["snapshot"]["text"] == "журнальна",
              "delete зберіг знімок задачі")


def test_history_external_change():
    print("\n[журнал: ручна правка повз сервіс]")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "tasks.md").write_text("# T\n- [ ] мігрована без id\n", encoding="utf-8")
        s = PlannerStore(root)

        # перший load: «запікає» ^id ВЛАСНИМ записом — це НЕ стороння правка
        s.list_tasks()
        s.list_tasks()
        ext = [r for r in s.history.read(limit=50) if r["op"] == "external"]
        check(not ext, "запікання id не вважається сторонньою правкою")

        # а тепер правимо файл повз сервіс (як Obsidian)
        f = root / "tasks.md"
        f.write_text(f.read_text(encoding="utf-8") + "- [ ] дописано руками\n", encoding="utf-8")
        s.list_tasks()
        ext = [r for r in s.history.read(limit=50) if r["op"] == "external"]
        check(len(ext) == 1 and ext[0]["file"] == "tasks.md", "ручну правку спіймано (external)")

        # повторний виклик без змін — другого external бути не має
        s.list_tasks()
        ext = [r for r in s.history.read(limit=50) if r["op"] == "external"]
        check(len(ext) == 1, "external не дублюється, поки файл не змінюють знову")


def test_web_ui_guard():
    print("\n[дашборд: запобіжник від публікації за Funnel]")
    import web_ui
    try:
        web_ui.register(object(), None, is_oauth=True)
        check(False, "register() на OAuth-інстансі мав впасти")
    except AssertionError as e:
        check("публічним" in str(e), "register() на OAuth-інстансі відхилено з поясненням")


def test_client_source():
    print("\n[журнал: джерело зміни (дашборд vs MCP) на одному інстансі]")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "tasks.md").write_text("# T\n", encoding="utf-8")
        s = PlannerStore(root)

        base = current_client()
        t = s.add_task("через MCP", title="через MCP")
        with as_client("web"):
            check(current_client() == "web", "усередині блоку джерело = web")
            s.edit_task(t["id"], priority="high")
        check(current_client() == base, "після блоку джерело повернулось")

        recs = s.history.read(limit=2)
        check(recs[0]["client"] == "web", f"зміна з дашборда підписана web → {recs[0]['client']}")
        check(recs[1]["client"] == base, f"зміна через MCP підписана міткою інстансу → {recs[1]['client']}")


def test_title():
    print("\n[заголовок задачі]")
    line = "- [ ] Довгий текст задачі  #побут ⏫ 📅 2026-09-08 ⚡S [[foo]] 📌[Короткий заголовок] ^ab12cd"
    t = parse_task_line(line)
    check(t.title == "Короткий заголовок", f"заголовок вийнято → '{t.title}'")
    check(t.text == "Довгий текст задачі", f"текст без токена заголовка → '{t.text}'")
    check(t.links == ["foo"], "wiki-лінк не сплутано з заголовком")
    check(t.render() == line, f"round-trip із заголовком:\n    {t.render()}")

    old = parse_task_line("- [ ] Задача без заголовка  ^ff0011")
    check(old.title == "", "старий рядок без токена → title порожній")
    check(old.render() == "- [ ] Задача без заголовка ^ff0011", "рядок без заголовка не змінюється")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "projects").mkdir()
        (root / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
        s = PlannerStore(root)

        a = s.add_task("Догрумити аутфіт на поїздку Дублін→Стокгольм→Прага (капсула + комплекти)",
                       title="Аутфіт на поїздку")
        check(a["title"] == "Аутфіт на поїздку", "заголовок збережено окремо від тексту")
        check(a["text"].endswith("комплекти)"), "текст задачі лишився повним")

        try:
            s.add_task("Задача без заголовка")
            check(False, "add_task без заголовка мав впасти")
        except PlannerError as e:
            check("обовʼязков" in str(e), f"заголовок обовʼязковий при створенні → {e}")

        b = s.add_task("Помити посуд", title="Посуд")
        check(b["title"] == "Посуд", "явний заголовок збережено")
        b2 = s.edit_task(b["id"], title="Помити посуд увечері")
        check(b2["title"] == "Помити посуд увечері", "заголовок редагується")
        try:
            s.edit_task(b["id"], title=None)
            check(False, "очищення заголовка мало впасти")
        except PlannerError:
            check(True, "заголовок не можна очистити (єдине поле без 'none')")
        still = [t for t in s.list_tasks() if t["id"] == b["id"]][0]
        check(still["title"] == "Помити посуд увечері", "після невдалої спроби заголовок цілий")

        try:
            s.add_task("Довга", title="я" * (TITLE_MAX + 1))
            check(False, "ліміт заголовка мав спрацювати")
        except PlannerError:
            check(True, f"заголовок довший за {TITLE_MAX} відхилено")

        c = s.add_task("Дужки й службові символи", title="Купити [молоко] ^ і #хліб")
        check("]" not in c["title"] and "^" not in c["title"] and "#" not in c["title"],
              f"службові символи вичищено → '{c['title']}'")
        reread = [t for t in s.list_tasks() if t["id"] == c["id"]][0]
        check(reread["title"] == c["title"], "заголовок переживає перечитування файлу")


def test_templates_and_vocab():
    print("\n[шаблони + довідники]")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "projects" / "home").mkdir(parents=True)
        (root / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
        (root / "projects" / "home" / "template.md").write_text(
            "# Шаблони — home\n\n"
            "- [ ] Винести сміття  #побут ⚡S 📌[Винести сміття]\n"
            "- [ ] Помити посуд  #побут ⚡S 📌[Помити посуд]\n",
            encoding="utf-8",
        )
        s = PlannerStore(root)
        s.add_task("Задача проєкту", title="Задача проєкту", project="home", domains=["побут"], links=["home/overview"])

        tpls = s.templates()
        check(len(tpls) == 2, f"знайдено 2 шаблони → {len(tpls)}")
        check(tpls[0]["project"] == "home", "проєкт шаблону = імʼя папки")
        check(tpls[0]["domains"] == ["побут"] and tpls[0]["energy"] == "S", "поля шаблону розпарсено")
        check(not (root / "projects" / "home" / "template.md").read_text(encoding="utf-8").count("^"),
              "у template.md не запікаються ^id (не керований файл)")
        check(all(t["file"] != "projects/home/template.md" for t in s.list_tasks()),
              "шаблони не потрапляють у список задач")

        v = s.vocab()
        check("home" in v["projects"] and "global" in v["projects"], f"проєкти → {v['projects']}")
        check(v["domains"] == ["побут"], f"домени → {v['domains']}")
        check(v["links"] == ["home/overview"], f"звʼязки → {v['links']}")


class StoreTests(unittest.TestCase):
    """Обгортка: кожна test_*-функція модуля — окремий тест unittest."""


for _name, _fn in list(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        setattr(StoreTests, _name, lambda self, _fn=_fn: _fn())
del _name, _fn  # інакше unittest збере StoreTests удруге через глобальну _fn


if __name__ == "__main__":
    unittest.main()
