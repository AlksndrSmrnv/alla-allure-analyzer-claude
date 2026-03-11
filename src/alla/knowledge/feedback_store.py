"""Абстрактный интерфейс для хранилища обратной связи KB."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from alla.knowledge.feedback_models import (
    FeedbackRecord,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackVote,
)
from alla.knowledge.models import KBEntry


@runtime_checkable
class FeedbackStore(Protocol):
    """Протокол хранилища обратной связи KB.

    Отделён от ``KnowledgeBaseProvider``: не все бэкенды поддерживают
    обратную связь (YAML — read-only, feedback требует PostgreSQL).

    Реализации:
    - PostgresFeedbackStore: хранение в alla.kb_feedback (PostgreSQL)
    """

    def record_vote(self, request: FeedbackRequest) -> FeedbackResponse:
        """Записать like/dislike. UPSERT: повторный вызов обновляет голос."""
        ...

    def get_feedback_for_entries(
        self, entry_ids: set[int],
    ) -> list[FeedbackRecord]:
        """Загрузить все записи feedback для данных entry_id.

        Вызывающий код использует результат для fuzzy matching
        через TF-IDF cosine similarity.
        """
        ...

    def resolve_votes(
        self,
        items: list[tuple[int, str, str]],
    ) -> dict[str, tuple[FeedbackVote, float]]:
        """Найти наиболее похожий голос для каждой тройки (entry_id, error_text, key).

        Использует fuzzy text similarity (TF-IDF cosine) для поиска
        ближайшего сохранённого голоса.

        Args:
            items: Список ``(kb_entry_id, error_text, resolve_key)``.
                resolve_key — произвольный ключ для идентификации элемента
                в ответе (например ``"entry_id:cluster_id"``).

        Returns:
            ``{resolve_key: (vote, similarity)}`` для элементов с подходящим feedback.
        """
        ...

    def create_kb_entry(self, entry: KBEntry, project_id: int | None) -> int | None:
        """Создать новую запись KB в PostgreSQL.

        Returns:
            entry_id созданной записи, или None при конфликте slug.
        """
        ...
