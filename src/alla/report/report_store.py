"""PostgreSQL-хранилище HTML-отчётов.

Таблица ``alla.report`` хранит self-contained HTML-отчёты.
Используется тот же DSN, что и для KB (``ALLURE_KB_POSTGRES_DSN``).

Миграция (выполняется автоматически при первом подключении)::

    CREATE SCHEMA IF NOT EXISTS alla;
    CREATE TABLE IF NOT EXISTS alla.report (
        id                    SERIAL       PRIMARY KEY,
        filename              TEXT         NOT NULL UNIQUE,
        launch_id             INTEGER      NOT NULL,
        html                  TEXT         NOT NULL,
        llm_prompt_tokens     INTEGER      NULL,
        llm_completion_tokens INTEGER      NULL,
        llm_total_tokens      INTEGER      NULL,
        created_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_report_launch_id ON alla.report(launch_id);
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from alla.models.llm import TokenUsage

logger = logging.getLogger(__name__)

_ENSURE_TABLE_SQL = """\
CREATE SCHEMA IF NOT EXISTS alla;
CREATE TABLE IF NOT EXISTS alla.report (
    id          SERIAL       PRIMARY KEY,
    filename    TEXT         NOT NULL UNIQUE,
    launch_id   INTEGER      NOT NULL,
    html        TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
ALTER TABLE alla.report ADD COLUMN IF NOT EXISTS project_id INTEGER NULL;
ALTER TABLE alla.report ADD COLUMN IF NOT EXISTS llm_prompt_tokens INTEGER NULL;
ALTER TABLE alla.report ADD COLUMN IF NOT EXISTS llm_completion_tokens INTEGER NULL;
ALTER TABLE alla.report ADD COLUMN IF NOT EXISTS llm_total_tokens INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_report_launch_id  ON alla.report(launch_id);
CREATE INDEX IF NOT EXISTS idx_report_project_id ON alla.report(project_id);
CREATE INDEX IF NOT EXISTS idx_report_created_at ON alla.report(created_at);
"""


class PostgresReportStore:
    """Сохранение и загрузка HTML-отчётов в PostgreSQL."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._ensure_table()

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_ENSURE_TABLE_SQL)
            conn.commit()
        logger.info("alla.report table ready")

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def save(
        self,
        filename: str,
        launch_id: int,
        html: str,
        project_id: int | None = None,
        *,
        token_usage: "TokenUsage | None" = None,
    ) -> None:
        """Вставить новый отчёт или тихо заменить существующий по filename."""
        prompt_tokens = token_usage.prompt_tokens if token_usage is not None else None
        completion_tokens = token_usage.completion_tokens if token_usage is not None else None
        total_tokens = token_usage.total_tokens if token_usage is not None else None

        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO alla.report ("
                    "filename, launch_id, html, project_id, "
                    "llm_prompt_tokens, llm_completion_tokens, llm_total_tokens"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (filename) DO UPDATE "
                    "SET html = EXCLUDED.html, "
                    "project_id = EXCLUDED.project_id, "
                    "llm_prompt_tokens = EXCLUDED.llm_prompt_tokens, "
                    "llm_completion_tokens = EXCLUDED.llm_completion_tokens, "
                    "llm_total_tokens = EXCLUDED.llm_total_tokens",
                    (
                        filename,
                        launch_id,
                        html,
                        project_id,
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                    ),
                )
            conn.commit()

    def load(self, filename: str) -> str | None:
        """Вернуть HTML-содержимое по filename или ``None``, если отчёт не найден."""
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT html FROM alla.report WHERE filename = %s",
                    (filename,),
                )
                row = cur.fetchone()
                return row[0] if row else None

    def list_for_launch(self, launch_id: int) -> list[str]:
        """Вернуть filenames для launch, сначала самые новые."""
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT filename FROM alla.report "
                    "WHERE launch_id = %s ORDER BY created_at DESC",
                    (launch_id,),
                )
                return [row[0] for row in cur.fetchall()]
