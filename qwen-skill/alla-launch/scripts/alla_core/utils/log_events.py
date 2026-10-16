"""События текстового лога: где запись начинается, где кончается и ошибка ли она.

Лог делится на события. Событие начинается строкой с признаком новой записи и
продолжается всеми строками без такого признака (кадры стека, ``Caused by``,
``... N more``, строки с отступом, многострочное сообщение). Поэтому соседняя
INFO-запись со своим временем не поглощается исключением.

Признаки начала записи:

* время в начале строки (возможно, в ``[]``): ISO с ``T`` или пробелом,
  ``dd.MM.yyyy``, ``yyyy/MM/dd``, ``HH:mm:ss.SSS``, syslog ``Oct 03 10:00:00``,
  JUL ``Oct 03, 2026 10:00:00 AM``; logfmt ``time=``/``ts=``;
* заголовок klog/glog ``E1003 10:00:00.123456 12 file.go:42]`` — уровень в первой
  букве (``I``/``W``/``E``/``F``);
* Go ``panic: …`` и ``fatal error: …`` с начала строки (уровни ``PANIC`` и
  ``FATAL``): дамп goroutine, кадры и пустые строки — продолжение до следующей
  записи;
* Java ``Exception in thread "…" pkg.SomeException…`` — исключение без времени;
* JSON-запись строкой (NDJSON: zap, zerolog, slog, logrus, logstash-logback-encoder,
  ECS, JsonTemplateLayout), возможно с отступом, — объект с полем уровня ``level``/``lvl``/``severity``/
  ``log.level`` (или ``{"log": {"level": …}}``); JSON без поля уровня (тело ответа
  после ERROR) остаётся продолжением;
* Python ``ERROR:root:…`` (``LEVEL:`` без пробела после двоеточия);
* ``Traceback (most recent call last):`` и строка исключения
  (``pkg.SomeException: …``), за которой идут кадры ``at …`` — если идущее
  событие не ошибка (иначе это продолжение её стека).

Уровень берётся только из позиции уровня: первые слова после времени
(``ERROR``, ``[ERROR]``, ``|ERROR|``, ``ERROR/Worker``…; в скобках регистр
любой, без скобок — только заглавные; группа в ``[]`` из нескольких слов, как
MDC ``[traceId=…, spanId=…]``, — одно место), ключ ``level=`` в logfmt или поле
уровня JSON (строка в любом регистре; целое под ``level`` от −1 до 5 — шкала
zerolog, 3 и выше — ошибка). Слово
``ERROR`` в тексте INFO-сообщения ошибкой не считается: первым найденный
уровень и решает. JUL склеивается: строка «дата + класс + метод» и следующая
``SEVERE: …`` — одно событие. Traceback Python заканчивается строкой
исключения.

Запись любого уровня с полями ошибки ответа — тоже ошибка (:func:`has_error_fields`):
``[INFO] … {"errorCode":6, "errorMessage":"счет закрыт"}``, ``errorCode=6``,
``<faultString>…</faultString>``. Поле — имя на ``error``/``err``/``fault`` + ``code``/``message``/
``msg``/``text``/``description``/``string`` (``ufsErrorCode``, ``error_message``); значение «успеха»
(пусто, ``null``, ``0``, ``OK``…) ошибкой не делает. Продолжение стека решает по-прежнему
только уровень (:attr:`LogEvent.is_error`).

JSON-ошибка показывается строкой записи; многострочные поля стека (``stack_trace``,
``stacktrace``, ``error.stack_trace``, ``exception``…) вынимаются из неё и идут
строками ниже, а заголовком становится запись без них. Многострочное сообщение
остаётся в заголовке.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from alla_core.utils.text_normalization import repeat_key

ERROR_LEVELS = frozenset({
    "ERROR", "ERR", "FATAL", "SEVERE", "CRITICAL", "CRIT", "ALERT", "EMERG", "EMERGENCY",
    "PANIC", "DPANIC",  # logrus, zap, zerolog
})
_LEVELS = ERROR_LEVELS | frozenset({
    "WARN", "WARNING", "INFO", "DEBUG", "TRACE", "NOTICE", "FINE", "FINER", "FINEST", "CONFIG",
})
_LEVEL_SCAN_TOKENS = 6

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_TIMESTAMP_RE = re.compile(
    r"^\[?(?:"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"          # 2026-10-03T10:00:00, 2026-10-03 10:00:00
    r"|\d{2}\.\d{2}\.\d{4}[T ]\d{2}:\d{2}:\d{2}"      # 03.10.2026 10:00:00
    r"|\d{4}/\d{2}/\d{2}[T ]\d{2}:\d{2}:\d{2}"        # 2026/10/03 10:00:00 (nginx)
    r"|" + _MONTH + r" \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} [AP]M"  # JUL
    r"|" + _MONTH + r"\s+\d{1,2} \d{2}:\d{2}:\d{2}"    # syslog
    r"|\d{2}:\d{2}:\d{2}[.,]\d{3}"                    # logback: 10:00:00.123
    r")"
)
_LOGFMT_START_RE = re.compile(r"^(?:time|ts|timestamp|t)=\S")
# Пара logfmt: значение в кавычках (с экранированием) читается целиком, поэтому
# «level=…» внутри msg="…" за поле уровня не принимается.
_LOGFMT_PAIR_RE = re.compile(r'(?:^|\s)(?P<key>[\w.-]+)=(?P<value>"(?:[^"\\]|\\.)*"|[^\s"]*)')
_LOGFMT_LEVEL_KEYS = frozenset({"level", "lvl", "severity"})
_KLOG_RE = re.compile(r"^(?P<level>[IWEF])\d{4} \d{2}:\d{2}:\d{2}\.\d{6}\s+\d+ [^\s\]]+:\d+\] ")
_KLOG_LEVELS = {"I": "INFO", "W": "WARNING", "E": "ERROR", "F": "FATAL"}
_GO_CRASH_RE = re.compile(r"^(?P<crash>panic|fatal error): \S")
_GO_CRASH_LEVELS = {"panic": "PANIC", "fatal error": "FATAL"}
_UNCAUGHT_RE = re.compile(r'^Exception in thread "[^"]*" [\w.$]+')
_JSON_LEVEL_KEYS = ("level", "lvl", "severity", "log.level")
# Поля стека: последняя часть имени (``error.stack_trace`` → ``stack_trace``). Многострочное
# значение такого поля идёт строками под записью; остальные (сообщение) остаются в заголовке.
_JSON_STACK_KEYS = frozenset({"stack_trace", "stacktrace", "stack", "exception",
                              "extendedstacktrace"})
_ZEROLOG_LEVELS = {-1: "TRACE", 0: "DEBUG", 1: "INFO", 2: "WARN", 3: "ERROR", 4: "FATAL",
                   5: "PANIC"}
_PYTHON_LEVEL_RE = re.compile(r"^(?P<level>[A-Z]+):(?=\S)")
_JUL_LEVEL_RE = re.compile(r"^(?P<level>[A-Z]+): ")
_TRACEBACK_RE = re.compile(r"^Traceback \(most recent call last\):")
_EXCEPTION_RE = re.compile(r"^[\w.$]+(?:Exception|Error|Throwable)(?::\s.*)?$")
_FRAME_RE = re.compile(r"^\s*at\s+\S")
_TOKEN_STRIP = "[]|:,;()<>"
# Поля ошибки ответа: JSON (``"errorCode":6``), ``key=value`` и ``key: value``, XML.
_ERROR_FIELD_KEY = (r"[A-Za-z_]*?(?:error|err|fault)_?"
                    r"(?:code|message|msg|text|description|desc|string)")
_ERROR_FIELD_RE = re.compile(
    rf'(?<![\w$])"?(?P<key>{_ERROR_FIELD_KEY})"?\s*[:=]\s*'
    r'(?P<value>"(?:[^"\\]|\\.)*"|[^\s,;(){}\[\]"<]*)',
    re.IGNORECASE,
)
_ERROR_FIELD_XML_RE = re.compile(
    rf"<(?:[\w-]+:)?(?P<key>{_ERROR_FIELD_KEY})>(?P<value>[^<]*)</", re.IGNORECASE)
# Ответ внутри строкового поля JSON-записи: ``"message":"Response {\\"errorCode\\":6}"``.
_ESCAPED_QUOTE_RE = re.compile(r'\\+"')
_SUCCESS_VALUES = frozenset({"", "null", "none", "nil", "ok", "success", "false"})
_ZEROS_RE = re.compile(r"0+(?:\.0+)?")


@dataclass
class LogEvent:
    """Событие лога; номера строк — с 1, по ``str.splitlines``."""

    first_line: int
    level: str | None
    kind: str  # level | traceback | exception | plain
    lines: list[str] = field(default_factory=list)
    closed: bool = False  # Traceback дошёл до строки исключения
    level_line: int = 0  # индекс строки уровня в ``lines``: у JUL это вторая строка
    # Показ первой строки, если он не дословный: JSON-запись со стеком — заголовок и
    # строки многострочных полей. Пометка строк у них — строка записи.
    rendered: list[str] = field(default_factory=list)

    @property
    def last_line(self) -> int:
        return self.first_line + len(self.lines) - 1

    @property
    def is_error(self) -> bool:
        return self.level in ERROR_LEVELS or self.kind in ("traceback", "exception")

    @property
    def reports_error(self) -> bool:
        """Событие-ошибка: уровень, Traceback, исключение или поля ошибки ответа."""
        return self.is_error or has_error_fields(self.text)

    @property
    def shown(self) -> list[str]:
        """Строки показа: ``lines``, где первая заменена на ``rendered``, если он есть."""
        return [*self.rendered, *self.lines[1:]] if self.rendered else self.lines

    @property
    def text(self) -> str:
        return "\n".join(self.shown)

    def source_offset(self, index: int) -> int:
        """Смещение в ``lines`` строки показа ``index``."""
        if not self.rendered:
            return index
        return max(0, index - len(self.rendered) + 1)


def _error_fields(text: str) -> Iterator[tuple[str, str]]:
    """Поля ошибки ответа ``(имя, значение)`` с непустым значением, а не «успехом»."""
    lowered = text.lower()
    if "err" not in lowered and "fault" not in lowered:
        return
    # Экранированные кавычки ответа внутри строки JSON-записи — как обычные: иначе
    # ``\\"errorCode\\":6`` не читается как поле.
    text = _ESCAPED_QUOTE_RE.sub('"', text)
    for regex in (_ERROR_FIELD_RE, _ERROR_FIELD_XML_RE):
        for match in regex.finditer(text):
            value = match.group("value")
            if value.startswith('"'):
                value = value[1:-1]
            value = value.strip()
            if value.lower() not in _SUCCESS_VALUES and not _ZEROS_RE.fullmatch(value):
                yield match.group("key"), value


def has_error_fields(text: str) -> bool:
    """В тексте есть поле ошибки ответа с непустым значением, а не «успех».

    ``"errorCode":6``, ``errorMessage=счет закрыт``, ``<faultCode>SOAP-ENV:Server</faultCode>`` —
    да; ``"errorCode":0``, ``"errorMessage":null``, ``errorText=""``, ``errorCount=3`` — нет.
    """
    return next(_error_fields(text), None) is not None


def error_field_codes(text: str) -> list[str]:
    """Коды ошибки ответа по порядку, без повторов: ``errorcode=6``, ``faultcode=soap-env:server``.

    Только поля-коды (имя на ``code``), не сообщения: код называет ошибку, а в тексте
    сообщения бывают номера счетов и заказов. Имя — без регистра и ``_`` (``error_code`` и
    ``errorCode`` — одно поле), значение — без регистра. Короткие коды (``6``) тоже: в отличие
    от :func:`~alla_core.utils.text_normalization.numeric_codes`, имя поля уже говорит, что это
    код ошибки.
    """
    codes = [
        f"{key.casefold().replace('_', '')}={value.casefold()}"
        for key, value in _error_fields(text)
        if key.casefold().endswith("code")
    ]
    return list(dict.fromkeys(codes))


def _level_after_timestamp(rest: str) -> str | None:
    """Первый уровень среди первых слов после времени; дальше — уже сообщение.

    Группа в ``[]`` из нескольких слов (MDC, поток с пробелами) занимает одно место:
    проверяется её первое слово (``[INFO ]``), остальные пропускаются.
    """
    places = 0
    in_group = False
    for token in rest.split():
        if in_group:
            in_group = not token.endswith("]")
            continue
        if places == _LEVEL_SCAN_TOKENS:
            break
        places += 1
        in_group = token.startswith("[") and "]" not in token
        decorated = token != token.strip(_TOKEN_STRIP) or "/" in token
        word = token.strip(_TOKEN_STRIP).split("/", 1)[0]
        if not word:
            continue
        if word.upper() in _LEVELS and (decorated or word.isupper()):
            return word.upper()
    return None


def _logfmt_level(line: str) -> str | None:
    """Значение первого самостоятельного поля ``level``/``lvl``/``severity``."""
    for pair in _LOGFMT_PAIR_RE.finditer(line):
        if pair.group("key").lower() in _LOGFMT_LEVEL_KEYS:
            return pair.group("value").strip('"').upper() or None
    return None


def _json_level(record: dict[str, Any]) -> tuple[bool, str | None]:
    """Есть ли у JSON-объекта поле уровня и какой уровень (``None`` — неизвестный)."""
    nested = record.get("log")
    for key in _JSON_LEVEL_KEYS:
        if key in record:
            value = record[key]
            break
    else:
        if not isinstance(nested, dict) or "level" not in nested:
            return False, None
        key, value = "log.level", nested["level"]
    if isinstance(value, str):
        level = value.upper()
        return True, level if level in _LEVELS else None
    if key == "level" and isinstance(value, int) and not isinstance(value, bool):
        return True, _ZEROLOG_LEVELS.get(value)
    return True, None


def _without_stacks(record: dict[str, Any], blocks: list[str]) -> dict[str, Any]:
    """Запись без многострочных полей стека (на любой глубине); их значения — в ``blocks``.

    Многострочное сообщение остаётся в заголовке: оно называет ошибку, а строки под записью
    сигнатура берёт, только если в них есть признак ошибки.
    """
    kept: dict[str, Any] = {}
    for key, value in record.items():
        name = str(key).rsplit(".", 1)[-1].lower()
        if isinstance(value, str) and "\n" in value and name in _JSON_STACK_KEYS:
            blocks.append(value)
        elif isinstance(value, dict):
            kept[key] = _without_stacks(value, blocks)
        else:
            kept[key] = value
    return kept


def _json_record(line: str) -> tuple[str | None, list[str]] | None:
    """JSON-запись лога в строке: уровень и показ (пустой — строка дословно); иначе ``None``."""
    # Отступ перед записью бывает (вывод через префикс, выравнивание); показ — исходная строка.
    if not line.lstrip(" \t").startswith("{"):
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    has_level, level = _json_level(record)
    if not has_level:
        return None
    blocks: list[str] = []
    header = _without_stacks(record, blocks)
    if not blocks:
        return level, []
    rendered = [json.dumps(header, ensure_ascii=False)]
    rendered += [part.rstrip() for block in blocks for part in block.splitlines() if part.strip()]
    return level, rendered


def _start(line: str) -> tuple[str | None, str | None]:
    """Вид записи, которую начинает строка (``None`` — продолжение), и её уровень."""
    match = _TIMESTAMP_RE.match(line)
    if match:
        return "level", _level_after_timestamp(line[match.end():])
    if _LOGFMT_START_RE.match(line):
        return "level", _logfmt_level(line)
    klog = _KLOG_RE.match(line)
    if klog:
        return "level", _KLOG_LEVELS[klog.group("level")]
    crash = _GO_CRASH_RE.match(line)
    if crash:
        return "level", _GO_CRASH_LEVELS[crash.group("crash")]
    if _UNCAUGHT_RE.match(line):
        return "exception", None
    python = _PYTHON_LEVEL_RE.match(line)
    if python and python.group("level") in _LEVELS:
        return "level", python.group("level")
    return None, None


def iter_events(text: str) -> Iterator[LogEvent]:
    """События лога по порядку; каждая строка принадлежит ровно одному событию."""
    lines = text.splitlines()
    current: LogEvent | None = None
    for index, line in enumerate(lines):
        number = index + 1
        started, level = _start(line)
        rendered: list[str] = []
        if started is None and (record := _json_record(line)) is not None:
            started = "level"
            level, rendered = record
        if started is not None:
            if current is not None:
                yield current
            current = LogEvent(number, level, started, [line], rendered=rendered)
            continue
        in_error = current is not None and current.is_error and not current.closed
        kind = None
        if not in_error and _TRACEBACK_RE.match(line):
            kind = "traceback"
        elif (not in_error and _EXCEPTION_RE.match(line)
                and index + 1 < len(lines) and _FRAME_RE.match(lines[index + 1])):
            kind = "exception"
        elif current is None or current.closed:
            kind = "plain"
        if kind is not None:
            if current is not None:
                yield current
            current = LogEvent(number, None, kind, [line])
            continue
        assert current is not None
        jul = _JUL_LEVEL_RE.match(line)
        if (jul and jul.group("level") in _LEVELS and current.kind == "level"
                and current.level is None and len(current.lines) == 1
                and _TIMESTAMP_RE.match(current.lines[0])):
            current.lines.append(line)  # JUL: «дата класс метод» + «SEVERE: …»
            current.level = jul.group("level")
            current.level_line = 1
            continue
        current.lines.append(line)
        if current.kind == "traceback" and line.strip() and not line[0].isspace():
            current.closed = True  # строка исключения завершает Traceback
    if current is not None:
        yield current


def parse_events(text: str) -> list[LogEvent]:
    return list(iter_events(text))


def error_events(text: str) -> Iterator[LogEvent]:
    """События-ошибки: уровень ошибки, Traceback, исключение со стеком или запись любого
    уровня с полями ошибки ответа (:func:`has_error_fields`)."""
    return (event for event in iter_events(text) if event.reports_error)


# ---------------------------------------------------------------------------
# Происхождение фрагментов: пометка строк первой строкой блока
# ---------------------------------------------------------------------------

MAX_LISTED_PLACES = 5
# «[строки 120–134]», «[строка 7]», с повторами — «[строки 3–5 · повторялось 7 раз: …]».
# Метаданные, а не лог: из материала сигнатуры, признака базы знаний и лог-канала
# кластеризации пометки вырезаются (strip_source_marks).
SOURCE_MARK_RE = re.compile(r"^\[строк[аи] \d+(?:–\d+)?(?: · повторял[а-я]* [^\]\n]*)?\]$",
                            re.MULTILINE)


def _span(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}–{last}"


def _times(count: int) -> str:
    return "раза" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "раз"


def source_mark(lines: tuple[int, int], repeats: list[tuple[int, int]] | None = None) -> str:
    """Пометка источника: строки фрагмента и, если событие повторялось, места повторов."""
    first, last = lines
    mark = f"[{'строка' if first == last else 'строки'} {_span(first, last)}"
    if repeats and len(repeats) > 1:
        listed = ", ".join(_span(a, b) for a, b in repeats[:MAX_LISTED_PLACES])
        rest = len(repeats) - MAX_LISTED_PLACES
        mark += (f" · повторялось {len(repeats)} {_times(len(repeats))}: {listed}"
                 + (f" и ещё {rest}" if rest > 0 else ""))
    return mark + "]"


def strip_source_marks(text: str) -> str:
    """Убрать пометки строк вместе с их переводом строки."""
    if "[строк" not in text:
        return text
    return SOURCE_MARK_RE.sub("\x00", text).replace("\x00\n", "").replace("\x00", "")


def _is_blank(line: str) -> bool:
    # Как разделитель блоков в log_focus: пустая строка или только пробелы и табы.
    return not line.strip(" \t")


def _paragraphs(lines: list[str]) -> list[tuple[int, int]]:
    """Смещения непустых участков события: отбор лога делит блоки по пустым строкам."""
    result: list[tuple[int, int]] = []
    start: int | None = None
    for offset, line in enumerate(lines):
        if _is_blank(line):
            if start is not None:
                result.append((start, offset - 1))
                start = None
        elif start is None:
            start = offset
    if start is not None:
        result.append((start, len(lines) - 1))
    return result


def _event_span(event: LogEvent) -> tuple[int, int]:
    paragraphs = _paragraphs(event.lines)
    end = paragraphs[-1][1] if paragraphs else 0
    return event.first_line, event.first_line + end


def _marked_event(event: LogEvent, repeats: list[tuple[int, int]]) -> str:
    """Текст события; каждый его абзац начинается своей пометкой строк.

    У первого абзаца — ещё и места повторов. Блок, отобранный без соседних абзацев,
    так всё равно знает свои строки.
    """
    shown = event.shown
    starts = {start: (index, end) for index, (start, end) in enumerate(_paragraphs(shown))}
    out: list[str] = []
    for offset, line in enumerate(shown):
        if offset in starts:
            index, end = starts[offset]
            span = (event.first_line + event.source_offset(offset),
                    event.first_line + event.source_offset(end))
            out.append(source_mark(span, repeats if index == 0 else None))
        out.append(line)
    return "\n".join(out)


def is_log_text(text: str) -> bool:
    """Текст — лог: есть хоть одна запись, которую распознаёт :func:`iter_events` (время,
    уровень, klog…), Traceback или исключение со стеком — вывод stderr бывает без времени.

    Запрос и ответ HTTP, JSON-документ, XML — не лог: id операций в них — свои id теста.
    """
    return any(event.kind != "plain" for event in iter_events(text))


def render_error_blocks(text: str) -> str:
    """Ошибки лога блоками через пустую строку; каждый абзац — с пометкой строк.

    Точные повторы (одинаковые после замены чисел, времени, UUID, но с теми же
    кодами ошибок — :func:`numeric_codes`) сворачиваются в первое вхождение с
    перечнем мест.
    """
    return render_event_blocks(error_events(text))


def render_event_blocks(events: Iterable[LogEvent]) -> str:
    """Блоки :func:`render_error_blocks` из уже отобранных событий-ошибок.

    Отбор по операции теста идёт **до** свёртки: ``repeat_key`` не различает id, и
    первым вхождением повтора иначе осталась бы ошибка чужой операции.
    """
    first_events: dict[str, LogEvent] = {}
    places: dict[str, list[tuple[int, int]]] = {}
    for event in events:
        # Коды ошибок различают события, хотя normalize_text сводит числа к <NUM>.
        key = repeat_key(event.text)
        first_events.setdefault(key, event)
        places.setdefault(key, []).append(_event_span(event))
    return "\n\n".join(_marked_event(event, places[key]) for key, event in first_events.items())
