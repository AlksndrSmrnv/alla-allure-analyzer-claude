"""Абстрактный интерфейс для хранилища обратной связи KB."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from alla.knowledge.feedback_models import FeedbackRequest, FeedbackResponse, FeedbackVote
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

    def get_exclusions(self, error_fingerprint: str) -> set[int]:
        """Вернуть entry_id записей с dislike для данного fingerprint.

        Используется TextMatcher для фильтрации результатов.
        """
        ...

    def get_boosts(self, error_fingerprint: str) -> set[int]:
        """Вернуть entry_id записей с like для данного fingerprint.

        Используется TextMatcher для повышения score.
        """
        ...

    def get_votes_for_fingerprint(
        self,
        error_fingerprint: str,
    ) -> dict[int, FeedbackVote]:
        """Вернуть все голоса для fingerprint: ``{entry_id: vote}``."""
        ...

    def create_kb_entry(self, entry: KBEntry, project_id: int | None) -> int | None:
        """Создать новую запись KB в PostgreSQL.

        Returns:
            entry_id созданной записи, или None при конфликте slug.
        """
        ...
