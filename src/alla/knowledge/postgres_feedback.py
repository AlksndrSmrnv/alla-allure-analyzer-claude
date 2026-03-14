"""PostgreSQL-реализация хранилища обратной связи KB.

Миграция со старой (fingerprint-based) схемы:

    -- Вариант 1: чистый старт (потеря старых голосов — ОК, т.к.
    -- fingerprint-based голоса всё равно бесполезны в fuzzy системе)
    DROP TABLE IF EXISTS alla.kb_feedback;

    CREATE TABLE alla.kb_feedback (
        feedback_id    SERIAL       PRIMARY KEY,
        kb_entry_id    INTEGER      NOT NULL,
        error_text     TEXT         NOT NULL,
        error_text_hash TEXT        GENERATED ALWAYS AS (md5(error_text)) STORED,
        vote           TEXT         NOT NULL CHECK (vote IN ('like', 'dislike')),
        launch_id      INTEGER,
        cluster_id     TEXT,
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (kb_entry_id, error_text_hash)
    );
    CREATE INDEX idx_kb_feedback_entry_id ON alla.kb_feedback(kb_entry_id);
"""

from __future__ import annotations

import logging

import psycopg

from alla.exceptions import KnowledgeBaseError
from alla.knowledge.feedback_models import (
    FeedbackRecord,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackVote,
)
from alla.knowledge.models import KBEntry

logger = logging.getLogger(__name__)

# Порог similarity для resolve_votes больше не используется:
# TF-IDF заменён на exact match после normalize_text_for_llm.


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

        Дедупликация по (kb_entry_id, md5(error_text)):
        при повторном вызове с тем же нормализованным текстом обновляет vote.
        """
        query = """
            INSERT INTO alla.kb_feedback
                (kb_entry_id, error_text, vote, launch_id, cluster_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (kb_entry_id, error_text_hash)
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
                            request.error_text,
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
            error_text_preview=request.error_text[:80],
            vote=request.vote,
            created=is_insert,
        )

    def get_feedback_for_entries(
        self, entry_ids: set[int],
    ) -> list[FeedbackRecord]:
        """Загрузить все feedback-записи для данных entry_id."""
        if not entry_ids:
            return []

        query = """
            SELECT kb_entry_id, error_text, vote
            FROM alla.kb_feedback
            WHERE kb_entry_id = ANY(%s)
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (list(entry_ids),))
                    return [
                        FeedbackRecord(
                            kb_entry_id=row[0],
                            error_text=row[1],
                            vote=FeedbackVote(row[2]),
                        )
                        for row in cur.fetchall()
                    ]
        except Exception as exc:
            logger.warning("Ошибка загрузки feedback: %s", exc)
            return []

    def resolve_votes(
        self,
        items: list[tuple[int, str, str]],
    ) -> dict[str, tuple[FeedbackVote, float]]:
        """Найти наиболее похожий голос для каждой тройки (entry_id, error_text, key).

        Используется HTML-отчётом для инициализации состояния кнопок.
        key — произвольный ключ (например ``"entry_id:cluster_id"``).
        """
        if not items:
            return {}

        entry_ids = {eid for eid, _, _ in items}
        all_feedback = self.get_feedback_for_entries(entry_ids)
        if not all_feedback:
            return {}

        # Группировка по entry_id
        fb_by_entry: dict[int, list[FeedbackRecord]] = {}
        for rec in all_feedback:
            fb_by_entry.setdefault(rec.kb_entry_id, []).append(rec)

        result: dict[str, tuple[FeedbackVote, float]] = {}
        for entry_id, error_text, resolve_key in items:
            records = fb_by_entry.get(entry_id)
            if not records:
                continue
            vote, sim = _find_best_feedback_match(error_text, records)
            if vote is not None and sim > 0:
                result[resolve_key] = (vote, sim)

        return result

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


def _find_best_feedback_match(
    query_text: str,
    records: list[FeedbackRecord],
) -> tuple[FeedbackVote | None, float]:
    """Найти feedback-запись с точным совпадением нормализованного текста.

    normalize_text_for_llm уже нормализует volatile данные (UUID, timestamps,
    IP), поэтому exact match корректно находит «тот же» голос между запусками,
    но не путает кластеры с разными числовыми значениями.

    Returns:
        (vote, similarity) — (vote, 1.0) при exact match, или (None, 0.0).
    """
    if not records:
        return None, 0.0

    from alla.utils.text_normalization import normalize_text_for_llm

    query_normalized = normalize_text_for_llm(query_text)

    for record in records:
        record_normalized = normalize_text_for_llm(record.error_text)
        if record_normalized == query_normalized:
            return record.vote, 1.0

    return None, 0.0
