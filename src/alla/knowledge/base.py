"""Абстрактный интерфейс для провайдеров базы знаний."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from alla.knowledge.models import KBEntry, KBMatchResult


@runtime_checkable
class KnowledgeBaseProvider(Protocol):
    """Протокол, определяющий контракт любого провайдера базы знаний.

    Реализации:
    - PostgresKnowledgeBase: загружает записи из PostgreSQL при init
    - Будущее: VectorKnowledgeBase (RAG): ищет по эмбеддингам в vector DB
    """

    def search_by_error(
        self,
        error_text: str,
        *,
        query_label: str | None = None,
        error_fingerprint: str | None = None,
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные тексту ошибки.

        Args:
            error_text: Текст ошибки для поиска.
            query_label: Метка для логирования.
            error_fingerprint: Опциональный предвычисленный fingerprint (SHA-256 hex).
                Если задан — используется для lookup exclusions/boosts вместо
                вычисления fingerprint из error_text. Позволяет передать
                fingerprint report_text при поиске по log_text, обеспечивая
                соответствие фидбэку из HTML-отчёта.
        """
        ...

    def get_all_entries(self) -> list[KBEntry]:
        """Вернуть все записи базы знаний."""
        ...

    def get_entry_by_id(self, entry_id: str) -> KBEntry | None:
        """Найти запись по ID."""
        ...
