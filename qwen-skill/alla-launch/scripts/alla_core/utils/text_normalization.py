"""Нормализация текста — замена волатильных данных плейсхолдерами.

Используется в кластеризации и KB-matching для устранения различий
в UUID, timestamps, числах, IP-адресах между запусками.
"""

import re

# ---------------------------------------------------------------------------
# Скомпилированные паттерны
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_UUID_NOHYPHEN_RE = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)

# --- Даты и время (от более специфичных к менее специфичным) ---

# ISO 8601 полный datetime + опциональные секунды, millis/micros и timezone.
# Ловит HH:MM и HH:MM:SS, а также Java/Log4j запятую: 2026-02-06 10:12:13,123
_DATETIME_ISO_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2})?"
    r"(?:[.,]\d{1,6})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?"
)

# Именованные месяцы (EN): "Feb 6, 2026", "06 Feb 2026", "6-Feb-2026"
# + опциональное время после даты.
_MONTH_NAMES = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATETIME_NAMED_MONTH_RE = re.compile(
    r"(?:"
    r"\d{1,2}[- ]" + _MONTH_NAMES + r"[- ]\d{4}"
    r"|"
    + _MONTH_NAMES + r"\.?\s+\d{1,2},?\s+\d{4}"
    r")"
    r"(?:[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)?",
    re.IGNORECASE,
)

# Слэш-даты: 02/06/2026, 2026/02/06 (требуется 4-значный год)
_DATE_SLASH_RE = re.compile(
    r"\b\d{4}/\d{1,2}/\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{4}\b"
)

# Точка-даты: 06.02.2026, 2026.02.06 (требуется 4-значный год → не ловит версии)
_DATE_DOT_RE = re.compile(
    r"\b\d{4}\.\d{1,2}\.\d{1,2}\b"
    r"|\b\d{1,2}\.\d{1,2}\.\d{4}\b"
)

# ISO дата без времени: 2026-02-06
_DATE_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b(?![T ]\d{2}:\d{2}:\d{2})")

# Отдельное время: 10:12:13, 10:12:13.123, 10:12:13,456
_TIME_ONLY_RE = re.compile(
    r"(?<!\d[.:])\b\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?\b"
)

# Защита assertion-значений: числа, граничащие с " ' < [ слева ИЛИ " ' > ] справа,
# сохраняются. Покрывает Hamcrest/JUnit/AssertJ форматы ("-1001", <33>, [404])
# и quoted идентификаторы в JSON-логах. Asymmetric delimiters тоже защищают
# (OR-семантика lookbehind/lookahead).
_LONG_NUMBER_RE = re.compile(r"(?<![\"'<\[])\b\d{4,}\b(?![\"'>\]])")
_IP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
_REPORT_LOG_MARKER_RE = re.compile(r"^\s*---\s*Лог приложения\s*---\s*$")


def _normalize_common(text: str, *, replace_long_numbers: bool) -> str:
    """Применить общую normalizer-цепочку с настраиваемой заменой длинных чисел."""
    text = _UUID_RE.sub("<ID>", text)
    text = _UUID_NOHYPHEN_RE.sub("<ID>", text)
    text = _DATETIME_ISO_RE.sub("<TS>", text)
    text = _DATETIME_NAMED_MONTH_RE.sub("<TS>", text)
    text = _IP_RE.sub("<IP>", text)
    text = _DATE_SLASH_RE.sub("<TS>", text)
    text = _DATE_DOT_RE.sub("<TS>", text)
    text = _DATE_ISO_RE.sub("<TS>", text)
    text = _TIME_ONLY_RE.sub("<TS>", text)
    if replace_long_numbers:
        text = _LONG_NUMBER_RE.sub("<NUM>", text)
    return text


def normalize_text(text: str) -> str:
    """Заменить волатильные данные плейсхолдерами.

    Не трогаем саму структуру текста, не удаляем стоп-слова,
    не приводим к lowercase — всё это делает TfidfVectorizer.

    Порядок применения критичен:
    - UUID до дат (hex-UUID содержит цифры, похожие на даты)
    - Полный datetime до date-only (иначе дата матчится отдельно от времени)
    - IP до точка-дат (192.168.1.1 не должен стать <TS>)
    - Long numbers последними (иначе год «2026» станет <NUM> до матча даты)
    """
    return _normalize_common(text, replace_long_numbers=True)


def normalize_text_for_llm(text: str) -> str:
    """Мягкая normalizer-версия для LLM: без замены длинных plain-number значений."""
    return _normalize_common(text, replace_long_numbers=False)


def canonicalize_kb_error_example(text: str) -> str:
    """Подготовить каноничный error_example для KB из UI/raw-пользовательского текста.

    Убирает служебные UI-маркеры отчёта, очищает пустые строки и затем
    применяет обычную KB-нормализацию с заменой volatile-данных.
    """
    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _REPORT_LOG_MARKER_RE.match(stripped):
            continue
        cleaned_lines.append(stripped)

    cleaned = "\n".join(cleaned_lines).strip()
    if not cleaned:
        return ""
    return normalize_text(cleaned)


# ---------------------------------------------------------------------------
# Значимые числовые коды
# ---------------------------------------------------------------------------

# Коды ошибок — не волатильные данные: «error_code=10001» и «error_code=10002» —
# разные ошибки, хотя normalize_text сводит оба числа к <NUM>. Используются в
# сигнатуре (numeric fingerprint) и в свёртке повторов лога.
_NUMERIC_CONTEXT_RE = re.compile(
    r"\b(?P<label>"
    r"code|status|status_code|error_code|response_code|http_status|errno|exit_code|rc"
    r")\b"
    r"(?:\s*(?:=|:|is|was|got|returned|returning|return|with))?\s*"
    r"(?P<number>\d{4,})\b",
    re.IGNORECASE,
)
_EMBEDDED_NUMERIC_CODE_RE = re.compile(
    r"\b(?P<prefix>[a-z][a-z0-9_]{1,15})-(?P<number>\d{4,}[a-z0-9-]*)\b",
    re.IGNORECASE,
)
NON_CODE_PREFIXES = frozenset(
    {
        "error",
        "fatal",
        "severe",
        "critical",
        "traceback",
        "failed",
        "failure",
        "caused",
        "by",
        "requestid",
        "correlationid",
        "traceid",
        "spanid",
        "sessionid",
        "build",
        "job",
        "task",
        "thread",
        "worker",
        "process",
        "pid",
        "tid",
        "from",
        "for",
        "the",
        "and",
        "with",
        "while",
        "during",
    }
)


def numeric_codes(text: str) -> list[str]:
    """Коды из текста по порядку, без повторов: ``code=10001``, ``ora-01017``…

    Контекстные (``code``/``status``/``errno`` … и 4+ цифры) и встроенные
    (``ORA-01017``, но не ``thread-1234``: префиксы из :data:`NON_CODE_PREFIXES`).
    """
    normalized = " ".join(normalize_text_for_llm(text).split()).casefold()
    values = [
        f"{match.group('label').casefold()}={match.group('number')}"
        for match in _NUMERIC_CONTEXT_RE.finditer(normalized)
    ]
    for match in _EMBEDDED_NUMERIC_CODE_RE.finditer(normalized):
        prefix = match.group("prefix").casefold()
        if prefix not in NON_CODE_PREFIXES:
            values.append(f"{prefix}-{match.group('number')}")
    return list(dict.fromkeys(values))
