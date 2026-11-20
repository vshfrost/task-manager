"""
planner_model — парсер/серіалізатор доменних файлів задач у корені planner (`TASKS_PLANNER_ROOT`).
=========================================================================

Формат рядка (сумісний з Obsidian Tasks):

    - [x] Текст задачі  #домен ⏫ 📅 2026-08-31 ⚡M [[звʼязок]] 📌[Заголовок] ^id

- Статус (у дужках):  ' '=backlog · '>'=to do · 'b'=blockers · '/'=in progress · 'r'=in review · 't'=testing · 'x'=done
- Пріоритет:          🔺=asap · ⏫=high · 🔽=low
- Дедлайн:            📅 РРРР-ММ-ДД
- Енергія:            ⚡S | ⚡M | ⚡L
- Домен:              #tag (можна кілька)
- Звʼязок:            [[wiki-link]] (можна кілька)
- Заголовок:          📌[короткий title, ≤50 симв.] — коротка назва для канбану/календаря
- Стабільний id:      ^abc123 (Obsidian block-id) — додаємо самі для адресації тулами

Модель парсить файл у список «блоків»: кожен рядок — або Task (чекбокс), або Raw
(заголовок/проза/порожній рядок). Це дає round-trip: переписуємо ЛИШЕ рядки-задачі,
решту (заголовки, секції, коментарі) пропускаємо без змін.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

# --- Довідники ---------------------------------------------------------------
STATUS_TO_CHAR = {
    "backlog": " ", "todo": ">", "blockers": "b", "in_progress": "/", "in_review": "r",
    "testing": "t", "done": "x",
}
CHAR_TO_STATUS = {
    " ": "backlog", ">": "todo", "b": "blockers", "/": "in_progress", "r": "in_review",
    "t": "testing", "x": "done", "X": "done",
}
STATUSES = ("backlog", "todo", "blockers", "in_progress", "in_review", "testing", "done")

# Правила переходів: у колонку можна лише ЗВІДСИ. Колонки, яких тут нема (backlog, done),
# приймають задачу з будь-якого статусу.
TRANSITION_SOURCES = {
    "todo": ("backlog", "testing", "in_progress", "in_review", "blockers"),
    "blockers": ("backlog", "todo", "in_progress", "testing"),
    "in_progress": ("todo",),
    "in_review": ("in_progress",),
    "testing": ("in_progress", "in_review", "blockers"),
}
# Перехід, який дозволений стору й дашборду, але заборонений через MCP.
MCP_FORBIDDEN_TRANSITIONS = (("backlog", "todo"),)
# Звідки задача, що повертається в To Do без явної позиції, стає НАГОРУ (інакше — в кінець).
TODO_TOP_SOURCES = ("in_progress", "in_review")

PRIO_TO_EMOJI = {"asap": "🔺", "high": "⏫", "low": "🔽"}
EMOJI_TO_PRIO = {v: k for k, v in PRIO_TO_EMOJI.items()}
PRIORITIES = ("asap", "high", "low")

ENERGIES = ("S", "M", "L")

# Ліміт короткого заголовка. Він існує заради верстки: у календарі й на картці
# показуємо саме title, і довгий рядок ламав би сітку.
TITLE_MAX = 50

# --- Регекси токенів ---------------------------------------------------------
_TASK_RE = re.compile(r"^(\s*)[-*]\s+\[(.)\]\s+(.*)$")
_ID_RE = re.compile(r"\s*\^([A-Za-z0-9]+)\s*$")
_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_DEADLINE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_ENERGY_RE = re.compile(r"⚡\s*([SML])")
_PRIO_RE = re.compile(r"(🔺|⏫|🔽)")
_DOMAIN_RE = re.compile(r"(?<!\S)#([^\s#]+)")
# Заголовок у явних дужках — інакше його не відрізнити від тексту задачі.
# Одинарні дужки не конфліктують із [[wiki-link]], бо ті знімаються раніше.
_TITLE_RE = re.compile(r"📌\s*\[([^\]]*)\]")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def new_id() -> str:
    """Короткий стабільний id (6 hex)."""
    return secrets.token_hex(3)


@dataclass
class Task:
    id: str
    status: str  # one of STATUSES
    text: str
    domains: list[str] = field(default_factory=list)
    priority: str | None = None  # one of PRIORITIES or None
    deadline: str | None = None  # YYYY-MM-DD or None
    energy: str | None = None  # S/M/L or None
    links: list[str] = field(default_factory=list)
    title: str = ""  # короткий заголовок (≤TITLE_MAX); "" = не заданий
    indent: str = ""  # збережений відступ (для підзадач)
    file: str = ""  # відносний шлях файлу в planner (заповнює стор)
    had_id: bool = True  # чи був ^id у вихідному рядку (для одноразового «запікання» id)

    def render(self) -> str:
        char = STATUS_TO_CHAR[self.status]
        line = f"{self.indent}- [{char}] {self.text}".rstrip()
        parts: list[str] = []
        parts += [f"#{d}" for d in self.domains]
        if self.priority:
            parts.append(PRIO_TO_EMOJI[self.priority])
        if self.deadline:
            parts.append(f"📅 {self.deadline}")
        if self.energy:
            parts.append(f"⚡{self.energy}")
        parts += [f"[[{l}]]" for l in self.links]
        if self.title:
            parts.append(f"📌[{self.title}]")
        if parts:
            line += "  " + " ".join(parts)
        line += f" ^{self.id}"
        return line

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "text": self.text,
            "domains": self.domains,
            "priority": self.priority,
            "deadline": self.deadline,
            "energy": self.energy,
            "links": self.links,
            "title": self.title,
            "file": self.file,
        }


def parse_task_line(line: str) -> Task | None:
    """Парсить один рядок у Task або None, якщо це не чекбокс."""
    m = _TASK_RE.match(line)
    if not m:
        return None
    indent, char, rest = m.group(1), m.group(2), m.group(3)
    status = CHAR_TO_STATUS.get(char)
    if status is None:
        return None  # невідомий статус-символ → не наша задача, лишаємо як raw

    tid = None
    mid = _ID_RE.search(rest)
    if mid:
        tid = mid.group(1)
        rest = rest[: mid.start()]

    title = ""
    mt = _TITLE_RE.search(rest)
    if mt:
        title = mt.group(1).strip()
        rest = _TITLE_RE.sub("", rest)

    links = _LINK_RE.findall(rest)
    rest = _LINK_RE.sub("", rest)

    deadline = None
    md = _DEADLINE_RE.search(rest)
    if md:
        deadline = md.group(1)
        rest = _DEADLINE_RE.sub("", rest)

    energy = None
    me = _ENERGY_RE.search(rest)
    if me:
        energy = me.group(1)
        rest = _ENERGY_RE.sub("", rest)

    priority = None
    mp = _PRIO_RE.search(rest)
    if mp:
        priority = EMOJI_TO_PRIO[mp.group(1)]
        rest = _PRIO_RE.sub("", rest)

    domains = _DOMAIN_RE.findall(rest)
    rest = _DOMAIN_RE.sub("", rest)

    text = re.sub(r"\s{2,}", " ", rest).strip()

    return Task(
        id=tid or new_id(),
        status=status,
        text=text,
        domains=domains,
        priority=priority,
        deadline=deadline,
        energy=energy,
        links=links,
        title=title,
        indent=indent,
        had_id=(tid is not None),
    )


# Блок файлу: ("raw", рядок) або ("task", Task)
Block = tuple[str, object]


def parse_file(text: str, relpath: str = "") -> list[Block]:
    blocks: list[Block] = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            blocks.append(("raw", line))
            continue
        t = None if in_fence else parse_task_line(line)
        if t is None:
            blocks.append(("raw", line))
        else:
            t.file = relpath
            blocks.append(("task", t))
    return blocks


def render_file(blocks: list[Block]) -> str:
    out: list[str] = []
    for kind, val in blocks:
        if kind == "task":
            out.append(val.render())  # type: ignore[union-attr]
        else:
            out.append(val)  # type: ignore[arg-type]
    return "\n".join(out)
