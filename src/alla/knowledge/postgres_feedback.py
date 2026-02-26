"""PostgreSQL-реализация хранилища обратной связи KB."""

from __future__ import annotations

import logging

import psycopg

from alla.exceptions import KnowledgeBaseError
from alla.knowledge.feedback_models import (
    FeedbackRequest,
    FeedbackResponse,
    FeedbackVote,
)
from alla.knowledge.models import KBEntry

logger = logging.getLogger(__name__)


class PostgresFeedbackStore:
    """Реализация FeedbackStore для PostgreSQL.

    Использует синхронный psycopg3 (как PostgresKnowledgeBase).
    Каждый метод открывает и закрывает соединение (short-lived connections) —
    достаточно для MVP с низкой частотой вызовов (ручные клики тестировщиков).
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------
    # FeedbackStore Protocol
    # ------------------------------------------------------------------

    def record_vote(self, request: FeedbackRequest) -> FeedbackResponse:
        """UPSERT голос в alla.kb_feedback.

        При повторном вызове с тем же (kb_entry_id, error_fingerprint)
        обновляет vote и created_at.
        """
        query = """
            INSERT INTO alla.kb_feedback
                (kb_entry_id, error_fingerprint, vote, launch_id, cluster_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (kb_entry_id, error_fingerprint)
            DO UPDATE SET vote = EXCLUDED.vote, created_at = now()
            RETURNING (xmax = 0) AS is_insert
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        query,
                        (
                            request.kb_entry_id,
                            request.error_fingerprint,
                            request.vote.value,
                            request.launch_id,
                            request.cluster_id,
                        ),
                    )
                    row = cur.fetchone()
                    is_insert = row[0] if row else True
                    conn.commit()
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка записи голоса в PostgreSQL: {exc}"
            ) from exc

        return FeedbackResponse(
            kb_entry_id=request.kb_entry_id,
            error_fingerprint=request.error_fingerprint,
            vote=request.vote,
            created=is_insert,
        )

    def get_exclusions(self, error_fingerprint: str) -> set[int]:
        """Вернуть entry_id записей с dislike для данного fingerprint."""
        query = """
            SELECT kb_entry_id
            FROM alla.kb_feedback
            WHERE error_fingerprint = %s AND vote = 'dislike'
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (error_fingerprint,))
                    return {row[0] for row in cur.fetchall()}
        except Exception as exc:
            logger.warning("Ошибка чтения exclusions: %s", exc)
            return set()

    def get_boosts(self, error_fingerprint: str) -> set[int]:
        """Вернуть entry_id записей с like для данного fingerprint."""
        query = """
            SELECT kb_entry_id
            FROM alla.kb_feedback
            WHERE error_fingerprint = %s AND vote = 'like'
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (error_fingerprint,))
                    return {row[0] for row in cur.fetchall()}
        except Exception as exc:
            logger.warning("Ошибка чтения boosts: %s", exc)
            return set()

    def get_votes_for_fingerprint(
        self,
        error_fingerprint: str,
    ) -> dict[int, FeedbackVote]:
        """Вернуть все голоса для fingerprint: ``{entry_id: vote}``."""
        query = """
            SELECT kb_entry_id, vote
            FROM alla.kb_feedback
            WHERE error_fingerprint = %s
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (error_fingerprint,))
                    return {
                        row[0]: FeedbackVote(row[1]) for row in cur.fetchall()
                    }
        except Exception as exc:
            logger.warning("Ошибка чтения votes: %s", exc)
            return {}

    def create_kb_entry(
        self,
        entry: KBEntry,
        project_id: int | None,
    ) -> int | None:
        """INSERT новую запись в alla.kb_entry.

        Returns:
            entry_id при успехе, None при конфликте slug (ON CONFLICT DO NOTHING).
        """
        query = """
            INSERT INTO alla.kb_entry
                (id, title, description, error_example, category,
                 resolution_steps, project_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING entry_id
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        query,
                        (
                            entry.id,
                            entry.title,
                            entry.description,
                            entry.error_example,
                            entry.category.value,
                            list(entry.resolution_steps),
                            project_id,
                        ),
                    )
                    row = cur.fetchone()
                    conn.commit()
                    return row[0] if row else None
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка создания KB-записи: {exc}"
            ) from exc
