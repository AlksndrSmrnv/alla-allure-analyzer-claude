"""Похожие проблемы после разбора: кандидаты, задание и решение модели.

Кластеризация осторожна: почти одинаковые ошибки она разводит по шагу, логу, ресурсам и
assertion (``core.md``), а разные симптомы одной причины склеить не может вовсе. После
разбора видно больше — причина, первый шаг и место в коде. Скрипт отбирает кандидатов
(``candidate_groups``), модель одним шагом (``STATUS: relate``) решает, у каких из них одна
причина, и пишет ``related.md``. Отчёт, краткий разбор и сводка показывают такие проблемы
одной — «Проблема 4 и 5 и 8» с меткой «объединил агент»; номера, разборы и тесты каждой
остаются (по ним работают ``apply``, ``remember`` и ``reject``).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from math import log
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.agent_rules import EXECUTOR_RULES, RELATED_FORMAT_REF, reference_line
from alla_skill_lib.analysis_format import ClusterAnalysis, code_locations, first_sentence
from alla_skill_lib.cluster_task import UNTRUSTED_NOTE
from alla_skill_lib.known_issues import KnownIssues

# Проблем в группе кандидатов и групп в задании: задание не растёт с прогоном.
MAX_RELATE_GROUP = 8
MAX_RELATE_GROUPS = 15
# Похожесть причины и первого шага (взвешенный Жаккар по словам, вес — idf по разборам
# прогона) и сколько значимых слов должно совпасть.
TEXT_SIMILARITY = 0.5
MIN_SHARED_WORDS = 3
# «Одно место в коде»: тот же файл и строки не дальше этого.
LINE_WINDOW = 5
RELATE_FIELD_CHARS = 220
RELATED_CAUSE_CHARS = 200

NEAR, CODE, KB, TEXT = "near", "code", "kb", "text"
EDGE_KINDS = (NEAR, CODE, KB, TEXT)
KB_ORIGINS = {"exact": "точное совпадение", "fingerprint": "признак найден в данных"}
# Что развело близкие кластеры (``NearPair.reason``) — словами для задания: это довод и за
# разные причины, модель должна его видеть.
NEAR_WHY = {
    "step": "текст ошибки почти тот же, но тесты упали на разных шагах",
    "log": "текст ошибки почти тот же, но в логах разные ошибки",
    "resource": "текст ошибки почти тот же, но разные хосты или элементы страницы",
    "assertion": "текст ошибки почти тот же, но разное фактическое значение в проверке",
    "message": "текст ошибки почти тот же у части тестов, у остальных — другой",
    "score": "текст ошибки почти тот же, но остальные данные (трейс, лог) различаются",
}

SAME, DIFFERENT = "ОДНА ПРИЧИНА", "РАЗНЫЕ"
RELATED_FORMAT = f"""\
{SAME}: <номера проблем через запятую> — <общая причина одной фразой>
{DIFFERENT}: <номера проблем через запятую> — <чем отличаются, по разборам>"""

RELATE_RULES = """\
- Ты сравниваешь готовые разборы похожих проблем одного прогона. Опирайся только на
  данные ниже: новых фактов и версий не добавляй, код и логи не открывай.
- Пиши по-русски, коротко и простым языком: «проблема», а не «кластер»."""

RELATE_TASK = f"""\
Для каждой группы реши, у каких проблем одна причина — то есть одно исправление уберёт
их все. Сравнивай причину, что сломалось, первый шаг исправления и место в коде.
Похожий текст ошибки — ещё не одна причина. Строка «почему сравниваются» говорит, чем
данные различаются: разные ошибки в логе, хосты или элементы страницы — довод за
{DIFFERENT}; разные шаги теста сами по себе не довод — одна ошибка сервиса бывает из разных
тестов. Строка «БАЗА ЗНАНИЙ» — запись базы знаний проекта, которая подходит данным
проблемы (признак записи нашёлся в её данных — это не похожесть текста ошибки): одна и та
же запись у проблем — довод за {SAME}, если их разборы не называют разных причин; у
проблем только разные записи — довод за {DIFFERENT}. Общую причину тогда можно взять из
причины этой записи. Проблемы из строки «{SAME}» отчёт покажет одной проблемой, и ошибочное объединение
спрячет отдельный сбой. Объединяй, только если разборы называют одну и ту же причину и одно
исправление; сомневаешься — {DIFFERENT}.
Запиши в файл только строки такого вида, по строке на решение:
{RELATED_FORMAT}
Номера — номера проблем («Проблема 4»), не групп. Каждый номер проблемы из групп — ровно в
одной строке. В строке «{SAME}» — минимум два номера из одной группы; в группе может быть
несколько таких строк."""

_LINE_RE = re.compile(
    r"^[\s>*_-]*(?P<kind>одна\s+причина|разные)[*_]*\s*[:：][*_]*\s*"
    r"(?P<numbers>\d+(?:\s*(?:,|;|\bи\b)\s*\d+)*)\s*[,;.]?\s*"
    r"(?:(?:[—–:-]+)\s*(?P<text>.*))?$",
    re.IGNORECASE,
)
_SKIP_LINE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})[\w-]*\s*$|^\s*#")
_WORD_RE = re.compile(r"[^\W_]+")
_DIGITS_RE = re.compile(r"\d+")
# Служебные слова не говорят о причине; короткие (до 3 букв) отсекаются отдельно.
_STOP_WORDS = frozenset(
    "что это при для как или его она они ещё уже нет так чем где когда если тест теста "
    "тесты тестов проблема ошибка ошибки ошибку причина after with from that the and for "
    "not".split()
)


@dataclass(frozen=True)
class Edge:
    """Почему две проблемы попали в одну группу кандидатов."""

    first: str
    second: str
    kind: str  # near | code | kb | text
    why: str


@dataclass
class CandidateGroup:
    """Проблемы, которые скрипт просит сравнить, и почему."""

    file_ids: list[str]
    edges: list[Edge]


@dataclass
class RelatedGroup:
    """Проблемы, у которых, по решению модели, одна причина."""

    file_ids: list[str]
    cause: str
    size: int  # тестов во всех проблемах группы


@dataclass
class Related:
    """Группы «одна причина» по решению модели: в отчёте — одна проблема с номерами всех."""

    groups: list[RelatedGroup] = field(default_factory=list)

    def group_of(self, file_id: str) -> RelatedGroup | None:
        return next((group for group in self.groups if file_id in group.file_ids), None)

    def without(self, file_ids: set[str], run: dict[str, Any]) -> Related:
        """Группы без этих проблем; группа, где осталось меньше двух, распадается.

        Так из групп выходят проблемы с подтверждённым выводом расширения: их причину назвал
        инженер, и объединённый пункт отчёта не должен прятать её за догадкой агента.
        """
        sizes = {entry["file_id"]: int(entry["member_count"]) for entry in run["clusters"]}
        groups = []
        for group in self.groups:
            left = [file_id for file_id in group.file_ids if file_id not in file_ids]
            if len(left) == len(group.file_ids):
                groups.append(group)
            elif len(left) >= 2:
                groups.append(RelatedGroup(left, group.cause, sum(sizes[f] for f in left)))
        groups.sort(key=lambda group: (-group.size, int(group.file_ids[0])))
        return Related(groups)


@dataclass
class RelatedDecision:
    """Разобранный ``related.md``: строки решений и нераспознанные строки."""

    same: list[tuple[list[int], str]] = field(default_factory=list)
    different: list[tuple[list[int], str]] = field(default_factory=list)
    unrecognized: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Кандидаты
# ---------------------------------------------------------------------------


def candidate_groups(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    project_root: Path,
    known: KnownIssues | None = None,
    kb: dict[str, list[dict[str, Any]]] | None = None,
) -> list[CandidateGroup]:
    """Группы проблем, которые стоит сравнить после разбора.

    Сравниваются принятые разборы модели одной категории: не ``auto``, не «формат нарушен»,
    не «СОГЛАСОВАННОСТЬ: разные проблемы», не по записи базы знаний, которую пользователь
    отверг для этой проблемы (причина не подтверждена), и не с подтверждённой записью
    (``known.refs``: причина уже известна и показана блоком известных проблем — иначе одна
    проблема попала бы в оба блока сводки). Связь — близкая пара кластеров (почти одинаковый
    текст ошибки), одно место в «КОД», общая запись базы знаний (она узнаёт ошибку по данным
    кластера) или похожие причина и первый шаг; у категории «неизвестно» — только близкая пара
    и запись базы знаний: её причины пишутся одними словами. Группа — компонента связности, не больше ``MAX_RELATE_GROUP`` крупнейших
    проблем; групп — не больше ``MAX_RELATE_GROUPS``, крупные первыми. ``kb`` — записи базы
    знаний, подходящие кластерам сейчас (``known_issues.current_kb_matches``); без него связей
    по базе знаний нет.
    """
    entries = {entry["file_id"]: entry for entry in run["clusters"]}
    eligible = [
        file_id for file_id, entry in entries.items()
        if file_id in analyses and file_id not in flagged and not entry["auto"]
        and analyses[file_id].category is not None
        and analyses[file_id].consistency_kind != "different"
        and not (known and (file_id in known.rejected or file_id in known.refs))
    ]

    def comparable(a: str, b: str) -> bool:
        return analyses[a].category == analyses[b].category

    edges: dict[tuple[str, str], Edge] = {}

    def add(a: str, b: str, kind: str, why: str) -> None:
        first, second = sorted((a, b), key=int)
        edges.setdefault((first, second), Edge(first, second, kind, why))

    allowed = set(eligible)
    by_cluster = {entry["cluster_id"]: entry["file_id"] for entry in run["clusters"]}
    for pair in (run.get("clustering") or {}).get("near_pairs") or []:
        a, b = by_cluster.get(pair.get("cluster_a")), by_cluster.get(pair.get("cluster_b"))
        if a is not None and b is not None and a in allowed and b in allowed and a != b \
                and comparable(a, b):
            add(a, b, NEAR, NEAR_WHY.get(str(pair.get("reason")), NEAR_WHY["score"]))

    known_cause = {file_id for file_id in eligible if analyses[file_id].category != "неизвестно"}
    locations = {file_id: code_locations(analyses[file_id], project_root) for file_id in known_cause}
    words = {file_id: _words(analyses[file_id]) for file_id in known_cause}
    idf = _idf(words.values())
    records = {file_id: (kb or {}).get(file_id, []) for file_id in eligible}
    for index, a in enumerate(eligible):
        for b in eligible[index + 1:]:
            if not comparable(a, b):
                continue
            both_known = a in known_cause and b in known_cause
            place = _same_place(locations[a], locations[b]) if both_known else ""
            shared = _shared_record(records[a], records[b])
            if place:
                add(a, b, CODE, f"одно место в коде: {place}")
            elif shared is not None:
                # Запись узнаёт ошибку по данным, а не по словам разбора: связь и у «неизвестно».
                add(a, b, KB, f"обеим подходит запись базы знаний {shared['id']} "
                              f"«{_clip(str(shared.get('title') or ''), 80)}»")
            elif both_known and _similar(words[a], words[b], idf):
                add(a, b, TEXT, "похожие причина и первый шаг исправления")

    groups = []
    for component in _components(list(edges)):
        members = sorted(component, key=lambda file_id: (-int(entries[file_id]["member_count"]), int(file_id)))
        kept = set(members[:MAX_RELATE_GROUP])
        inner = [edge for key, edge in edges.items() if key[0] in kept and key[1] in kept]
        linked = {file_id for edge in inner for file_id in (edge.first, edge.second)}
        if len(linked) >= 2:
            groups.append(CandidateGroup(sorted(linked, key=int), sorted(
                inner, key=lambda edge: (int(edge.first), int(edge.second)))))
    groups.sort(key=lambda group: (
        -sum(int(entries[file_id]["member_count"]) for file_id in group.file_ids),
        int(group.file_ids[0]),
    ))
    return groups[:MAX_RELATE_GROUPS]


def _kb_key(match: dict[str, Any]) -> tuple[str, str]:
    """Запись — папка базы знаний модуля и id: у модулей свои записи с одинаковыми id."""
    return str(match.get("kb_dir") or ""), str(match.get("id"))


def _kb_line(match: dict[str, Any], *, with_module: bool) -> str:
    origin = KB_ORIGINS.get(str(match.get("origin")), KB_ORIGINS["fingerprint"])
    where = f"{match.get('kb_label')}; " if with_module and match.get("kb_label") else ""
    line = (f"БАЗА ЗНАНИЙ: {match.get('id')} «{_clip(str(match.get('title') or ''), 80)}» "
            f"({where}{match.get('category')}; {origin})")
    cause = " ".join(str(match.get("description") or "").split())
    return f"{line} — причина записи: {_clip(cause, 160)}" if cause else line


def _shared_record(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> dict[str, Any] | None:
    keys = {_kb_key(match) for match in b}
    return next((match for match in a if _kb_key(match) in keys), None)


def _words(analysis: ClusterAnalysis) -> set[str]:
    text = f"{analysis.cause_reason} {analysis.first_fix_step()}".lower().replace("ё", "е")
    text = _DIGITS_RE.sub("#", text)
    return {word for word in _WORD_RE.findall(text) if len(word) > 3 and word not in _STOP_WORDS}


def _idf(documents: Any) -> dict[str, float]:
    documents = list(documents)
    counts: Counter[str] = Counter(word for words in documents for word in words)
    total = len(documents)
    return {word: log((1 + total) / (1 + count)) + 1.0 for word, count in counts.items()}


def _similar(a: set[str], b: set[str], idf: dict[str, float]) -> bool:
    shared = a & b
    if len(shared) < MIN_SHARED_WORDS:
        return False
    union = sum(idf[word] for word in a | b)
    return union > 0 and sum(idf[word] for word in shared) / union >= TEXT_SIMILARITY


def _same_place(a: list[tuple[str, int | None]], b: list[tuple[str, int | None]]) -> str:
    """Общее место в коде двух разборов: «путь:строка» или пусто."""
    for path_a, line_a in a:
        for path_b, line_b in b:
            if path_a == path_b and line_a is not None and line_b is not None \
                    and abs(line_a - line_b) <= LINE_WINDOW:
                return f"{path_a}:{min(line_a, line_b)}"
    return ""


def _components(pairs: list[tuple[str, str]]) -> list[set[str]]:
    parent: dict[str, str] = {}

    def root(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for a, b in pairs:
        parent[root(a)] = root(b)
    found: dict[str, set[str]] = {}
    for item in parent:
        found.setdefault(root(item), set()).add(item)
    return list(found.values())


def candidate_stats(groups: list[CandidateGroup]) -> dict[str, Any]:
    """Только числа — для ``state.json`` и ``pilot-summary.md``."""
    kinds = Counter(edge.kind for group in groups for edge in group.edges)
    return {
        "groups": len(groups),
        "problems": sum(len(group.file_ids) for group in groups),
        "edges": {kind: kinds[kind] for kind in EDGE_KINDS if kinds[kind]},
    }


# ---------------------------------------------------------------------------
# Задание
# ---------------------------------------------------------------------------


def relate_data(
    run: dict[str, Any], groups: list[CandidateGroup], analyses: dict[str, ClusterAnalysis],
    kb: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """Блок «Группы» задания; его SHA-256 — ``state.related_data_hash``."""
    entries = {entry["file_id"]: entry for entry in run["clusters"]}
    lines: list[str] = []
    for number, group in enumerate(groups, start=1):
        numbers = ", ".join(str(int(file_id)) for file_id in group.file_ids)
        lines += ["", f"--- Группа {number}: проблемы {numbers} ---", "Почему сравниваются:"]
        lines += [f"- {int(edge.first)} и {int(edge.second)}: {edge.why}" for edge in group.edges]
        # Записи из разных папок базы знаний (модулей) — разные, даже с одним id: назвать папку.
        with_module = len({str(match.get("kb_dir") or "") for file_id in group.file_ids
                           for match in (kb or {}).get(file_id, [])}) > 1
        for file_id in group.file_ids:
            entry, analysis = entries[file_id], analyses[file_id]
            tests = int(entry["member_count"])
            lines += ["", f"Проблема {int(file_id)} · {tests} {_tests(tests)} · "
                          f"ошибка: {_clip(str(entry.get('label') or ''))}"]
            lines.append(f"ПРИЧИНА: {_clip(analysis.cause)}")
            what = first_sentence(analysis.what)
            if what:
                lines.append(f"ЧТО СЛОМАЛОСЬ: {_clip(what)}")
            step = analysis.first_fix_step()
            if step:
                lines.append(f"ПЕРВЫЙ ШАГ ИСПРАВЛЕНИЯ: {_clip(step)}")
            lines += [f"КОД: {_clip(line)}" for line in analysis.code[:2]]
            records = (kb or {}).get(file_id, [])
            lines += [_kb_line(match, with_module=with_module) for match in records]
    return "\n".join(lines).strip("\n")


def build_relate_task(run: dict[str, Any], data: str, paths: ws.RunPaths) -> str:
    return "\n".join([
        f"# Похожие проблемы прогона #{run['launch_id']}",
        "",
        "Скрипт нашёл проблемы с похожими ошибками или разборами. Реши, у каких из них одна",
        "причина, и запиши решение в файл (абсолютный путь, инструментом записи файлов):",
        str(paths.related),
        "",
        "Затем выполни:",
        paths.next_command(),
        "",
        UNTRUSTED_NOTE,
        "",
        "## Правила",
        RELATE_RULES,
        EXECUTOR_RULES,
        "",
        "## Группы",
        "",
        data,
        "",
        "## Задание",
        RELATE_TASK,
        "",
        reference_line(RELATED_FORMAT_REF, "Формат с примером"),
    ]) + "\n"


# ---------------------------------------------------------------------------
# Решение модели
# ---------------------------------------------------------------------------


def parse_related(text: str) -> RelatedDecision:
    decision = RelatedDecision()
    for line in text.splitlines():
        if not line.strip() or _SKIP_LINE_RE.match(line):
            continue
        match = _LINE_RE.match(line.strip())
        if match is None:
            decision.unrecognized.append(line.strip())
            continue
        numbers = [int(number) for number in re.findall(r"\d+", match.group("numbers"))]
        reason = " ".join((match.group("text") or "").split()).strip(" «»\"")
        kind = " ".join(match.group("kind").upper().split())
        (decision.same if kind == SAME else decision.different).append((numbers, reason))
    return decision


def validate_related(decision: RelatedDecision, groups: list[CandidateGroup]) -> list[str]:
    errors = [
        f"строка не распознана: «{_clip(line, 120)}» — пиши только строки «{SAME}: …» и "
        f"«{DIFFERENT}: …»"
        for line in decision.unrecognized
    ]
    if not decision.same and not decision.different:
        return errors or [f"нет ни одной строки «{SAME}: …» или «{DIFFERENT}: …»"]
    group_of = {int(file_id): index for index, group in enumerate(groups) for file_id in group.file_ids}
    seen: Counter[int] = Counter()
    for numbers, _ in decision.same + decision.different:
        seen.update(numbers)
    unknown = sorted(number for number in seen if number not in group_of)
    if unknown:
        errors.append(f"{_problems(unknown)} нет в группах задания — пиши только номера из «Групп»")
    repeated = sorted(number for number, count in seen.items() if count > 1)
    if repeated:
        errors.append(f"{_problems(repeated)} встречается в нескольких строках — у каждого номера одна строка")
    missing = sorted(number for number in group_of if number not in seen)
    if missing:
        errors.append(f"нет решения по {_problems(missing, dative=True)} — добавь их в строку "
                      f"«{SAME}» или «{DIFFERENT}»")
    for numbers, reason in decision.same:
        listed = ", ".join(map(str, numbers))
        if len(set(numbers)) < 2:
            errors.append(f"в строке «{SAME}: {listed}» нужно минимум два номера — одну проблему "
                          f"пиши в «{DIFFERENT}»")
        elif len({group_of[number] for number in numbers if number in group_of}) > 1:
            errors.append(f"в строке «{SAME}: {listed}» проблемы из разных групп задания — "
                          "объединять можно только проблемы одной группы")
        if not reason:
            errors.append(f"в строке «{SAME}: {listed}» после номеров нет общей причины — "
                          "допиши её через «—»")
    return errors


def related_groups(decision: RelatedDecision, run: dict[str, Any]) -> Related:
    entries = {int(entry["file_id"]): entry for entry in run["clusters"]}
    groups = []
    for numbers, reason in decision.same:
        file_ids = [entries[number]["file_id"] for number in sorted(set(numbers))]
        size = sum(int(entries[number]["member_count"]) for number in set(numbers))
        groups.append(RelatedGroup(file_ids, _clip(reason, RELATED_CAUSE_CHARS), size))
    groups.sort(key=lambda group: (-group.size, int(group.file_ids[0])))
    return Related(groups)


def _problems(numbers: list[int], *, dative: bool = False) -> str:
    listed = ", ".join(map(str, numbers))
    if dative:
        return ("проблеме " if len(numbers) == 1 else "проблемам ") + listed
    return ("проблемы " if len(numbers) == 1 else "проблем ") + listed


def _tests(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "тест"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "теста"
    return "тестов"


def _clip(text: str, limit: int = RELATE_FIELD_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
