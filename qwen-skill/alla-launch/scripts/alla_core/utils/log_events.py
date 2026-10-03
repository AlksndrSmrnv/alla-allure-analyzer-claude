"""События текстового лога: где запись начинается, где кончается и ошибка ли она.

Лог делится на события. Событие начинается строкой с признаком новой записи и
продолжается всеми строками без такого признака (кадры стека, ``Caused by``,
``... N more``, строки с отступом, многострочное сообщение). Поэтому соседняя
INFO-запись со своим временем не поглощается исключением.

Признаки начала записи:

* время в начале строки (возможно, в ``[]``): ISO с ``T`` или пробелом,
  ``dd.MM.yyyy``, ``yyyy/MM/dd``, ``HH:mm:ss.SSS``, syslog ``Oct 03 10:00:00``,
  JUL ``Oct 03, 2026 10:00:00 AM``; logfmt ``time=``/``ts=``;
* Python ``ERROR:root:…`` (``LEVEL:`` без пробела после двоеточия);
* ``Traceback (most recent call last):`` и строка исключения
  (``pkg.SomeException: …``), за которой идут кадры ``at …`` — если идущее
  событие не ошибка (иначе это продолжение её стека).

Уровень берётся только из позиции уровня: первые слова после времени
(``ERROR``, ``[ERROR]``, ``|ERROR|``, ``ERROR/Worker``…; в скобках регистр
любой, без скобок — только заглавные) или ключ ``level=`` в logfmt. Слово
``ERROR`` в тексте INFO-сообщения ошибкой не считается: первым найденный
уровень и решает. JUL склеивается: строка «дата + класс + метод» и следующая
``SEVERE: …`` — одно событие. Traceback Python заканчивается строкой
исключения.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

ERROR_LEVELS = frozenset({
    "ERROR", "ERR", "FATAL", "SEVERE", "CRITICAL", "CRIT", "ALERT", "EMERG", "EMERGENCY",
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
_LOGFMT_LEVEL_RE = re.compile(r"(?:^|\s)(?:level|lvl|severity)=\"?(?P<level>[A-Za-z]+)")
_PYTHON_LEVEL_RE = re.compile(r"^(?P<level>[A-Z]+):(?=\S)")
_JUL_LEVEL_RE = re.compile(r"^(?P<level>[A-Z]+): ")
_TRACEBACK_RE = re.compile(r"^Traceback \(most recent call last\):")
_EXCEPTION_RE = re.compile(r"^[\w.$]+(?:Exception|Error|Throwable)(?::\s.*)?$")
_FRAME_RE = re.compile(r"^\s*at\s+\S")
_TOKEN_STRIP = "[]|:,;()<>"


@dataclass
class LogEvent:
    """Событие лога; номера строк — с 1, по ``str.splitlines``."""

    first_line: int
    level: str | None
    kind: str  # level | traceback | exception | plain
    lines: list[str] = field(default_factory=list)
    closed: bool = False  # Traceback дошёл до строки исключения

    @property
    def last_line(self) -> int:
        return self.first_line + len(self.lines) - 1

    @property
    def is_error(self) -> bool:
        return self.level in ERROR_LEVELS or self.kind in ("traceback", "exception")

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _level_after_timestamp(rest: str) -> str | None:
    """Первый уровень среди первых слов после времени; дальше — уже сообщение."""
    for token in rest.split()[:_LEVEL_SCAN_TOKENS]:
        decorated = token != token.strip(_TOKEN_STRIP) or "/" in token
        word = token.strip(_TOKEN_STRIP).split("/", 1)[0]
        if not word:
            continue
        if word.upper() in _LEVELS and (decorated or word.isupper()):
            return word.upper()
    return None


def _start(line: str) -> tuple[bool, str | None]:
    """Начинает ли строка запись (время, logfmt, ``ERROR:root:``) и с каким уровнем."""
    match = _TIMESTAMP_RE.match(line)
    if match:
        return True, _level_after_timestamp(line[match.end():])
    if _LOGFMT_START_RE.match(line):
        level = _LOGFMT_LEVEL_RE.search(line)
        return True, level.group("level").upper() if level else None
    python = _PYTHON_LEVEL_RE.match(line)
    if python and python.group("level") in _LEVELS:
        return True, python.group("level")
    return False, None


def iter_events(text: str) -> Iterator[LogEvent]:
    """События лога по порядку; каждая строка принадлежит ровно одному событию."""
    lines = text.splitlines()
    current: LogEvent | None = None
    for index, line in enumerate(lines):
        number = index + 1
        started, level = _start(line)
        if started:
            if current is not None:
                yield current
            current = LogEvent(number, level, "level", [line])
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
            continue
        current.lines.append(line)
        if current.kind == "traceback" and line.strip() and not line[0].isspace():
            current.closed = True  # строка исключения завершает Traceback
    if current is not None:
        yield current


def parse_events(text: str) -> list[LogEvent]:
    return list(iter_events(text))


def error_events(text: str) -> Iterator[LogEvent]:
    """События-ошибки: уровень ошибки, Traceback или исключение со стеком."""
    return (event for event in iter_events(text) if event.is_error)

