"""Отбор логов при выгрузке и под лимит задания по связи с ошибкой.

``log_snippet`` теста — секции ``--- [<тип>: <имя>] ---`` из разных вложений;
внутри секции блоки разделены пустой строкой (``[ERROR]``-строка вместе со
своим стек-трейсом, HTTP-сигналы, JSON-журнал). Если всё не помещается в
лимит, лог не режется по началу: блоки ранжируются по пересечению с текстом
ошибки (ID запросов весят больше слов), а оставшееся место занимают самые
ранние блоки — первопричина часто не имеет общих слов с assertion. Порядок
событий сохраняется, пропуски и не вошедшие вложения помечаются.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from alla_core.utils.log_events import SOURCE_MARK_RE

FOCUS_NOTE = "[лог сокращён: отобраны блоки по связи с ошибкой, пропуски помечены]"
CONTEXT_LINES = 2
ID_WEIGHT = 3.0
ERROR_HINT_BONUS = 0.5
MIN_FRAGMENT_CHARS = 80
LINE_HEAD_CHARS = 120

_SECTION_HEADER_RE = re.compile(r"^--- \[[^\]:\s][^\]:]*?: .+?\] ---$", re.MULTILINE)
_BLOCK_SPLIT_RE = re.compile(r"\n[ \t]*\n")
_EXTRACTION_MARKER_RE = re.compile(
    r"^\[\.\.\. обрезано: было \d+ символов, оставлено \d+ \.\.\.\]$"
)
_SKIPPED_MARKER_RE = re.compile(r"^\[… пропущено блоков: (?:\d+|\?), строк: (?:\d+|\?) …\]$")
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
    lines: int | None
    gap: bool = False
    header: str | None = None
    source_position: int = 0
    source_total: int = 0

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


def selection_error_text(message: str | None, trace: str | None, correlation: str | None) -> str:
    """Общий контекст отбора: сообщение, первые 20 строк трейса, известная корреляция."""
    parts = [message or "", "\n".join((trace or "").splitlines()[:20]), correlation or ""]
    return "\n".join(part for part in parts if part)


def _is_gap_marker(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped == "[…]" or _SKIPPED_MARKER_RE.fullmatch(stripped))


def _take_extraction_footer(snippet: str) -> tuple[str, str | None]:
    """Только последняя непустая строка может быть итоговой пометкой ядра."""
    lines = snippet.splitlines(keepends=True)
    index = len(lines) - 1
    while index >= 0 and not lines[index].strip():
        index -= 1
    if index >= 0 and _EXTRACTION_MARKER_RE.fullmatch(lines[index].strip()):
        footer = lines.pop(index).strip()
        return "".join(lines), footer
    return snippet, None


def strip_log_selection_metadata(snippet: str) -> str:
    """Убрать standalone-пометки только из заведомо сокращённого ядром лога."""
    snippet, _footer = _take_extraction_footer(snippet)
    return "".join(line for line in snippet.splitlines(keepends=True) if not _is_gap_marker(line))


def focus_log(
    snippet: str, error_text: str, budget: int, *, log_selection_truncated: bool = False,
) -> str:
    """Вернуть лог не длиннее ``budget`` символов (короткий — без изменений)."""
    if len(snippet) <= budget:
        return snippet
    tokens = error_tokens(error_text)
    footer = None
    if log_selection_truncated:
        snippet, footer = _take_extraction_footer(snippet)
        snippet = snippet.rstrip("\n")
    sections = _split_sections(snippet)
    blocks: list[_Block] = []
    originals: dict[int, str] = {}
    for section_index, (_header, texts) in enumerate(sections):
        for original in texts:
            # Старые пропуски — метаданные, а не важные блоки. Они объединяются
            # с новыми пропусками при render; единственная закрепляемая пометка — footer.
            if log_selection_truncated:
                pieces = re.split(r"(?m)^(\[…\]|\[… пропущено блоков: (?:\d+|\?), строк: (?:\d+|\?) …\])[ \t]*$", original)
            else:
                pieces = [original]
            for piece in pieces:
                if not piece.strip():
                    continue
                position = len(blocks)
                if log_selection_truncated and _is_gap_marker(piece):
                    blocks.append(_Block(section_index, position, "", 0.0, None, gap=True))
                    continue
                originals[position] = piece
                text = piece.strip("\n")
                if len(text) > budget // 2:
                    text = _shrink_block(text, tokens, budget // 2)
                blocks.append(_Block(section_index, position, text, _block_score(piece, tokens),
                                     piece.count("\n") + 1))

    priority = sorted((b for b in blocks if not b.gap), key=lambda b: (-b.score, b.position))
    chosen: list[_Block] = []
    available = budget - _overhead(sections) - (len(footer) + 2 if footer else 0)
    for block in priority:
        if block.cost <= available:
            chosen.append(block)
            available -= block.cost

    result = _render(sections, blocks, {b.position for b in chosen}, footer=footer)
    while len(result) > budget and chosen:
        chosen.pop()  # сначала уходят наименее важные
        result = _render(sections, blocks, {b.position for b in chosen}, footer=footer)
    if not chosen and priority:
        # Лимит меньше заголовков и маркеров: лучше фрагмент самого важного
        # блока (из исходного текста), чем одни пометки о пропусках. Если
        # пояснение съест почти весь лимит — без него: обрезку и так видно по «…».
        best = originals[priority[0].position].strip("\n")
        # Footer не должен вытеснить весь полезный текст. Tiny-budget fallback
        # может опустить заголовки промпта, но всегда выбирает настоящий блок.
        footer_suffix = f"\n\n{footer}" if footer else ""
        room = budget - len(FOCUS_NOTE) - 2 - len(footer_suffix)
        if room >= MIN_FRAGMENT_CHARS:
            return f"{FOCUS_NOTE}\n\n{_marked_fragment(best, tokens, room)}{footer_suffix}"
        room = budget - len(footer_suffix)
        if footer and room >= MIN_FRAGMENT_CHARS:
            return _marked_fragment(best, tokens, room) + footer_suffix
        return _marked_fragment(best, tokens, budget)
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


def _split_source_mark(text: str) -> tuple[str | None, str]:
    """Пометка строк источника в начале блока и остальной текст."""
    first, newline, rest = text.partition("\n")
    if newline and SOURCE_MARK_RE.fullmatch(first):
        return first, rest
    return None, text


def _with_mark(text: str, limit: int, shrink: Callable[[str, int], str]) -> str:
    """Сократить блок, сохранив пометку источника целиком или не оставив от неё ничего.

    Обрезанная пометка перестаёт распознаваться и попала бы в материал сигнатуры и
    признак базы знаний как строка лога.
    """
    mark, body = _split_source_mark(text)
    if mark is None:
        return shrink(text, limit)
    room = limit - len(mark) - 1
    if room < MIN_FRAGMENT_CHARS:
        return shrink(body, limit)
    return f"{mark}\n{shrink(body, room)}"


def _shrink_block(text: str, tokens: dict[str, float], limit: int) -> str:
    """Сократить огромный блок; пометка источника сохраняется целиком (или убирается)."""
    return _with_mark(text, limit, lambda body, room: _shrink_lines(body, tokens, room))


def _marked_fragment(text: str, tokens: dict[str, float], room: int) -> str:
    return _with_mark(text, room, lambda body, size: _fragment(body, tokens, size))


def _shrink_lines(text: str, tokens: dict[str, float], limit: int) -> str:
    """Сократить огромный блок до значимых строк и их контекста.

    Значимые строки — совпадающие с текстом ошибки; если таких нет (assertion
    не пересекается с логом) — строки с явными ошибками приложения; если нет
    и их — начало блока, сколько поместится.
    """
    if limit <= 4:
        return text[:max(0, limit - 1)] + ("…" if limit > 0 else "")
    lines = text.split("\n")
    anchors = [i for i, line in enumerate(lines) if _score(line, tokens) > 0]
    if not anchors:
        anchors = [i for i, line in enumerate(lines) if _ERROR_HINT_RE.search(line)]
    signaled = bool(anchors)
    if not anchors:
        anchors = list(range(len(lines)))  # начало блока, дальше отрежет лимит
    # Последующие signal lines получают место даже когда первая строка сама
    # является anchor с огромным payload. Для длинных строк резервируем fragment.
    signal_costs = [0] * (len(anchors) + 1) if signaled else []
    if signaled:
        for offset in range(len(anchors) - 1, -1, -1):
            signal_costs[offset] = min(limit // 2, signal_costs[offset + 1]
                                       + min(len(lines[anchors[offset]]), MIN_FRAGMENT_CHARS) + 6)
    anchor_cursor = 0
    keep = {0}
    for index in anchors:
        keep.update(range(index - CONTEXT_LINES, index + CONTEXT_LINES + 1))
    kept: list[str] = []
    previous = -1
    size = 0
    for index in sorted(i for i in keep if 0 <= i < len(lines)):
        while anchor_cursor < len(anchors) and anchors[anchor_cursor] <= index:
            anchor_cursor += 1
        prefix = "[…]\n" if index != previous + 1 else ""
        piece = lines[index]
        room = limit - 4 - size - len(prefix) - 1
        later_signal = signaled and anchor_cursor < len(anchors)
        # Один доступный бюджет и для full append, и для fragment: даже короткий
        # context не может потратить зарезервированное место следующих anchors.
        reserve = signal_costs[anchor_cursor] + 7 if later_signal else 0
        fragment_room = max(0, room - reserve)
        before_first = signaled and index < anchors[0]
        if before_first:
            fragment_room = min(LINE_HEAD_CHARS, fragment_room)
        if len(piece) > fragment_room:
            # Строка целиком не помещается (например, [ERROR] с огромным
            # payload) — оставляем её значимый фрагмент, а не теряем совсем.
            # Первую значимую строку сохраняем при любом месте, остальные —
            # только если фрагмент выйдет осмысленной длины.
            if (fragment_room >= MIN_FRAGMENT_CHARS
                    or ((not kept or before_first) and fragment_room > 0)):
                fragment = _fragment(piece, tokens, fragment_room)
                kept.append(prefix + fragment)
                size += len(prefix) + len(fragment) + 1
                previous = index
            if later_signal:
                continue
            break
        kept.append(prefix + piece)
        size += len(prefix) + len(piece) + 1
        previous = index
    if previous < len(lines) - 1:
        kept.append("[…]")
    return "\n".join(kept)


def _fragment(text: str, tokens: dict[str, float], room: int) -> str:
    """Фрагмент длинного текста не длиннее ``room`` с пометками обрезки «…».

    Берётся окно вокруг первого совпадения с ошибкой (или явной ошибки
    приложения); начало строки — время, уровень, логгер — сохраняется.
    """
    if room <= 0:
        return ""
    if len(text) <= room:
        return text
    if room <= 4:
        return text[:room - 1] + "…"
    lowered = text.lower()
    hits = [lowered.find(token) for token in tokens if token in lowered]
    hint = _ERROR_HINT_RE.search(text)
    anchor = min(hits) if hits else (hint.start() if hint else 0)
    width = room - 1  # место под «…» в конце
    head = min(LINE_HEAD_CHARS, width // 4)
    start = max(0, anchor - width // 4)
    if start <= head:
        return text[:width] + "…"
    body = width - head - 3  # « … » между началом и окном
    start = min(start, len(text) - body)
    end = start + body
    return text[:head] + " … " + text[start:end] + ("…" if end < len(text) else "")


def _overhead(sections: list[tuple[str | None, list[str]]]) -> int:
    headers = sum(len(header) + 2 for header, _ in sections if header)
    return len(FOCUS_NOTE) + 2 + headers + 60 * len(sections)


def _render(
    sections: list[tuple[str | None, list[str]]],
    blocks: list[_Block],
    chosen: set[int],
    *,
    show_note: bool = True,
    footer: str | None = None,
) -> str:
    parts = [FOCUS_NOTE] if show_note else []
    by_section: dict[int, list[_Block]] = {}
    for block in blocks:
        by_section.setdefault(block.section, []).append(block)
    for section_index, (header, _texts) in enumerate(sections):
        own = by_section.get(section_index, [])
        lines: list[str] = [header] if header else []
        if not any(block.position in chosen for block in own):
            total = "?" if any(block.lines is None for block in own) else sum(block.lines or 0 for block in own)
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
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)


def _skipped_marker(skipped: list[_Block]) -> str:
    unknown = any(block.gap for block in skipped)
    lines = "?" if unknown else sum(block.lines or 0 for block in skipped)
    count = "?" if unknown else len(skipped)
    return f"[… пропущено блоков: {count}, строк: {lines} …]"


@dataclass
class _RetainedSource:
    header: str
    total: int
    positions: set[int] = field(default_factory=set)
    text_chars: int = 0
    gaps: int = 1  # изначально пропущена вся секция

    @property
    def size(self) -> int:
        units = len(self.positions) + self.gaps
        return (len(self.header) + 1 + self.text_chars
                + self.gaps * len("[… пропущено блоков: ?, строк: ? …]") + 2 * (units - 1))

    def update(self, block: _Block, *, add: bool) -> None:
        position = block.source_position
        left = position == 0 or position - 1 in self.positions
        right = position == self.total - 1 or position + 1 in self.positions
        gap_delta = -1 if left and right else (1 if not left and not right else 0)
        if add:
            self.positions.add(position)
            self.text_chars += len(block.text)
            self.gaps += gap_delta
        else:
            self.positions.remove(position)
            self.text_chars -= len(block.text)
            self.gaps -= gap_delta


class StreamingLogSelector:
    """Общий bounded бюджет вложений; до первого overflow — буквальная склейка.

    После overflow сохраняются только display-блоки, исходные score и ordinal.
    Отброшенные блоки не возвращаются: это потоковый отбор, не offline optimum.
    """

    def __init__(self, error_text: str, budget: int | None) -> None:
        self.budget = budget
        self.tokens = error_tokens(error_text)
        self.truncated = False
        self._literal: list[tuple[str, str]] = []
        self._literal_chars = 0
        self._blocks: dict[int, _Block] = {}
        self._priority: list[tuple[float, int]] = []
        self._sources: dict[int, _RetainedSource] = {}
        self._body_chars = 0
        self._section = 0
        self._position = 0
        self.original_chars = 0

    @property
    def retained_chars(self) -> int:
        if not self.truncated:
            return self._literal_chars
        return sum(source.text_chars + len(source.header) for source in self._sources.values())

    def add_section(self, header: str, body: str) -> None:
        size = len(header) + 1 + len(body)
        sep = 2 if self._section else 0
        self.original_chars += sep + size
        source = self._section
        self._section += 1
        if not self.truncated and (self.budget is None or self._literal_chars + sep + size <= self.budget):
            self._literal.append((header, body))
            self._literal_chars += sep + size
            return
        if not self.truncated:
            self.truncated = True
            previous = self._literal
            self._literal = []
            self._literal_chars = 0
            for index, (old_header, old_body) in enumerate(previous):
                self._offer_section(index, old_header, old_body)
        self._offer_section(source, header, body)

    def _offer_section(self, source: int, header: str, body: str) -> None:
        assert self.budget is not None
        texts = _split_blocks(body)
        for source_position, raw in enumerate(texts):
            text = _shrink_block(raw, self.tokens, self.budget // 2) if len(raw) > self.budget // 2 else raw
            block = _Block(source, self._position, text, _block_score(raw, self.tokens),
                           raw.count("\n") + 1, header=header,
                           source_position=source_position, source_total=len(texts))
            self._position += 1
            self._blocks[block.position] = block
            heapq.heappush(self._priority, (block.score, -block.position))
            self._update_source(block, add=True)
            while self._blocks and self._render_size() > self.budget:
                _score, negative_position = heapq.heappop(self._priority)
                removed = self._blocks.pop(-negative_position)
                self._update_source(removed, add=False)

    def _update_source(self, block: _Block, *, add: bool) -> None:
        source = self._sources.get(block.section)
        if source is None:
            assert add and block.header is not None
            source = _RetainedSource(block.header, block.source_total)
            self._sources[block.section] = source
        else:
            self._body_chars -= source.size
        source.update(block, add=add)
        if source.positions:
            self._body_chars += source.size
        else:
            del self._sources[block.section]

    def _render_size(self) -> int:
        if not self._blocks:
            return 0
        return self._body_chars + 2 * (len(self._sources) - 1) + 2 + len(self._footer())

    def _footer(self) -> str:
        return f"[... обрезано: было {self.original_chars} символов, оставлено {self.budget} ...]"

    def _render_retained(self) -> str:
        if not self._blocks:
            return ""
        sections: list[tuple[str | None, list[str]]] = []
        display: list[_Block] = []
        own: dict[int, list[_Block]] = {}
        for block in sorted(self._blocks.values(), key=lambda b: b.position):
            own.setdefault(block.section, []).append(block)
        for blocks in own.values():
            section_index = len(sections)
            sections.append((blocks[0].header, []))
            previous = -1
            for block in blocks:
                if block.source_position != previous + 1:
                    display.append(_Block(section_index, -1, "", 0, None, gap=True))
                display.append(replace(block, section=section_index))
                previous = block.source_position
            if previous < blocks[-1].source_total - 1:
                display.append(_Block(section_index, -1, "", 0, None, gap=True))
        return _render(sections, display, set(self._blocks),
                       show_note=False, footer=self._footer())

    def render(self) -> str:
        if not self.truncated:
            return "\n\n".join(f"{header}\n{body}" for header, body in self._literal)
        return self._render_retained()
