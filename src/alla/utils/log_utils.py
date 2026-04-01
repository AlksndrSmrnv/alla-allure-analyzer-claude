"""Утилиты для анализа лог-фрагментов."""

import re

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


def has_explicit_errors(log_snippet: str | None) -> bool:
    """Проверить наличие явных маркеров ошибок в лог-фрагменте."""
    if not log_snippet:
        return False
    return bool(_LOG_ERROR_RE.search(log_snippet))


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
