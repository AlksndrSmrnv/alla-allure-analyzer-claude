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
        *,
        query_label: str | None = None,
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
                    "KB совпадение%s: '%s' (id=%s), паттерн='%s'",
                    f" [{query_label}]" if query_label else "",
                    entry.title,
                    entry.id,
                    pattern,
                )

        if not results and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "KB: нет совпадений%s (query_len=%d, head='%s', tail='%s')",
                f" [{query_label}]" if query_label else "",
                len(error_text),
                _preview_head(error_text, 220),
                _preview_tail(error_text, 220),
            )

        return results


def _preview_head(text: str, max_chars: int) -> str:
    """Сжать head-preview для DEBUG-логов одной строкой."""
    return text[:max_chars].replace("\n", " ")


def _preview_tail(text: str, max_chars: int) -> str:
    """Сжать tail-preview для DEBUG-логов одной строкой."""
    if len(text) <= max_chars:
        return text.replace("\n", " ")
    return text[-max_chars:].replace("\n", " ")
