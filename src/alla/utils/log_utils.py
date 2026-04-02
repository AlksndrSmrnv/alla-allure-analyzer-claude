"""Утилиты для анализа лог-фрагментов."""

import re
from collections.abc import Mapping

# Паттерны явных ошибок в логах приложения
_LOG_ERROR_RE = re.compile(
    r"\b(?:ERROR|FATAL|SEVERE|CRITICAL)\b"
    r"|(?:Exception|Error|Traceback|Caused by)\b"
    r"|(?:FAILED|Failed to)\b",
    re.IGNORECASE,
)

# Заголовок секции, создаваемый LogExtractionService:
# --- [файл: name] ---
# --- [HTTP: name] ---
_LOG_SECTION_RE = re.compile(
    r"^--- \[(?P<section_type>файл|HTTP): (?P<section_name>.+?)\] ---$",
    re.MULTILINE,
)
_CORRELATION_LINE_RE = re.compile(
    r"^\s*Корреляция:\s*(?P<pairs>.+?)\s*$",
    re.IGNORECASE,
)
_CORRELATION_PAIR_RE = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9]*)\s*=\s*(?P<value>[^,\s][^,]*?)(?=\s*(?:,|$))"
)
_CANONICAL_CORRELATION_KEYS = {
    "operuid": "operUID",
    "rquid": "rqUID",
    "requestid": "requestId",
    "correlationid": "correlationId",
    "traceid": "traceId",
}
_CORRELATION_KEY_PRIORITY = {
    "operuid": 0,
    "rquid": 1,
    "requestid": 2,
    "correlationid": 3,
    "traceid": 4,
}


def has_explicit_errors(log_snippet: str | None) -> bool:
    """Проверить наличие явных маркеров ошибок в лог-фрагменте."""
    if not log_snippet:
        return False
    return bool(_LOG_ERROR_RE.search(log_snippet))


def parse_correlation_line(line: str) -> dict[str, str]:
    """Разобрать строку ``Корреляция: ...`` в mapping key -> value.

    Ключи нормализуются к каноничному display-виду (``operUID``, ``rqUID``).
    При дублирующихся ключах сохраняется первое непустое значение.
    """
    match = _CORRELATION_LINE_RE.match(line)
    if match is None:
        return {}

    pairs: dict[str, str] = {}
    for pair_match in _CORRELATION_PAIR_RE.finditer(match.group("pairs")):
        raw_key = pair_match.group("key").strip()
        raw_value = pair_match.group("value").strip()
        if not raw_value:
            continue
        display_key = _CANONICAL_CORRELATION_KEYS.get(raw_key.lower(), raw_key)
        pairs.setdefault(display_key, raw_value)
    return pairs


def format_correlation_pairs(pairs: Mapping[str, str]) -> str | None:
    """Сформировать каноничную строку ``key=value, ...`` для correlation IDs."""
    normalized: dict[str, str] = {}
    for raw_key, raw_value in pairs.items():
        key = str(raw_key).strip()
        value = str(raw_value).strip()
        if not key or not value:
            continue
        display_key = _CANONICAL_CORRELATION_KEYS.get(key.lower(), key)
        normalized.setdefault(display_key, value)

    if not normalized:
        return None

    ordered_keys = sorted(
        normalized,
        key=lambda key: (_CORRELATION_KEY_PRIORITY.get(key.lower(), 100), key.lower()),
    )
    return ", ".join(f"{key}={normalized[key]}" for key in ordered_keys)


def parse_log_sections(
    log_snippet: str,
    *,
    include_http: bool = True,
) -> list[tuple[str, str]]:
    """Разбить log_snippet на секции [(label, content), ...].

    LogExtractionService объединяет ERROR-блоки из нескольких файлов в одну
    строку с разделителями вида ``--- [файл: name.log] ---`` или
    ``--- [HTTP: response.json] ---``. Эта функция разбивает строку обратно
    на именованные секции.

    Для HTTP-секций label возвращается как ``HTTP: <name>``, чтобы downstream
    consumers могли отобразить их отдельно от обычных лог-файлов.

    Если заголовков не найдено (например, старый формат или один файл без
    заголовка) — возвращает ``[("", log_snippet.strip())]``.
    """
    matches = list(_LOG_SECTION_RE.finditer(log_snippet))
    if not matches:
        return [("", log_snippet.strip())]

    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        section_type = match.group("section_type")
        if section_type == "HTTP" and not include_http:
            continue

        name = match.group("section_name").strip()
        body_start = match.end()
        body_end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(log_snippet)
        )
        body = log_snippet[body_start:body_end].strip()
        if body:
            label = name if section_type == "файл" else f"HTTP: {name}"
            sections.append((label, body))
    return sections


def extract_correlation_from_log(log_snippet: str | None) -> str | None:
    """Извлечь первую доступную correlation-строку из HTTP-секций лога."""
    if not log_snippet:
        return None

    for label, body in parse_log_sections(log_snippet):
        if not label.startswith("HTTP:"):
            continue
        for line in body.splitlines():
            pairs = parse_correlation_line(line)
            if pairs:
                return format_correlation_pairs(pairs)
    return None
