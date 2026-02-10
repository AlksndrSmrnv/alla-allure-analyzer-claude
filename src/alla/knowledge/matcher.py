"""Алгоритм сопоставления ошибок с записями базы знаний."""

from __future__ import annotations

import logging

from alla.knowledge.models import KBEntry, KBMatchResult

logger = logging.getLogger(__name__)


class TextMatcher:
    """Сопоставляет текст ошибки с записями KB по подстроке error_pattern."""

    def match(
        self,
        error_text: str,
        entries: list[KBEntry],
    ) -> list[KBMatchResult]:
        """Найти записи KB, чей error_pattern содержится в тексте ошибки.

        Args:
            error_text: Объединённый текст ошибки (message + trace).
            entries: Записи KB для сопоставления.

        Returns:
            Список KBMatchResult для всех совпавших записей (score=1.0).
        """
        if not error_text or not error_text.strip():
            return []

        text_lower = error_text.lower()
        results: list[KBMatchResult] = []

        for entry in entries:
            pattern = entry.error_pattern.strip()
            if not pattern:
                continue

            if pattern.lower() in text_lower:
                results.append(KBMatchResult(entry=entry, score=1.0))
                logger.debug(
                    "KB совпадение: '%s' (id=%s), паттерн='%s'",
                    entry.title,
                    entry.id,
                    pattern,
                )

        if not results:
            preview = error_text[:150].replace("\n", " ")
            logger.debug("KB: нет совпадений для запроса='%s'", preview)

        return results
