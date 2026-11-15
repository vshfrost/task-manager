"""
planner_store — читання/запис доменних файлів задач у `~/AI/planner/`.
=====================================================================

Сховище над markdown-файлами planner (джерело правди). Дає операції для MCP-тулів:
list / add / move (з правилами переходів і WIP-гардом) / set_priority / set_deadline / edit (будь-які поля) / delete.

- Керовані файли: `tasks.md` (глобальний) + `projects/*.md`. `README.md` — виключено.
- План задачі (багаторядковий markdown) — окремий файл `plans/<id>.md`, поза рядком задачі.
  Стор його як файл задач НЕ читає: `- [ ]` усередині плану задачами не стають.
- Запис — атомарний (`os.replace`) під `fcntl.flock` (безпечно навіть якщо Obsidian редагує).
- Ручний порядок колонки To Do — `<planner>/todo-order.json` (список id): задачі To Do лежать
  у різних файлах проєктів, тож порядок рядків файлу його не передає. Це не файл задач.
- Стан робочого циклу Elf — `<planner>/elf-state.json` = {id: {"failures": N, "pr": url,
  "ci": "green"}}: лічильник невдач, PR і результат CI не потрапляють в рядок задачі.
  Це не файл задач.
- WIP-ліміт: не більше MAX_IN_PROGRESS задач у статусі `in_progress` (по всьому planner).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from datetime import date
from pathlib import Path

from planner_history import HistoryLog, diff, initial, sha256_text
from planner_model import (
    PRIORITIES,
    MCP_FORBIDDEN_TRANSITIONS,
    STATUSES,
    TODO_TOP_SOURCES,
    TRANSITION_SOURCES,
    ENERGIES,
    TITLE_MAX,
    Block,
    Task,
    parse_file,
    parse_task_line,
    render_file,
    new_id,
)

MAX_IN_PROGRESS = int(os.environ.get("TASKS_WIP_LIMIT", "15"))
PLANNER_ROOT = Path(
    os.environ.get("TASKS_PLANNER_ROOT", str(Path.home() / "AI" / "planner"))
).expanduser()

_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")
# Кольори тегів: `<planner>/tag-colors.json` = {"<тег без #>": "#RRGGBB"}. Не файл задач.
TAG_COLORS_FILE = "tag-colors.json"
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Реєстр проєктів: `<planner>/projects.json` = {"<слаг>": {"name": str, "repo": "owner/name" | ""}}.
# Не файл задач. Проєкт без запису тут (є лише `projects/<слаг>.md`) працює як і раніше.
PROJECTS_FILE = "projects.json"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _is_date(value: str) -> bool:
    """РРРР-ММ-ДД і реальна календарна дата: регулярка сама пропускала `2026-13-45`,
    а така задача ламала календар дашборда."""
    if not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class PlannerError(ValueError):
    """Помилка бізнес-правил (WIP-ліміт, невалідне значення, не знайдено)."""


class _Unset:
    """Сентинел: поле НЕ передавали (на відміну від None = «очистити поле»)."""

    def __repr__(self) -> str:  # для читабельних трейсів
        return "UNSET"


UNSET = _Unset()


PLANS_DIRNAME = "plans"
TODO_ORDER_FILENAME = "todo-order.json"

# Робочий цикл Elf: агенти, поріг невдач, службові теги (теги задачі — це її `domains`).
ELF_STATE_FILENAME = "elf-state.json"
ELF_AGENTS = ("claude", "codex")
ELF_MAX_FAILURES = 3
ELF_TAG_QUEUE, ELF_TAG_BLOCKED, ELF_TAG_ADMIN = "to-agent", "blocked", "to-admin"
ELF_TAG_DEV = "dev"
ELF_CI_RESULTS = ("green", "red")
_ELF_HEADING_RE = re.compile(r"^## Elf[ \t]*$", re.M)
_NEXT_HEADING_RE = re.compile(r"^#{1,2} ", re.M)
_AUDIT_WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")
_AUDIT_PR_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
AUDIT_MAX_PRS = 50


def _check_position(position: int | None) -> None:
    """Позиція в To Do — ціле ≥ 1 (1 — угорі); bool — не число, хоч і підклас int."""
    if position is None:
        return
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise PlannerError("Позиція має бути цілим числом ≥ 1 (1 — угорі колонки To Do)")


def _require_todo_for_position(position: int | None, status: str) -> None:
    if position is not None and status != "todo":
        raise PlannerError("Позиція можлива лише в колонці To Do")


def _norm_plan(plan: str | None) -> str | None:
    """Порожній план, лише пробіли або `none` → None (план прибрати); інакше текст як є —
    без обрізання й нормалізації, щоб файл лишався байт-у-байт таким, як написали."""
    if plan is None or not plan.strip() or plan.strip().lower() == "none":
        return None
    return plan


def _require_one_line(text: str) -> None:
    """`text` — один рядок задачі: перенос зламав би рядок `- [ ] …` у markdown.
    Багаторядкове — у `plan`."""
    if "\n" in text or "\r" in text:
        raise PlannerError(
            "Текст задачі — один рядок (без переносів). Багаторядкове клади в поле plan"
        )


def _with_plan(task: dict, plan: str | None) -> dict:
    """to_dict() + `plan`, лише якщо він є: без плану ключа нема взагалі."""
    return {**task, "plan": plan} if plan is not None else task


def _slugify(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace("/", "-")


class PlannerStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else PLANNER_ROOT
        self._lock = threading.Lock()
        self._audit_lock = threading.Lock()  # перевірка «вже є» + створення — одним кроком
        self.history = HistoryLog(self.root)

    # --- Внутрішнє: перелік керованих файлів --------------------------------
    def _managed_files(self) -> list[str]:
        files: list[str] = []
        tasks_md = self.root / "tasks.md"
        if tasks_md.exists():
            files.append("tasks.md")
        proj_dir = self.root / "projects"
        if proj_dir.is_dir():
            for p in sorted(proj_dir.glob("*.md")):
                files.append(f"projects/{p.name}")
        return files

    # --- План задачі: plans/<id>.md ------------------------------------------
    def _plan_path(self, task_id: str) -> Path:
        return self.root / PLANS_DIRNAME / f"{task_id}.md"

    def _read_plan(self, task_id: str) -> str | None:
        try:
            # newline="" — без перетворення \r\n, щоб прочитане дорівнювало записаному
            with open(self._plan_path(task_id), encoding="utf-8", newline="") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def _write_plan(self, task_id: str, plan: str | None) -> None:
        """None — прибрати файл плану; текст — записати атомарно, байт-у-байт."""
        path = self._plan_path(task_id)
        if plan is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        with open(tmp, "wb") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(plan.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, path)

    # --- Ручний порядок To Do: todo-order.json --------------------------------
    def _read_order(self) -> list[str]:
        """Збережений порядок. Немає файлу чи він зіпсований — порожній: задачі не губляться,
        бо _todo_order дописує все, чого в ньому нема."""
        try:
            data = json.loads((self.root / TODO_ORDER_FILENAME).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []
        return [i for i in data if isinstance(i, str)] if isinstance(data, list) else []

    def _todo_order(self, tasks: list[Task]) -> list[str]:
        """id задач To Do в ручному порядку: спершу за файлом (без застарілих id), потім ті,
        яких у файлі нема (напр. поставлені в Obsidian) — у порядку читання, у кінці."""
        todo = [t.id for t in tasks if t.status == "todo"]
        in_todo = set(todo)
        known = dict.fromkeys(i for i in self._read_order() if i in in_todo)
        return [*known, *(i for i in todo if i not in known)]

    def _sync_todo_order(
        self, tasks: list[Task], moved: Task | None = None,
        position: int | None = None, entered: bool = False,
    ) -> None:
        """Після зміни статусів: прибирає з порядку тих, хто пішов із To Do, ставить `moved`
        на `position` (без позиції — в кінець, якщо щойно зайшла в To Do) і пише файл,
        лише якщо він змінився."""
        order = self._todo_order(tasks)
        if moved is not None and moved.status == "todo" and (entered or position is not None):
            order.remove(moved.id)
            order.insert(len(order) if position is None else position - 1, moved.id)
        if order == self._read_order():
            return
        path = self.root / TODO_ORDER_FILENAME
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(order, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, path)

    def _read_blocks(self, relpath: str) -> list[Block]:
        text = (self.root / relpath).read_text(encoding="utf-8")
        return parse_file(text, relpath)

    def _write_blocks(self, relpath: str, blocks: list[Block]) -> None:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        text = render_file(blocks)
        if not text.endswith("\n"):
            text += "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, path)
        # Запамʼятовуємо хеш ВЛАСНОГО запису — інакше наступний _load() вирішить,
        # що це стороння правка, і засмітить журнал хибними external.
        self.history.update_hashes({relpath: sha256_text(text)})

    # --- Завантаження всього стору ------------------------------------------
    def _load(self) -> dict[str, list[Block]]:
        """Читає всі керовані файли, ловить сторонні правки й «запікає» нові ^id.

        Два проходи навмисно: спершу звіряємо хеші всіх файлів із тим, що сервіс
        записував сам (розбіжність = правка повз нас, напр. з Obsidian → пишемо
        `external` у журнал), і тільки потім робимо власні записи. Інакше «запікання»
        id перезаписало б хеш раніше, ніж ми встигли б порівняти.
        """
        store: dict[str, list[Block]] = {}
        state = self.history.load_state()
        fresh: dict[str, str] = {}
        to_bake: list[str] = []

        # Прохід 1 — читання + детект сторонніх правок
        for relpath in self._managed_files():
            raw = (self.root / relpath).read_text(encoding="utf-8")
            h = sha256_text(raw)
            known = state.get(relpath)
            if known is None:
                fresh[relpath] = h          # перша зустріч із файлом — просто запамʼятали
            elif known != h:
                self.history.record_external(relpath, known, h)
                fresh[relpath] = h
            blocks = parse_file(raw, relpath)
            store[relpath] = blocks
            if any(k == "task" and not t.had_id for k, t in blocks):  # type: ignore[union-attr]
                to_bake.append(relpath)
        self.history.update_hashes(fresh)

        # Прохід 2 — власні записи (запікання ^id); _write_blocks оновить хеші сам
        for relpath in to_bake:
            for k, t in store[relpath]:
                if k == "task":
                    t.had_id = True  # type: ignore[union-attr]
            self._write_blocks(relpath, store[relpath])
        return store

    def _all_tasks(self, store: dict[str, list[Block]]) -> list[Task]:
        out: list[Task] = []
        for blocks in store.values():
            out += [t for k, t in blocks if k == "task"]  # type: ignore[misc]
        return out

    def _find(self, store: dict[str, list[Block]], task_id: str) -> Task:
        for t in self._all_tasks(store):
            if t.id == task_id:
                return t
        raise PlannerError(f"Задачу з id '{task_id}' не знайдено")

    # --- Публічні операції ---------------------------------------------------
    def list_tasks(
        self,
        *,
        status: str | None = None,
        project: str | None = None,
        priority: str | None = None,
        domain: str | None = None,
        query: str | None = None,
    ) -> list[dict]:
        if status and status not in STATUSES:
            raise PlannerError(f"Невідомий статус '{status}'. Дозволено: {', '.join(STATUSES)}")
        with self._lock:
            store = self._load()
            tasks = self._all_tasks(store)
            rank = {tid: i for i, tid in enumerate(self._todo_order(tasks))}
        if status:
            tasks = [t for t in tasks if t.status == status]
        if priority:
            tasks = [t for t in tasks if t.priority == priority]
        if domain:
            tasks = [t for t in tasks if domain.lstrip("#") in t.domains]
        if query:
            q = query.casefold()
            tasks = [t for t in tasks if q in t.text.casefold() or q in (t.title or "").casefold()]
        if project is not None:
            rel = "tasks.md" if project in ("", "global") else f"projects/{_slugify(project)}.md"
            tasks = [t for t in tasks if t.file == rel]
        # сортування: asap→high→low→none, потім за дедлайном
        prio_rank = {"asap": 0, "high": 1, "low": 2, None: 3}
        tasks.sort(key=lambda t: (prio_rank.get(t.priority, 3), t.deadline or "9999-99-99"))
        # To Do — у ручному порядку, а не за пріоритетом: задачі To Do лишаються на своїх місцях
        # списку, але між собою йдуть за порядком колонки
        slots = [i for i, t in enumerate(tasks) if t.status == "todo"]
        for i, t in zip(slots, sorted((tasks[i] for i in slots), key=lambda t: rank[t.id])):
            tasks[i] = t
        return [t.to_dict() for t in tasks]

    def get_task(self, task_id: str) -> dict:
        with self._lock:
            task = self._find(self._load(), task_id)
            return _with_plan(task.to_dict(), self._read_plan(task.id))

    def count_in_progress(self) -> int:
        with self._lock:
            store = self._load()
            return sum(1 for t in self._all_tasks(store) if t.status == "in_progress")

    def add_task(
        self,
        text: str,
        *,
        project: str | None = None,
        priority: str | None = None,
        deadline: str | None = None,
        energy: str | None = None,
        domains: list[str] | None = None,
        links: list[str] | None = None,
        status: str = "backlog",
        title: str | None = None,
        plan: str | None = None,
    ) -> dict:
        text = (text or "").strip()
        if not text:
            raise PlannerError("Порожній текст задачі")
        _require_one_line(text)
        plan = _norm_plan(plan)
        _validate(status=status, priority=priority, energy=energy, deadline=deadline)
        title = _require_title(title)
        with self._lock:
            store = self._load()
            if status == "in_progress" and self._count_ip(store) >= MAX_IN_PROGRESS:
                raise PlannerError(
                    f"WIP-ліміт: вже {MAX_IN_PROGRESS} задач в in progress. "
                    "Заверши/поверни одну, перш ніж брати нову."
                )
            task = Task(
                id=new_id(),
                status=status,
                text=text,
                domains=[d.lstrip("#") for d in (domains or [])],
                priority=priority,
                deadline=deadline,
                energy=energy,
                links=links or [],
                title=title,
            )
            rel = self._target_file(project)
            task.file = rel
            blocks = store.get(rel)
            if blocks is None:
                title = "Tasks — глобальний список" if rel == "tasks.md" else f"Задачі — {project}"
                blocks = [("raw", f"# {title}"), ("raw", "")]
            blocks = _insert_task(blocks, task)
            self._write_blocks(rel, blocks)
            store[rel] = blocks
            if status == "todo":
                self._sync_todo_order(self._all_tasks(store), task, entered=True)
            if plan is not None:
                self._write_plan(task.id, plan)
            created = _with_plan(task.to_dict(), plan)
            self.history.record(
                "add", tool="add_task", task_id=task.id, file=rel, changes=initial(created)
            )
            return created

    def move_task(
        self, task_id: str, status: str, position: int | None = None, *, via_mcp: bool = False
    ) -> dict:
        if status not in STATUSES:
            raise PlannerError(f"Невідомий статус '{status}'. Дозволено: {', '.join(STATUSES)}")
        _check_position(position)
        _require_todo_for_position(position, status)
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            _check_transition(task.status, status, via_mcp=via_mcp)
            position = _entry_position(task.status, status, position)
            if status == "in_progress" and task.status != "in_progress":
                if self._count_ip(store) >= MAX_IN_PROGRESS:
                    raise PlannerError(
                        f"WIP-ліміт: вже {MAX_IN_PROGRESS} задач в in progress. "
                        "Заверши/поверни одну, перш ніж брати цю."
                    )
            before = task.to_dict()
            was_todo = task.status == "todo"
            task.status = status
            self._write_blocks(task.file, store[task.file])
            self._sync_todo_order(self._all_tasks(store), task, position, not was_todo)
            after = task.to_dict()
            self.history.record(
                "edit", tool="move_task", task_id=task_id, file=task.file,
                changes=diff(before, after),
            )
            return after

    def set_priority(self, task_id: str, priority: str | None) -> dict:
        if priority is not None and priority not in PRIORITIES:
            raise PlannerError(f"Невідомий пріоритет '{priority}'. Дозволено: {', '.join(PRIORITIES)}, або none")
        return self._set(task_id, "priority", priority, tool="set_priority")

    def set_deadline(self, task_id: str, deadline: str | None) -> dict:
        if deadline is not None and not _is_date(deadline):
            raise PlannerError("Дедлайн має бути реальною датою у форматі РРРР-ММ-ДД (напр. 2026-10-01) або none")
        return self._set(task_id, "deadline", deadline, tool="set_deadline")

    def delete_task(self, task_id: str) -> dict:
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            blocks = store[task.file]
            blocks = [b for b in blocks if not (b[0] == "task" and b[1].id == task_id)]  # type: ignore[union-attr]
            self._write_blocks(task.file, blocks)
            store[task.file] = blocks
            self._sync_todo_order(self._all_tasks(store))
            plan = self._read_plan(task_id)
            if plan is not None:
                self._write_plan(task_id, None)  # файл плану без задачі нікому не потрібен
            gone = _with_plan(task.to_dict(), plan)
            self.history.record(
                "delete", tool="delete_task", task_id=task_id, file=task.file, snapshot=gone
            )
            return gone

    def edit_task(
        self,
        task_id: str,
        *,
        text=UNSET,
        status=UNSET,
        priority=UNSET,
        deadline=UNSET,
        energy=UNSET,
        domains=UNSET,
        links=UNSET,
        project=UNSET,
        title=UNSET,
        plan=UNSET,
        position: int | None = None,
        via_mcp: bool = False,
    ) -> dict:
        """Точкове редагування будь-яких полів задачі за один прохід.

        Кожне поле має ТРИ стани: не передане (UNSET) — не чіпаємо; None — очистити;
        значення — записати. Зміна `project` фізично переносить задачу між файлами.
        Зміна `status` підлягає тим самим правилам переходів і WIP-ліміту, що й move_task.
        """
        # --- валідація до будь-якого запису (щоб не лишити файли напівзміненими)
        if status is not UNSET:
            if status is None or status not in STATUSES:
                raise PlannerError(
                    f"Невідомий статус '{status}'. Дозволено: {', '.join(STATUSES)}"
                )
        if priority is not UNSET and priority is not None and priority not in PRIORITIES:
            raise PlannerError(
                f"Невідомий пріоритет '{priority}'. Дозволено: {', '.join(PRIORITIES)}, або none"
            )
        if energy is not UNSET and energy is not None and energy not in ENERGIES:
            raise PlannerError(
                f"Невідома енергія '{energy}'. Дозволено: {', '.join(ENERGIES)}, або none"
            )
        if deadline is not UNSET and deadline is not None and not _is_date(deadline):
            raise PlannerError("Дедлайн має бути реальною датою у форматі РРРР-ММ-ДД (напр. 2026-10-01) або none")
        if text is not UNSET and not (text or "").strip():
            raise PlannerError("Порожній текст задачі")
        if text is not UNSET:
            _require_one_line(text.strip())
        if plan is not UNSET:
            plan = _norm_plan(plan)
        if title is not UNSET:
            title = _require_title(title)
        _check_position(position)

        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            old_plan = self._read_plan(task_id)
            before = _with_plan(task.to_dict(), old_plan)

            if status is not UNSET:
                _check_transition(task.status, status, via_mcp=via_mcp)
            _require_todo_for_position(position, task.status if status is UNSET else status)
            if status is not UNSET:
                position = _entry_position(task.status, status, position)
            if status is not UNSET and status == "in_progress" and task.status != "in_progress":
                if self._count_ip(store) >= MAX_IN_PROGRESS:
                    raise PlannerError(
                        f"WIP-ліміт: вже {MAX_IN_PROGRESS} задач в in progress. "
                        "Заверши/поверни одну, перш ніж брати цю."
                    )

            was_todo = task.status == "todo"
            if text is not UNSET:
                task.text = text.strip()
            if status is not UNSET:
                task.status = status
            if priority is not UNSET:
                task.priority = priority
            if deadline is not UNSET:
                task.deadline = deadline
            if energy is not UNSET:
                task.energy = energy
            if domains is not UNSET:
                task.domains = [d.lstrip("#") for d in (domains or [])]
            if links is not UNSET:
                task.links = list(links or [])
            if title is not UNSET:
                task.title = title
            new_plan = old_plan if plan is UNSET else plan
            if new_plan != old_plan:
                self._write_plan(task_id, new_plan)

            old_rel = task.file
            new_rel = self._target_file(project) if project is not UNSET else old_rel

            if new_rel == old_rel:
                self._write_blocks(old_rel, store[old_rel])
                self._sync_todo_order(self._all_tasks(store), task, position, not was_todo)
                return self._logged_edit(before, task, new_plan)

            # --- перенос між файлами: вийняти зі старого, вставити в новий
            store[old_rel] = [
                b for b in store[old_rel]
                if not (b[0] == "task" and b[1].id == task_id)  # type: ignore[union-attr]
            ]
            task.file = new_rel
            blocks = store.get(new_rel)
            if blocks is None:
                title = (
                    "Tasks — глобальний список" if new_rel == "tasks.md"
                    else f"Задачі — {project}"
                )
                blocks = [("raw", f"# {title}"), ("raw", "")]
            store[new_rel] = _insert_task(blocks, task)
            self._write_blocks(old_rel, store[old_rel])
            self._write_blocks(new_rel, store[new_rel])
            self._sync_todo_order(self._all_tasks(store), task, position, not was_todo)
            return self._logged_edit(before, task, new_plan)

    def vocab(self) -> dict:
        """Довідники для UI: наявні проєкти, домени, звʼязки.

        Дашборд не дає вигадувати проєкт чи звʼязок — їх можна тільки ОБРАТИ
        (нові проєкти заводить AI, звʼязки ведуть у нотатки мозку, якого цей сервіс
        не бачить). Тому список береться з того, що вже реально є у planner.
        """
        with self._lock:
            store = self._load()
            tasks = self._all_tasks(store)
        projects = {_project_of(rel) for rel in store}
        projects |= set(self._template_projects())
        projects |= set(self._projects_registry())
        domains: set[str] = set()
        links: set[str] = set()
        for t in tasks:
            domains |= set(t.domains)
            links |= set(t.links)
        return {
            "projects": sorted(projects),
            "domains": sorted(domains),
            "links": sorted(links),
        }

    # --- Кольори тегів -------------------------------------------------------
    def tag_colors(self) -> dict[str, str]:
        """Збережені кольори тегів; без файлу — порожньо (чіп нейтральний)."""
        path = self.root / TAG_COLORS_FILE
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise PlannerError(f"{TAG_COLORS_FILE} — не валідний JSON")
        if not isinstance(data, dict):
            raise PlannerError(f"{TAG_COLORS_FILE} має бути JSON-обʼєктом")
        return data

    def set_tag_color(self, tag: str, color: str | None) -> dict[str, str]:
        """Ставить колір `#RRGGBB` тегу (без `#`) або знімає його (`None`).

        Тег не мусить мати задач: колір можна задати наперед. Повертає всі кольори."""
        tag = tag.strip().lstrip("#").strip() if isinstance(tag, str) else ""
        if not tag:
            raise PlannerError("не вказано тег")
        if color is not None and not (isinstance(color, str) and _COLOR_RE.match(color)):
            raise PlannerError(f"колір має бути у форматі #RRGGBB, отримано {color!r}")
        with self._lock:
            colors = self.tag_colors()
            if color is None:
                colors.pop(tag, None)
            else:
                colors[tag] = color
            path = self.root / TAG_COLORS_FILE
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(json.dumps(colors, ensure_ascii=False, indent=2) + "\n")
                f.flush()
                os.fsync(f.fileno())
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            os.replace(tmp, path)
        return colors

    # --- Реєстр проєктів -----------------------------------------------------
    def create_project(self, slug: str, name: str, repo: str = "") -> dict:
        """Заводить проєкт у реєстрі. Слаг — `[a-z0-9-]`, не з дефіса, унікальний;
        repo — порожній або `owner/name`. Повертає запис `{slug, name, repo}`."""
        if not (isinstance(slug, str) and _SLUG_RE.match(slug)) or slug == "global":
            raise PlannerError(
                f"слаг проєкту — малі латинські літери, цифри й дефіс, не з дефіса "
                f"(і не 'global'), отримано {slug!r}"
            )
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            raise PlannerError("не вказано назву проєкту")
        repo = repo.strip() if isinstance(repo, str) else repo
        if repo is None:
            repo = ""
        if not (isinstance(repo, str) and (repo == "" or _REPO_RE.match(repo))):
            raise PlannerError(f"repo має бути порожнім або у форматі owner/name, отримано {repo!r}")
        with self._lock:
            registry = self._projects_registry()
            if slug in registry:
                raise PlannerError(f"проєкт {slug!r} уже є")
            registry[slug] = {"name": name, "repo": repo}
            path = self.root / PROJECTS_FILE
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
                f.flush()
                os.fsync(f.fileno())
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            os.replace(tmp, path)
        return {"slug": slug, "name": name, "repo": repo}

    def list_projects(self) -> list[dict]:
        """Проєкти реєстру + ті, що є лише файлом `projects/<слаг>.md` (name/repo порожні).

        Глобальний список (`tasks.md`) — не проєкт. Порядок — за слагом."""
        with self._lock:
            registry = self._projects_registry()
            store = self._load()
        out = {
            slug: {"slug": slug, "name": e.get("name", ""), "repo": e.get("repo", "")}
            for slug, e in registry.items()
        }
        for rel in store:
            slug = _project_of(rel)
            if slug != "global":
                out.setdefault(slug, {"slug": slug, "name": "", "repo": ""})
        return [out[k] for k in sorted(out)]

    def _projects_registry(self) -> dict[str, dict]:
        path = self.root / PROJECTS_FILE
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise PlannerError(f"{PROJECTS_FILE} — не валідний JSON")
        if not isinstance(data, dict):
            raise PlannerError(f"{PROJECTS_FILE} має бути JSON-обʼєктом")
        return data

    def templates(self, project: str | None = None) -> list[dict]:
        """Шаблони задач із `projects/<слаг>/template.md` (по файлу на проєкт).

        Формат — той самий рядок задачі, тож шаблон редагується в Obsidian як звичайний
        список. Файли лежать у ПІДПАПЦІ проєкту, тому під `projects/*.md` не підпадають
        і стором як задачі не читаються.
        """
        out: list[dict] = []
        for slug in self._template_projects():
            if project and slug != project:
                continue
            path = self.root / "projects" / slug / "template.md"
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in raw.split("\n"):
                t = parse_task_line(line)
                if t is None:
                    continue
                out.append({
                    "project": slug,
                    "title": t.title or _auto_title(t.text),
                    "text": t.text,
                    "domains": t.domains,
                    "priority": t.priority,
                    "energy": t.energy,
                    "links": t.links,
                })
        return out

    def _template_projects(self) -> list[str]:
        proj_dir = self.root / "projects"
        if not proj_dir.is_dir():
            return []
        return sorted(
            d.name for d in proj_dir.iterdir() if d.is_dir() and (d / "template.md").exists()
        )

    # --- Elf: робочий цикл агента ---------------------------------------------
    def _read_elf_state(self) -> dict[str, dict]:
        """{id: {"failures": N}}. Немає файлу чи він зіпсований — порожній (усі лічильники 0)."""
        try:
            data = json.loads((self.root / ELF_STATE_FILENAME).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _write_elf_state(self, state: dict[str, dict]) -> None:
        path = self.root / ELF_STATE_FILENAME
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, path)

    def claim(self, agent: str) -> dict | None:
        """Атомарно бере ВЕРХНЮ задачу To Do (ручний порядок) для агента → in_progress.

        Підходить задача з тегом `to-agent` без `blocked` і з тегом агента (без тегів
        `claude`/`codex` — це claude). Черга порожня — None, нічого не рухається."""
        _check_agent(agent)
        with self._lock:
            store = self._load()
            tasks = self._all_tasks(store)
            by_id = {t.id: t for t in tasks}
            task = next(
                (by_id[i] for i in self._todo_order(tasks) if _elf_eligible(by_id[i], agent)),
                None,
            )
            if task is None:
                return None
            if self._count_ip(store) >= MAX_IN_PROGRESS:
                raise PlannerError(
                    f"WIP-ліміт: вже {MAX_IN_PROGRESS} задач в in progress. "
                    "Заверши/поверни одну, перш ніж брати нову."
                )
            before = task.to_dict()
            task.status = "in_progress"
            self._write_blocks(task.file, store[task.file])
            self._sync_todo_order(tasks, task)
            self.history.record(
                "edit", tool="elf_claim", task_id=task.id, file=task.file,
                changes={**diff(before, task.to_dict()), "elf": [None, f"claim ({agent})"]},
            )
            return {
                **task.to_dict(),
                "plan": self._read_plan(task.id),
                "failures": _failures(self._read_elf_state(), task.id),
            }

    def release(self, task_id: str, reason: str, agent: str) -> dict:
        """in_progress → нагору To Do, невдач +1, причина — в журнал і в `## Elf` плану.
        На 3-й невдачі замість цього — block (причина «3 невдач: …»)."""
        _check_agent(agent)
        reason = _elf_reason(reason)
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            _require_in_progress(task)
            return self._elf_fail(store, task, "release", agent, reason, tool="elf_release")

    def _elf_fail(
        self, store: dict[str, list[Block]], task: Task, kind: str, agent: str, reason: str,
        *, tool: str,
    ) -> dict:
        """Спільне для release / red CI / return під уже взятим замком: невдач +1 (старий
        результат CI скидається), задача нагору To Do; 3-тя невдача — block, лічильник
        після блоку скидається в 0."""
        state = self._read_elf_state()
        was = _failures(state, task.id)
        failures = was + 1
        entry = {k: v for k, v in state.get(task.id, {}).items() if k != "ci"}
        blocker = None
        if failures >= ELF_MAX_FAILURES:
            reason = f"{ELF_MAX_FAILURES} невдач: {reason}"
            blocker = _elf_blocker(task, reason, agent)
            failures = 0  # будь-який блок скидає лічильник
        state[task.id] = {**entry, "failures": failures}
        self._write_elf_state(state)
        self._elf_return(
            store, task, "block" if blocker else kind, agent, reason,
            tool=tool, failures=(was, failures), blocker=blocker,
        )
        return self._elf_result(task, failures, blocker)

    def block(self, task_id: str, reason: str, agent: str) -> dict:
        """Нова задача в blockers (`to-admin` + тег агента, той самий проєкт); оригінал:
        −`to-agent`, +`blocked`, +тег агента, нагору To Do. Причина — і в `## Elf` плану;
        лічильник невдач скидається в 0. Можна з in_progress і з in_review (ci-watch не знайшов
        PR); збережений результат CI скидається, посилання на PR лишається."""
        _check_agent(agent)
        reason = _elf_reason(reason)
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            _require_blockable(task)
            blocker = _elf_blocker(task, reason, agent)
            state = self._read_elf_state()
            was = _failures(state, task_id)
            if was or "ci" in state.get(task_id, {}):
                state[task_id] = {
                    **{k: v for k, v in state[task_id].items() if k != "ci"}, "failures": 0,
                }
                self._write_elf_state(state)
            self._elf_return(
                store, task, "block", agent, reason, tool="elf_block",
                failures=(was, 0) if was else None, blocker=blocker,
            )
            return self._elf_result(task, 0, blocker)

    def submit(self, task_id: str, agent: str, pr: str = "") -> dict:
        """in_progress → in_review (задача з тегом `dev`, посилання на PR обов'язкове) або
        testing (без `dev`, PR за бажанням). PR — у `elf-state.json`, рядок — у `## Elf` плану;
        старий результат CI скидається."""
        _check_agent(agent)
        pr = " ".join((pr or "").split())
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            _require_status(task, "in_progress", "передати на рев'ю")
            dev = ELF_TAG_DEV in task.domains
            if dev and not pr:
                raise PlannerError("Для dev-задачі потрібне посилання на PR")
            state = self._read_elf_state()
            entry = {k: v for k, v in state.get(task_id, {}).items() if k != "ci"}
            state[task_id] = {**entry, **({"pr": pr} if pr else {})}
            self._write_elf_state(state)
            target = "in_review" if dev else "testing"
            line = f"submit ({agent}): {target}" + (f", PR {pr}" if pr else "")
            self._elf_advance(store, task, target, line, tool="elf_submit")
            return {"task": _with_plan(task.to_dict(), self._read_plan(task_id)), "pr": pr or None}

    def ci(self, task_id: str, result: str) -> dict:
        """Результат CI для задачі в in_review. green — лише запам'ятати (`ci` у стані);
        red — як невдача: нагору To Do, невдач +1 (3-тя блокує), рядок у `## Elf`."""
        if result not in ELF_CI_RESULTS:
            raise PlannerError(f"Невідомий результат CI '{result}'. Дозволено: {', '.join(ELF_CI_RESULTS)}")
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            _require_status(task, "in_review", "прийняти результат CI")
            if result == "red":
                return self._elf_fail(
                    store, task, "ci", _task_agent(task), "CI red", tool="elf_ci"
                )
            state = self._read_elf_state()
            old = state.get(task_id, {}).get("ci")
            state[task_id] = {**state.get(task_id, {}), "ci": "green"}
            self._write_elf_state(state)
            self.history.record(
                "edit", tool="elf_ci", task_id=task_id, file=task.file,
                changes={"ci": [old, "green"]},
            )
            return self._elf_result(task, _failures(state, task_id), None)

    def review_next(self, agent: str) -> list[dict]:
        """Лише читає: задачі in_review із зеленим CI для агента (без тегів агентів — claude),
        без тегу `to-admin`, у порядку in_review; у кожної id, title і посилання на PR."""
        _check_agent(agent)
        tasks = self.list_tasks(status="in_review")
        with self._lock:
            state = self._read_elf_state()
            by_id = {t.id: t for t in self._all_tasks(self._load())}
        return [
            {"id": t["id"], "title": t["title"] or t["text"], "pr": state[t["id"]].get("pr")}
            for t in tasks
            if state.get(t["id"], {}).get("ci") == "green" and _has_agent(by_id[t["id"]], agent)
            and ELF_TAG_ADMIN not in by_id[t["id"]].domains
        ]

    def merged(self, task_id: str) -> dict:
        """PR змерджено: in_review → testing, рядок у `## Elf`."""
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            _require_status(task, "in_review", "позначити змерджено")
            self._elf_advance(store, task, "testing", "merged", tool="elf_merged")
            return _with_plan(task.to_dict(), self._read_plan(task_id))

    def send_back(self, task_id: str, comment: str, agent: str) -> dict:
        """Рев'юер повертає задачу: in_review → нагору To Do, невдач +1 (3-тя блокує),
        коментар — у `## Elf` плану; результат CI скидається."""
        _check_agent(agent)
        comment = _elf_reason(comment)
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            _require_status(task, "in_review", "повернути з рев'ю")
            return self._elf_fail(store, task, "return", agent, comment, tool="elf_return")

    def escalate(self, task_id: str, reason: str, agent: str) -> dict:
        """Рев'юер віддає задачу Владу: лишається в in_review, +тег `to-admin` (review_next її
        пропускає), причина — у `## Elf` плану; лічильник невдач і результат CI не чіпаємо."""
        _check_agent(agent)
        reason = _elf_reason(reason)
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            _require_status(task, "in_review", "передати адміну")
            old_plan = self._read_plan(task_id)
            before = _with_plan(task.to_dict(), old_plan)
            if ELF_TAG_ADMIN not in task.domains:
                task.domains = [*task.domains, ELF_TAG_ADMIN]
            line = f"escalate ({agent}): {reason}"
            new_plan = _add_elf_line(old_plan, f"- {date.today().isoformat()} {line}")
            self._write_plan(task_id, new_plan)
            self._write_blocks(task.file, store[task.file])
            self.history.record(
                "edit", tool="elf_escalate", task_id=task_id, file=task.file,
                changes={**diff(before, _with_plan(task.to_dict(), new_plan)), "elf": [None, line]},
            )
            return _with_plan(task.to_dict(), new_plan)

    def audit(self, week: str, prs: list[str]) -> tuple[dict, bool]:
        """Задача-нагадування «Аудит Elf тижня <week>» у backlog (проєкт elf, тег to-admin, у плані
        список PR). Ідемпотентно за заголовком (будь-який статус): (задача, створена_чи_ні)."""
        if not isinstance(week, str) or not _AUDIT_WEEK_RE.match(week):
            raise PlannerError(f"Тиждень '{week}' не схожий на РРРР-Wнн (01–53), напр. 2026-W46")
        urls = list(dict.fromkeys(prs or []))
        if not urls:
            raise PlannerError("Порожній список PR")
        if len(urls) > AUDIT_MAX_PRS:
            raise PlannerError(f"Забагато PR: {len(urls)} (максимум {AUDIT_MAX_PRS})")
        for url in urls:
            if not isinstance(url, str) or not _AUDIT_PR_RE.match(url):
                raise PlannerError(f"'{url}' не схоже на URL PR (https://github.com/<owner>/<repo>/pull/<n>)")
        title = f"Аудит Elf тижня {week}"
        with self._audit_lock:
            for t in self.list_tasks():
                if t.get("title") == title:
                    return t, False
            created = self.add_task(
                f"{title}: {len(urls)} PR (green audit + yellow без вибірки) — переглянь після мержу",
                title=title,
                project="elf",
                domains=[ELF_TAG_ADMIN],
                plan="\n".join(f"- {u}" for u in urls) + "\n",
            )
            return created, True

    def _elf_advance(
        self, store: dict[str, list[Block]], task: Task, status: str, line: str, *, tool: str
    ) -> None:
        """Спільний хвіст submit/merged під замком: новий статус, рядок у `## Elf` плану, журнал."""
        old_plan = self._read_plan(task.id)
        before = _with_plan(task.to_dict(), old_plan)
        task.status = status
        new_plan = _add_elf_line(old_plan, f"- {date.today().isoformat()} {line}")
        self._write_plan(task.id, new_plan)
        self._write_blocks(task.file, store[task.file])
        self._sync_todo_order(self._all_tasks(store), task)
        self.history.record(
            "edit", tool=tool, task_id=task.id, file=task.file,
            changes={**diff(before, _with_plan(task.to_dict(), new_plan)), "elf": [None, line]},
        )

    def _elf_return(
        self, store: dict[str, list[Block]], task: Task, kind: str, agent: str, reason: str,
        *, tool: str, failures: tuple[int, int] | None = None, blocker: Task | None = None,
    ) -> None:
        """Спільний хвіст release/block під уже взятим замком: задача нагору To Do, рядок у
        `## Elf` плану, запис у журнал; для block ще й нова задача-блокер."""
        old_plan = self._read_plan(task.id)
        before = _with_plan(task.to_dict(), old_plan)
        task.status = "todo"
        if blocker is not None:
            tags = [d for d in task.domains if d != ELF_TAG_QUEUE]
            task.domains = [*tags, *(t for t in (ELF_TAG_BLOCKED, agent) if t not in tags)]
            blocker.file = task.file
            store[task.file] = _insert_task(store[task.file], blocker)
        line = f"{kind} ({agent}): {reason}"
        new_plan = _add_elf_line(old_plan, f"- {date.today().isoformat()} {line}")
        self._write_plan(task.id, new_plan)
        self._write_blocks(task.file, store[task.file])
        self._sync_todo_order(self._all_tasks(store), task, 1, True)
        changes = {**diff(before, _with_plan(task.to_dict(), new_plan)), "elf": [None, line]}
        if failures:
            changes["failures"] = list(failures)
        self.history.record(
            "edit", tool=tool, task_id=task.id, file=task.file, changes=changes
        )
        if blocker is not None:
            self.history.record(
                "add", tool=tool, task_id=blocker.id, file=blocker.file,
                changes=initial(blocker.to_dict()),
            )

    def _elf_result(self, task: Task, failures: int, blocker: Task | None) -> dict:
        return {
            "task": _with_plan(task.to_dict(), self._read_plan(task.id)),
            "failures": failures,
            "blocker": blocker.to_dict() if blocker else None,
        }

    # --- helpers -------------------------------------------------------------
    def _logged_edit(self, before: dict, task: Task, plan: str | None) -> dict:
        after = _with_plan(task.to_dict(), plan)
        self.history.record(
            "edit", tool="edit_task", task_id=task.id, file=task.file,
            changes=diff(before, after),
        )
        return after

    def _set(self, task_id: str, field_: str, value, *, tool: str) -> dict:
        with self._lock:
            store = self._load()
            task = self._find(store, task_id)
            before = task.to_dict()
            setattr(task, field_, value)
            self._write_blocks(task.file, store[task.file])
            after = task.to_dict()
            self.history.record(
                "edit", tool=tool, task_id=task_id, file=task.file,
                changes=diff(before, after),
            )
            return after

    def _count_ip(self, store: dict[str, list[Block]]) -> int:
        return sum(1 for t in self._all_tasks(store) if t.status == "in_progress")

    def _target_file(self, project: str | None) -> str:
        if project in (None, "", "global"):
            return "tasks.md"
        return f"projects/{_slugify(project)}.md"


def _project_of(relpath: str) -> str:
    """`tasks.md` → 'global'; `projects/home.md` → 'home'."""
    if relpath == "tasks.md":
        return "global"
    return relpath.removeprefix("projects/").removesuffix(".md")


def _require_title(title: str | None) -> str:
    """Заголовок обовʼязковий: ним підписані картка й календар, і порожній там — дірка.

    Тому, на відміну від решти полів, `none` його НЕ очищає, а падає з поясненням.
    """
    t = _clean_title(title)
    if not t:
        raise PlannerError(
            "Заголовок обовʼязковий: додай короткий title "
            f"(≤{TITLE_MAX} символів) — саме він видно на канбані й у календарі"
        )
    return t


def _clean_title(title: str | None) -> str:
    """Нормалізує заголовок: обрізає пробіли й службові символи, стереже ліміт."""
    t = " ".join((title or "").split())
    # ']' зламав би токен 📌[…] при round-trip, '^' — розбір id, '#' — домени.
    t = t.replace("]", ")").replace("[", "(").replace("^", "").replace("#", "")
    if len(t) > TITLE_MAX:
        raise PlannerError(f"Заголовок задачі — максимум {TITLE_MAX} символів (зараз {len(t)})")
    return t


def _auto_title(text: str) -> str:
    """Короткий заголовок із тексту — по межі слова, ≤TITLE_MAX.

    Задачам більше НЕ підставляється (там заголовок обовʼязковий і його пише людина/AI):
    лишився для шаблонів, де рядок може бути без токена 📌[…], і для підказки у формі.
    """
    t = " ".join((text or "").split())
    # Відрізаємо уточнення в дужках/після тире — вони майже завжди «хвіст» формулювання.
    for sep in (" (", " — ", " – ", ": "):
        head = t.split(sep)[0]
        if 12 <= len(head) < len(t):
            t = head
    t = t.replace("]", ")").replace("[", "(").replace("^", "").replace("#", "").strip()
    if len(t) <= TITLE_MAX:
        return t
    cut = t[: TITLE_MAX - 1].rsplit(" ", 1)[0].rstrip(" ,.;:—–-")
    return (cut or t[: TITLE_MAX - 1]) + "…"


def _check_agent(agent: str) -> None:
    if agent not in ELF_AGENTS:
        raise PlannerError(f"Невідомий агент '{agent}'. Дозволено: {', '.join(ELF_AGENTS)}")


def _elf_reason(reason: str) -> str:
    """Причина — один непорожній рядок (рядок журналу в плані й текст блокера не ламаються)."""
    one_line = " ".join((reason or "").split())
    if not one_line:
        raise PlannerError("Вкажи причину")
    return one_line


def _require_in_progress(task: Task) -> None:
    if task.status != "in_progress":
        raise PlannerError(
            f"Задача `{task.id}` у статусі {task.status}: повернути чи заблокувати можна лише in_progress"
        )


def _require_blockable(task: Task) -> None:
    if task.status not in ("in_progress", "in_review"):
        raise PlannerError(
            f"Задача `{task.id}` у статусі {task.status}: заблокувати можна лише з in_progress або in_review"
        )


def _require_status(task: Task, status: str, action: str) -> None:
    if task.status != status:
        raise PlannerError(f"Задача `{task.id}` у статусі {task.status}: {action} можна лише з {status}")


def _failures(state: dict[str, dict], task_id: str) -> int:
    n = state.get(task_id, {}).get("failures", 0)
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


def _elf_eligible(task: Task, agent: str) -> bool:
    """Задачу може взяти `agent`: `to-agent` є, `blocked` нема; тег агента є, або тегів
    агентів нема взагалі і agent = claude."""
    tags = set(task.domains)
    if ELF_TAG_QUEUE not in tags or ELF_TAG_BLOCKED in tags:
        return False
    return _has_agent(task, agent)


def _has_agent(task: Task, agent: str) -> bool:
    """Тег агента є, або тегів агентів нема взагалі і agent = claude."""
    tags = set(task.domains)
    return agent in tags or (agent == "claude" and not tags & set(ELF_AGENTS))


def _task_agent(task: Task) -> str:
    """Агент задачі за тегом; без тегів агентів — claude."""
    return next((a for a in ELF_AGENTS if a in task.domains), "claude")


def _elf_blocker(task: Task, reason: str, agent: str) -> Task:
    """Нова задача для адміна в blockers: id і причина — у тексті, заголовок ≤TITLE_MAX."""
    title = f"Блокер {task.id}: {task.title or task.text}"
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "…"
    return Task(
        id=new_id(),
        status="blockers",
        text=f"Elf ({agent}) заблокований на задачі {task.id}: {reason}",
        domains=[ELF_TAG_ADMIN, agent],
        title=_clean_title(title),
    )


def _add_elf_line(plan: str | None, line: str) -> str:
    """Дописує рядок наприкінці розділу `## Elf` плану; розділу нема — створює його в кінці."""
    if plan is None:
        return f"## Elf\n{line}\n"
    m = _ELF_HEADING_RE.search(plan)
    if m is None:
        base = plan if plan.endswith("\n") else plan + "\n"
        return f"{base}\n## Elf\n{line}\n"
    nxt = _NEXT_HEADING_RE.search(plan, m.end())
    end = nxt.start() if nxt else len(plan)
    section = plan[m.end():end]
    body = section.rstrip()
    return f"{plan[:m.end()]}{body}\n{line}{section[len(body):] or chr(10)}{plan[end:]}"


def _check_transition(old: str, new: str, *, via_mcp: bool = False) -> None:
    """Перехід між колонками дозволений лише зі списку TRANSITION_SOURCES (той самий статус — не перехід).
    Через MCP додатково заборонені MCP_FORBIDDEN_TRANSITIONS (стор і дашборд їх приймають)."""
    if via_mcp and (old, new) in MCP_FORBIDDEN_TRANSITIONS:
        raise PlannerError(
            f"Перехід {old} → {new} заборонено: через MCP його не можна, лише з дашборда"
        )
    allowed = TRANSITION_SOURCES.get(new)
    if old == new or allowed is None or old in allowed:
        return
    raise PlannerError(
        f"Перехід {old} → {new} заборонено: в {new} можна лише з {' або '.join(allowed)}"
    )


def _entry_position(old: str, new: str, position: int | None) -> int | None:
    """Позиція при вході в To Do: з in_progress/in_review без явної позиції — нагору (1);
    з решти колонок без позиції — None (стор поставить в кінець)."""
    if position is None and new == "todo" and old in TODO_TOP_SOURCES:
        return 1
    return position


def _validate(*, status: str, priority: str | None, energy: str | None, deadline: str | None) -> None:
    if status not in STATUSES:
        raise PlannerError(f"Невідомий статус '{status}'. Дозволено: {', '.join(STATUSES)}")
    if priority is not None and priority not in PRIORITIES:
        raise PlannerError(f"Невідомий пріоритет '{priority}'. Дозволено: {', '.join(PRIORITIES)}")
    if energy is not None and energy not in ENERGIES:
        raise PlannerError(f"Невідома енергія '{energy}'. Дозволено: {', '.join(ENERGIES)}")
    if deadline is not None and not _is_date(deadline):
        raise PlannerError("Дедлайн має бути реальною датою у форматі РРРР-ММ-ДД (напр. 2026-10-01)")


def _insert_task(blocks: list[Block], task: Task) -> list[Block]:
    """Вставляє задачу перед секцією Done (## …Done/✅), інакше — у кінець."""
    entry: Block = ("task", task)
    for i, (kind, val) in enumerate(blocks):
        if kind == "raw" and isinstance(val, str) and val.startswith("## ") and (
            "Done" in val or "✅" in val
        ):
            return blocks[:i] + [entry] + blocks[i:]
    # у кінець, прибравши хвостові порожні рядки
    out = list(blocks)
    while out and out[-1][0] == "raw" and out[-1][1] == "":  # type: ignore[index]
        out.pop()
    out.append(entry)
    return out
