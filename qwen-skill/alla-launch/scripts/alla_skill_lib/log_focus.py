"""Отбор фрагмента лога под лимит задания по связи с ошибкой.

``log_snippet`` теста — секции ``--- [<тип>: <имя>] ---`` из разных вложений;
внутри секции блоки разделены пустой строкой (``[ERROR]``-строка вместе со
своим стек-трейсом, HTTP-сигналы, JSON-журнал). Если всё не помещается в
лимит, лог не режется по началу: блоки ранжируются по пересечению с текстом
ошибки (ID запросов весят больше слов), а оставшееся место занимают самые
ранние блоки — первопричина часто не имеет общих слов с assertion. Порядок
событий сохраняется, пропуски и не вошедшие вложения помечаются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FOCUS_NOTE = "[лог сокращён: отобраны блоки по связи с ошибкой, пропуски помечены]"
CONTEXT_LINES = 2
ID_WEIGHT = 3.0
ERROR_HINT_BONUS = 0.5

_SECTION_HEADER_RE = re.compile(r"^--- \[[^\]:\s][^\]:]*?: .+?\] ---$", re.MULTILINE)
_BLOCK_SPLIT_RE = re.compile(r"\n[ \t]*\n")
_EXTRACTION_MARKER_RE = re.compile(r"^\[\.\.\. обрезано:")
_ERROR_HINT_RE = re.compile(
    r"\b(?:ERROR|FATAL|SEVERE|CRITICAL)\b|\w(?:Exception|Error)\b|Caused by|Traceback"
    r"|\bfail(?:ed|ure)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"\b[^\W\d_]\w{3,}\b")
_ID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    r"|\b[0-9a-f]{16,}\b"
    r"|\b\d{5,}\b",
    re.IGNORECASE,
)
_STOP_WORDS = frozenset({
    "error", "errors", "exception", "failed", "failure", "fail", "expected",
    "actual", "assert", "assertion", "assertionerror", "java", "lang", "null",
    "true", "false", "with", "from", "that", "this", "test", "tests", "caused",
    "info", "warn", "warning", "debug", "trace", "http", "https", "status",
    "code", "message", "value", "while", "when", "then", "none", "unknown",
    "ошибка", "ошибки", "ошибку", "было", "была", "ожидалось", "получено",
})


@dataclass
class _Block:
    section: int
    position: int
    text: str
    score: float
    pinned: bool

    @property
    def cost(self) -> int:
        return len(self.text) + 2  # блоки разделяются пустой строкой


def error_tokens(text: str) -> dict[str, float]:
    """Токены текста ошибки с весами: ID запросов ×3, значимые слова ×1."""
    tokens: dict[str, float] = {}
    for word in _WORD_RE.findall(text.lower()):
        if word not in _STOP_WORDS:
            tokens[word] = 1.0
    for identifier in _ID_RE.findall(text.lower()):
        tokens[identifier] = ID_WEIGHT
    return tokens


def focus_log(snippet: str, error_text: str, budget: int) -> str:
    """Вернуть лог не длиннее ``budget`` символов (короткий — без изменений)."""
    if len(snippet) <= budget:
        return snippet
    tokens = error_tokens(error_text)
    sections = _split_sections(snippet)
    blocks: list[_Block] = []
    for section_index, (_header, texts) in enumerate(sections):
        for text in texts:
            if len(text) > budget // 2:
                text = _shrink_block(text, tokens, budget // 2)
            blocks.append(_Block(
                section=section_index,
                position=len(blocks),
                text=text,
                score=_block_score(text, tokens),
                pinned=bool(_EXTRACTION_MARKER_RE.match(text)),
            ))

    priority = sorted(blocks, key=lambda b: (not b.pinned, -b.score, b.position))
    chosen: list[_Block] = []
    available = budget - _overhead(sections)
    for block in priority:
        if block.cost <= available:
            chosen.append(block)
            available -= block.cost

    result = _render(sections, blocks, {b.position for b in chosen})
    while len(result) > budget and chosen:
        chosen.pop()  # сначала уходят наименее важные
        result = _render(sections, blocks, {b.position for b in chosen})
    if not chosen and priority:
        # Лимит меньше заголовков и маркеров: лучше начало самого важного
        # блока, чем одни пометки о пропусках.
        room = budget - len(FOCUS_NOTE) - 3
        if room > 0:
            return f"{FOCUS_NOTE}\n\n{priority[0].text[:room]}…"
    return result if len(result) <= budget else result[: budget - 1] + "…"


def _split_sections(snippet: str) -> list[tuple[str | None, list[str]]]:
    matches = list(_SECTION_HEADER_RE.finditer(snippet))
    if not matches:
        return [(None, _split_blocks(snippet))]
    sections: list[tuple[str | None, list[str]]] = []
    preamble = snippet[: matches[0].start()]
    if preamble.strip():
        sections.append((None, _split_blocks(preamble)))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(snippet)
        sections.append((match.group(0), _split_blocks(snippet[match.end():end])))
    return sections


def _split_blocks(body: str) -> list[str]:
    return [block.strip("\n") for block in _BLOCK_SPLIT_RE.split(body) if block.strip()]


def _score(text: str, tokens: dict[str, float]) -> float:
    if not tokens:
        return 0.0
    present = set(_WORD_RE.findall(text.lower())) | set(_ID_RE.findall(text.lower()))
    return sum(weight for token, weight in tokens.items() if token in present)


def _block_score(text: str, tokens: dict[str, float]) -> float:
    """Пересечение с ошибкой + небольшой бонус блокам с явной ошибкой приложения.

    Бонус меньше одного совпавшего слова: он лишь поднимает ERROR-блоки над
    «шумом» (HTTP-сигналы, INFO-журнал), когда пересечения нет ни у кого.
    """
    bonus = ERROR_HINT_BONUS if _ERROR_HINT_RE.search(text) else 0.0
    return _score(text, tokens) + bonus


def _shrink_block(text: str, tokens: dict[str, float], limit: int) -> str:
    """Сократить огромный блок до значимых строк и их контекста.

    Значимые строки — совпадающие с текстом ошибки; если таких нет (assertion
    не пересекается с логом) — строки с явными ошибками приложения; если нет
    и их — начало блока, сколько поместится.
    """
    lines = text.split("\n")
    anchors = [i for i, line in enumerate(lines) if _score(line, tokens) > 0]
    if not anchors:
        anchors = [i for i, line in enumerate(lines) if _ERROR_HINT_RE.search(line)]
    if not anchors:
        anchors = list(range(len(lines)))  # начало блока, дальше отрежет лимит
    keep = {0}
    for index in anchors:
        keep.update(range(index - CONTEXT_LINES, index + CONTEXT_LINES + 1))
    kept: list[str] = []
    previous = -1
    size = 0
    for index in sorted(i for i in keep if 0 <= i < len(lines)):
        piece = lines[index]
        if index != previous + 1:
            piece = "[…]\n" + piece
        if size + len(piece) + 1 > limit - 4:
            break
        kept.append(piece)
        size += len(piece) + 1
        previous = index
    if previous < len(lines) - 1:
        kept.append("[…]")
    return "\n".join(kept)


def _overhead(sections: list[tuple[str | None, list[str]]]) -> int:
    headers = sum(len(header) + 2 for header, _ in sections if header)
    return len(FOCUS_NOTE) + 2 + headers + 60 * len(sections)


def _render(
    sections: list[tuple[str | None, list[str]]],
    blocks: list[_Block],
    chosen: set[int],
) -> str:
    parts = [FOCUS_NOTE]
    for section_index, (header, _texts) in enumerate(sections):
        own = [block for block in blocks if block.section == section_index]
        lines: list[str] = [header] if header else []
        if not any(block.position in chosen for block in own):
            total = sum(block.text.count("\n") + 1 for block in own)
            lines.append(f"[вложение не вошло в лимит задания: {total} строк]")
            parts.append("\n".join(lines))
            continue
        body: list[str] = []
        skipped: list[_Block] = []
        for block in own:
            if block.position in chosen:
                if skipped:
                    body.append(_skipped_marker(skipped))
                    skipped = []
                body.append(block.text)
            else:
                skipped.append(block)
        if skipped:
            body.append(_skipped_marker(skipped))
        lines.append("\n\n".join(body))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _skipped_marker(skipped: list[_Block]) -> str:
    lines = sum(block.text.count("\n") + 1 for block in skipped)
    return f"[… пропущено блоков: {len(skipped)}, строк: {lines} …]"
