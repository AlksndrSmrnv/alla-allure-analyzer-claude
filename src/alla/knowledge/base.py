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

    def search_by_error(
        self,
        error_text: str,
        *,
        query_label: str | None = None,
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные тексту ошибки."""
        ...

    def get_all_entries(self) -> list[KBEntry]:
        """Вернуть все записи базы знаний."""
        ...

    def get_entry_by_id(self, entry_id: str) -> KBEntry | None:
        """Найти запись по ID."""
        ...
