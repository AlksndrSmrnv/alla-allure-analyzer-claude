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

    def resolve_votes(
        self,
        items: list[tuple[int, str, int, str]],
    ) -> dict[str, tuple[FeedbackVote, int | None]]:
        """Найти exact-memory голос для каждой четверки (entry_id, hash, version, key).

        Args:
            items: Список ``(kb_entry_id, issue_signature_hash, version, resolve_key)``.
                resolve_key — произвольный ключ для идентификации элемента
                в ответе (например ``"entry_id:cluster_id"``).

        Returns:
            ``{resolve_key: (vote, feedback_id)}`` для exact feedback hits.
        """
        ...

    def get_feedback_for_signature(
        self,
        issue_signature_hash: str,
        issue_signature_version: int,
    ) -> list[FeedbackRecord]:
        """Загрузить все exact feedback-записи для одной сигнатуры проблемы."""
        ...

    def create_kb_entry(self, entry: KBEntry, project_id: int | None) -> int | None:
        """Создать новую запись KB в PostgreSQL.

        Returns:
            entry_id созданной записи, или None при конфликте slug.
        """
        ...
