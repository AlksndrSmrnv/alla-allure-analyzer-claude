"""PostgreSQL-реализация хранилища exact feedback memory."""

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from alla.exceptions import KnowledgeBaseError
from alla.knowledge.feedback_models import (
    FeedbackRecord,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackVote,
)
from alla.knowledge.models import KBEntry
from alla.knowledge.models import RootCauseCategory

logger = logging.getLogger(__name__)


class PostgresFeedbackStore:
    """Реализация FeedbackStore для PostgreSQL."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def record_vote(self, request: FeedbackRequest) -> FeedbackResponse:
        """UPSERT голос в alla.kb_feedback по exact issue signature."""
        query = """
            INSERT INTO alla.kb_feedback (
                kb_entry_id,
                error_text,
                vote,
                launch_id,
                cluster_id,
                issue_signature_hash,
                issue_signature_version,
                issue_signature_payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (kb_entry_id, issue_signature_hash)
            DO UPDATE SET
                error_text = EXCLUDED.error_text,
                vote = EXCLUDED.vote,
                launch_id = EXCLUDED.launch_id,
                cluster_id = EXCLUDED.cluster_id,
                issue_signature_version = EXCLUDED.issue_signature_version,
                issue_signature_payload = EXCLUDED.issue_signature_payload,
                created_at = now()
            RETURNING (xmax = 0) AS is_insert, feedback_id
        """
        payload = request.issue_signature_payload or {
            "signature_hash": request.issue_signature_hash,
            "version": request.issue_signature_version,
        }
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        query,
                        (
                            request.kb_entry_id,
                            request.audit_text,
                            request.vote.value,
                            request.launch_id,
                            request.cluster_id,
                            request.issue_signature_hash,
                            request.issue_signature_version,
                            Jsonb(payload),
                        ),
                    )
                    row = cur.fetchone()
                    is_insert = row[0] if row else True
                    fb_id = row[1] if row else None
                    conn.commit()
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка записи голоса в PostgreSQL: {exc}"
            ) from exc

        return FeedbackResponse(
            kb_entry_id=request.kb_entry_id,
            audit_text_preview=request.audit_text[:80],
            vote=request.vote,
            created=is_insert,
            feedback_id=fb_id,
        )

    def resolve_votes(
        self,
        items: list[tuple[int, str, int, str]],
    ) -> dict[str, tuple[FeedbackVote, int | None]]:
        """Найти exact-memory голос для каждой пары entry + issue_signature."""
        if not items:
            return {}

        signature_hashes = sorted({issue_hash for _, issue_hash, _, _ in items if issue_hash})
        if not signature_hashes:
            return {}

        query = """
            SELECT feedback_id, kb_entry_id, vote, issue_signature_hash, issue_signature_version
            FROM alla.kb_feedback
            WHERE issue_signature_hash = ANY(%s)
        """
        records: dict[tuple[int, str, int], tuple[FeedbackVote, int | None]] = {}
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (signature_hashes,))
                    for row in cur.fetchall():
                        feedback_id, kb_entry_id, vote_raw, issue_hash, version = row
                        if not issue_hash or version is None:
                            continue
                        records[(kb_entry_id, issue_hash, version)] = (
                            FeedbackVote(vote_raw),
                            feedback_id,
                        )
        except Exception as exc:
            logger.warning("Ошибка exact-resolve feedback: %s", exc)
            return {}

        resolved: dict[str, tuple[FeedbackVote, int | None]] = {}
        for entry_id, issue_hash, version, resolve_key in items:
            hit = records.get((entry_id, issue_hash, version))
            if hit is not None:
                resolved[resolve_key] = hit

        return resolved

    def get_feedback_for_signature(
        self,
        issue_signature_hash: str,
        issue_signature_version: int,
    ) -> list[FeedbackRecord]:
        """Загрузить все feedback-записи для exact issue signature."""
        if not issue_signature_hash:
            return []

        query = """
            SELECT
                feedback_id,
                kb_entry_id,
                error_text,
                vote,
                issue_signature_hash,
                issue_signature_version,
                issue_signature_payload
            FROM alla.kb_feedback
            WHERE issue_signature_hash = %s
              AND issue_signature_version = %s
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        query,
                        (issue_signature_hash, issue_signature_version),
                    )
                    return [
                        FeedbackRecord(
                            feedback_id=row[0],
                            kb_entry_id=row[1],
                            audit_text=row[2],
                            vote=FeedbackVote(row[3]),
                            issue_signature_hash=row[4],
                            issue_signature_version=row[5],
                            issue_signature_payload=row[6],
                        )
                        for row in cur.fetchall()
                        if row[4] is not None
                    ]
        except Exception as exc:
            logger.warning("Ошибка загрузки feedback по сигнатуре: %s", exc)
            return []

    def create_kb_entry(
        self,
        entry: KBEntry,
        project_id: int | None,
    ) -> int | None:
        """INSERT новую запись в alla.kb_entry."""
        query = """
            INSERT INTO alla.kb_entry
                (id, title, description, error_example, step_path, category,
                 resolution_steps, project_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
                            entry.step_path,
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

    def find_kb_entry_by_slug(
        self, slug: str, project_id: int | None
    ) -> KBEntry | None:
        """SELECT существующую запись по (id, project_id). NULL project_id → глобальная."""
        if project_id is None:
            where = "id = %s AND project_id IS NULL"
            params: tuple[object, ...] = (slug,)
        else:
            where = "id = %s AND project_id = %s"
            params = (slug, project_id)
        query = (
            "SELECT entry_id, id, title, description, error_example, step_path, "
            "category, resolution_steps, project_id FROM alla.kb_entry "
            f"WHERE {where} LIMIT 1"
        )
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    row = cur.fetchone()
                    if row is None:
                        return None
                    return KBEntry(
                        entry_id=row[0],
                        id=row[1],
                        title=row[2],
                        description=row[3] or "",
                        error_example=row[4],
                        step_path=row[5],
                        category=row[6],
                        resolution_steps=list(row[7] or []),
                        project_id=row[8],
                    )
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка чтения записи базы знаний по slug: {exc}"
            ) from exc

    def update_kb_entry(self, entry_id: int, fields: dict[str, Any]) -> bool:
        """UPDATE запись в alla.kb_entry по entry_id."""
        allowed = {
            "title",
            "description",
            "error_example",
            "step_path",
            "category",
            "resolution_steps",
        }
        to_update = {k: v for k, v in fields.items() if k in allowed}
        if not to_update:
            return False

        set_parts: list[str] = []
        params: list[object] = []
        for col, val in to_update.items():
            set_parts.append(f"{col} = %s")
            if col == "resolution_steps":
                params.append(list(val))
            else:
                params.append(val)
        params.append(entry_id)

        query = f"UPDATE alla.kb_entry SET {', '.join(set_parts)} WHERE entry_id = %s"
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    updated = cur.rowcount > 0
                    conn.commit()
                    return updated
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка обновления записи базы знаний: {exc}"
            ) from exc

    def delete_kb_entry(self, entry_id: int) -> bool:
        """DELETE запись из alla.kb_entry по entry_id."""
        query = """
            DELETE FROM alla.kb_entry
            WHERE entry_id = %s
            RETURNING entry_id
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (entry_id,))
                    row = cur.fetchone()
                    conn.commit()
                    return row is not None
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка удаления записи базы знаний: {exc}"
            ) from exc

    def count_feedback_for_entry(self, entry_id: int) -> int:
        """Посчитать feedback-голоса, связанные с KB-записью."""
        query = "SELECT COUNT(*) FROM alla.kb_feedback WHERE kb_entry_id = %s"
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (entry_id,))
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка подсчёта feedback для KB-записи: {exc}"
            ) from exc

    def list_kb_entries(self, project_id: int | None = None) -> list[KBEntry]:
        """Вернуть KB-записи для CLI/API list."""
        if project_id is None:
            query = """
                SELECT entry_id, id, title, description, error_example,
                       step_path, category, resolution_steps, project_id
                FROM alla.kb_entry
                ORDER BY project_id NULLS FIRST, id
            """
            params: tuple[Any, ...] = ()
        else:
            query = """
                SELECT entry_id, id, title, description, error_example,
                       step_path, category, resolution_steps, project_id
                FROM alla.kb_entry
                WHERE project_id IS NULL OR project_id = %s
                ORDER BY project_id NULLS FIRST, id
            """
            params = (project_id,)

        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка чтения списка KB-записей: {exc}"
            ) from exc

        entries: list[KBEntry] = []
        for row in rows:
            entries.append(
                KBEntry(
                    entry_id=row[0],
                    id=row[1],
                    title=row[2],
                    description=row[3] or "",
                    error_example=row[4] or "",
                    step_path=row[5],
                    category=RootCauseCategory(row[6]),
                    resolution_steps=list(row[7] or []),
                    project_id=row[8],
                )
            )
        return entries
