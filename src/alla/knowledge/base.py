"""Абстрактный интерфейс для провайдеров базы знаний."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from alla.knowledge.models import KBEntry, KBMatchResult


@runtime_checkable
class KnowledgeBaseProvider(Protocol):
    """Протокол, определяющий контракт любого провайдера базы знаний.

    Реализации:
    - YamlKnowledgeBase (MVP): читает YAML-файлы с диска
    - Будущее: VectorKnowledgeBase (RAG): ищет по эмбеддингам в vector DB
    """

    def search_by_failure(
        self,
        status_message: str | None,
        status_trace: str | None,
        category: str | None,
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные конкретной ошибке теста."""
        ...

    def search_by_text(
        self,
        query_text: str,
        *,
        max_results: int = 5,
        min_score: float = 0.0,
    ) -> list[KBMatchResult]:
        """Поиск по произвольному тексту (для будущей интеграции с LLM)."""
        ...

    def get_all_entries(self) -> list[KBEntry]:
        """Вернуть все записи базы знаний."""
        ...

    def get_entry_by_id(self, entry_id: str) -> KBEntry | None:
        """Найти запись по ID."""
        ...
