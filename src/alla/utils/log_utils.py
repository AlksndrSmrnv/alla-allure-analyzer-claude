"""Утилиты для анализа лог-фрагментов."""

import re
from collections.abc import Mapping
from typing import Any

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
# --- [журнал: name] ---  (структурированные лог-журналы)
# Допускаем любой непустой токен типа секции — это позволяет добавлять новые
# AttachmentHandler-ы без правок этого regex.
_LOG_SECTION_RE = re.compile(
    r"^--- \[(?P<section_type>[^\]:\s][^\]:]*?): (?P<section_name>.+?)\] ---$",
    re.MULTILINE,
)

# Единый реестр correlation-ключей: новая запись lowercase -> display
# автоматически подхватывается regex-ами, JSON-recursion и сортировкой.
# При расширении списка проверьте также feedback_signature.py:_GENERIC_LOG_WORDS,
# где похожие токены фильтруются для другой, независимой логики.
_CANONICAL_CORRELATION_KEYS = {
    "operuid": "operUID",
    "rquid": "rqUID",
    "requestid": "requestId",
    "correlationid": "correlationId",
    "traceid": "traceId",
}
_CORRELATION_KEY_PRIORITY = {
    key: index for index, key in enumerate(_CANONICAL_CORRELATION_KEYS)
}
_CORRELATION_KEYS_LOWER: frozenset[str] = frozenset(_CANONICAL_CORRELATION_KEYS)
_CORRELATION_VALUE_PATTERN = r"[A-Za-z0-9\-_.]{4,64}"
_PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {
        "string",
        "integer",
        "long",
        "boolean",
        "uuid",
        "object",
        "number",
        "double",
        "float",
        "short",
        "byte",
        "char",
        "bigdecimal",
        "biginteger",
        "localdatetime",
        "localdate",
        "instant",
        "offsetdatetime",
        "zoneddatetime",
        "timestamp",
        "date",
        "time",
        "array",
        "list",
        "map",
        "set",
        "enum",
        "any",
        "null",
        "none",
        "undefined",
        "example",
        "value",
        "placeholder",
        "todo",
        "xxx",
    }
)
_KEY_ALTERNATION = "|".join(re.escape(key) for key in _CANONICAL_CORRELATION_KEYS)
_CORRELATION_LINE_RE = re.compile(
    r"^\s*Корреляция:\s*(?P<pairs>.+?)\s*$",
    re.IGNORECASE,
)
_CORRELATION_PAIR_RE = re.compile(
    rf"(?P<key>[A-Za-z][A-Za-z0-9]*)\s*=\s*(?P<value>{_CORRELATION_VALUE_PATTERN})"
)
_CORR_ID_JSON_RE = re.compile(
    rf"\"(?P<key>{_KEY_ALTERNATION})\"\s*:\s*\"(?P<value>{_CORRELATION_VALUE_PATTERN})\"",
    re.IGNORECASE,
)
_CORR_ID_KV_RE = re.compile(
    rf"\b(?P<key>{_KEY_ALTERNATION})\b"
    rf"\s*[=:]\s*\"?(?P<value>{_CORRELATION_VALUE_PATTERN})",
    re.IGNORECASE,
)
_CORR_ID_XML_RE = re.compile(
    rf"<(?P<key>{_KEY_ALTERNATION})>"
    rf"(?P<value>{_CORRELATION_VALUE_PATTERN})"
    rf"</(?:{_KEY_ALTERNATION})>",
    re.IGNORECASE,
)


def _is_placeholder_value(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDER_VALUES


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
        if not raw_value or _is_placeholder_value(raw_value):
            continue
        display_key = _CANONICAL_CORRELATION_KEYS.get(raw_key.lower(), raw_key)
        pairs.setdefault(display_key, raw_value)
    return pairs


def extract_correlation_pairs_from_text(text: str) -> dict[str, str]:
    """Извлечь correlation ID пары из произвольного текста.

    Применяет JSON/KV/XML regex-ы. Ключи возвращаются как в исходном тексте;
    нормализация display-вида выполняется в ``format_correlation_pairs``.
    Первое встретившееся значение для ключа выигрывает.
    """
    if not text:
        return {}

    matches: list[tuple[int, int, str, str]] = []
    for order, regex in enumerate((_CORR_ID_JSON_RE, _CORR_ID_KV_RE, _CORR_ID_XML_RE)):
        for match in regex.finditer(text):
            matches.append(
                (
                    match.start(),
                    order,
                    match.group("key"),
                    match.group("value"),
                )
            )

    pairs: dict[str, str] = {}
    seen_keys: set[str] = set()
    for _start, _order, raw_key, raw_value in sorted(matches):
        key_lower = raw_key.lower()
        if key_lower in seen_keys:
            continue
        value = raw_value.strip()
        if not value or _is_placeholder_value(value):
            continue
        pairs[raw_key] = value
        seen_keys.add(key_lower)
    return pairs


def extract_correlation_pairs_from_json(
    obj: Any,
    *,
    max_depth: int = 10,
) -> dict[str, str]:
    """Рекурсивно собрать correlation ID пары из JSON-like объекта.

    Обходит dict/list до ``max_depth`` включительно. Учитываются только scalar
    values ``str``/``int``; значения обрезаются до 64 символов. Ключи
    возвращаются как в исходном объекте.
    """
    pairs: dict[str, str] = {}
    seen_keys: set[str] = set()

    def visit(value: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(value, dict):
            for raw_key, raw_value in value.items():
                key = str(raw_key)
                key_lower = key.lower()
                if (
                    key_lower in _CORRELATION_KEYS_LOWER
                    and isinstance(raw_value, (str, int))
                ):
                    normalized_value = str(raw_value).strip()
                    if (
                        normalized_value
                        and not _is_placeholder_value(normalized_value)
                        and key_lower not in seen_keys
                    ):
                        pairs[key] = normalized_value[:64]
                        seen_keys.add(key_lower)
                    continue
                visit(raw_value, depth + 1)
        elif isinstance(value, list):
            for item in value:
                visit(item, depth + 1)

    visit(obj, 0)
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
            # `файл` остаётся без префикса для обратной совместимости с
            # downstream-кодом (HTML-отчёт, CLI). Все остальные типы секций
            # (HTTP, журнал, …) получают префикс ``<тип>: ``.
            label = name if section_type == "файл" else f"{section_type}: {name}"
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
