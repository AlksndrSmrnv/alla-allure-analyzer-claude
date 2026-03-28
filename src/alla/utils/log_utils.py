"""Утилиты для анализа лог-фрагментов."""

import re

# Паттерны явных ошибок в логах приложения
_LOG_ERROR_RE = re.compile(
    r"\b(?:ERROR|FATAL|SEVERE|CRITICAL)\b"
    r"|(?:Exception|Error|Traceback|Caused by)\b"
    r"|(?:FAILED|Failed to)\b",
    re.IGNORECASE,
)

# Заголовок секции, создаваемый LogExtractionService: --- [файл: name] ---
_LOG_SECTION_RE = re.compile(r"^--- \[файл: (.+?)\] ---$", re.MULTILINE)


def has_explicit_errors(log_snippet: str | None) -> bool:
    """Проверить наличие явных маркеров ошибок в лог-фрагменте."""
    if not log_snippet:
        return False
    return bool(_LOG_ERROR_RE.search(log_snippet))


def parse_log_sections(log_snippet: str) -> list[tuple[str, str]]:
    """Разбить log_snippet на секции [(filename, content), ...].

    LogExtractionService объединяет ERROR-блоки из нескольких файлов в одну
    строку с разделителями вида ``--- [файл: name.log] ---``.
    Эта функция разбивает строку обратно на именованные секции.

    Если заголовков не найдено (например, старый формат или один файл без
    заголовка) — возвращает ``[("", log_snippet.strip())]``.
    """
    parts = _LOG_SECTION_RE.split(log_snippet)
    # re.split() с группой возвращает: [prefix, name1, body1, name2, body2, ...]
    sections: list[tuple[str, str]] = []
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if body:
            sections.append((name, body))
    return sections or [("", log_snippet.strip())]
