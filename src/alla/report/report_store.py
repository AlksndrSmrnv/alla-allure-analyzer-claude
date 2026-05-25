"""PostgreSQL-хранилище HTML-отчётов.

Таблица ``alla.report`` хранит self-contained HTML-отчёты.
Используется тот же DSN, что и для KB (``ALLURE_KB_POSTGRES_DSN``).

Схема (создаётся автоматически при первом подключении)::

    CREATE SCHEMA IF NOT EXISTS alla;
    CREATE TABLE IF NOT EXISTS alla.report (
        id                     SERIAL       PRIMARY KEY,
        filename               TEXT         NOT NULL UNIQUE,
        launch_id              INTEGER      NOT NULL,
        html                   TEXT         NOT NULL,
        project_id             INTEGER      NULL,
        llm_prompt_tokens      INTEGER      NULL,
        llm_completion_tokens  INTEGER      NULL,
        llm_total_tokens       INTEGER      NULL,
        analysis_duration_ms   INTEGER      NULL,
        created_at             TIMESTAMPTZ  NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_report_launch_id  ON alla.report(launch_id);
    CREATE INDEX IF NOT EXISTS idx_report_project_id ON alla.report(project_id);
    CREATE INDEX IF NOT EXISTS idx_report_created_at ON alla.report(created_at);

    CREATE TABLE IF NOT EXISTS alla.report_view (
        view_id     BIGSERIAL    PRIMARY KEY,
        filename    TEXT         NOT NULL,
        project_id  INTEGER      NULL,
        launch_id   INTEGER      NULL,
        viewer_id   TEXT         NULL,
        viewed_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_report_view_viewed_at  ON alla.report_view(viewed_at);
    CREATE INDEX IF NOT EXISTS idx_report_view_project_id ON alla.report_view(project_id);
    CREATE INDEX IF NOT EXISTS idx_report_view_filename   ON alla.report_view(filename);
    -- Дедупликация: один (filename, viewer_id) — одна строка. NULL viewer_id
    -- (легаси/нет cookie) в индекс не попадают и не дедуплицируются.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_report_view_unique_per_viewer
        ON alla.report_view(filename, viewer_id) WHERE viewer_id IS NOT NULL;
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import psycopg

if TYPE_CHECKING:
    from alla.models.llm import TokenUsage

logger = logging.getLogger(__name__)

_ENSURE_TABLE_SQL = """\
CREATE SCHEMA IF NOT EXISTS alla;
CREATE TABLE IF NOT EXISTS alla.report (
    id                     SERIAL       PRIMARY KEY,
    filename               TEXT         NOT NULL UNIQUE,
    launch_id              INTEGER      NOT NULL,
    html                   TEXT         NOT NULL,
    project_id             INTEGER      NULL,
    llm_prompt_tokens      INTEGER      NULL,
    llm_completion_tokens  INTEGER      NULL,
    llm_total_tokens       INTEGER      NULL,
    analysis_duration_ms   INTEGER      NULL,
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_report_launch_id  ON alla.report(launch_id);
CREATE INDEX IF NOT EXISTS idx_report_project_id ON alla.report(project_id);
CREATE INDEX IF NOT EXISTS idx_report_created_at ON alla.report(created_at);
"""

_ENSURE_VIEW_TABLE_SQL = """\
CREATE SCHEMA IF NOT EXISTS alla;
CREATE TABLE IF NOT EXISTS alla.report_view (
    view_id     BIGSERIAL    PRIMARY KEY,
    filename    TEXT         NOT NULL,
    project_id  INTEGER      NULL,
    launch_id   INTEGER      NULL,
    viewer_id   TEXT         NULL,
    viewed_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
-- Миграция для существующих установок, созданных до появления колонки.
ALTER TABLE alla.report_view ADD COLUMN IF NOT EXISTS viewer_id TEXT;
CREATE INDEX IF NOT EXISTS idx_report_view_viewed_at  ON alla.report_view(viewed_at);
CREATE INDEX IF NOT EXISTS idx_report_view_project_id ON alla.report_view(project_id);
CREATE INDEX IF NOT EXISTS idx_report_view_filename   ON alla.report_view(filename);
CREATE UNIQUE INDEX IF NOT EXISTS idx_report_view_unique_per_viewer
    ON alla.report_view(filename, viewer_id) WHERE viewer_id IS NOT NULL;
"""

_REPORT_TABLE_EXISTS_SQL = "SELECT to_regclass(%s)"

# Дедуп: при существующей паре (filename, viewer_id) INSERT молча игнорируется
# благодаря частичному UNIQUE-индексу. Если viewer_id IS NULL (нет cookie /
# легаси), индекс не применяется, запись всё равно добавляется.
_RECORD_VIEW_WITH_REPORT_SQL = """\
INSERT INTO alla.report_view (filename, project_id, launch_id, viewer_id)
SELECT %(filename)s,
       COALESCE(%(project_id)s, r.project_id),
       COALESCE(%(launch_id)s,  r.launch_id),
       %(viewer_id)s
FROM (SELECT %(filename)s::text AS f) k
LEFT JOIN alla.report r ON r.filename = k.f
ON CONFLICT (filename, viewer_id) WHERE viewer_id IS NOT NULL DO NOTHING;
"""

_RECORD_VIEW_SQL = """\
INSERT INTO alla.report_view (filename, project_id, launch_id, viewer_id)
VALUES (%(filename)s, %(project_id)s, %(launch_id)s, %(viewer_id)s)
ON CONFLICT (filename, viewer_id) WHERE viewer_id IS NOT NULL DO NOTHING;
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
        analysis_duration_ms: int | None = None,
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
                    "llm_prompt_tokens, llm_completion_tokens, llm_total_tokens, "
                    "analysis_duration_ms"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (filename) DO UPDATE "
                    "SET html = EXCLUDED.html, "
                    "project_id = EXCLUDED.project_id, "
                    "llm_prompt_tokens = EXCLUDED.llm_prompt_tokens, "
                    "llm_completion_tokens = EXCLUDED.llm_completion_tokens, "
                    "llm_total_tokens = EXCLUDED.llm_total_tokens, "
                    "analysis_duration_ms = EXCLUDED.analysis_duration_ms",
                    (
                        filename,
                        launch_id,
                        html,
                        project_id,
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        analysis_duration_ms,
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


class PostgresReportViewStore:
    """Append-only учёт успешных открытий HTML-отчётов."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._report_table_known: bool | None = None
        self._ensure_table()

    def _ensure_table(self) -> None:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_ENSURE_VIEW_TABLE_SQL)
            conn.commit()
        logger.info("alla.report_view table ready")

    def _report_table_exists(self, cur: Any) -> bool:
        if self._report_table_known is True:
            return True

        cur.execute(_REPORT_TABLE_EXISTS_SQL, ("alla.report",))
        row = cur.fetchone()
        exists = row is not None and row[0] is not None
        self._report_table_known = exists
        return exists

    def record_view(
        self,
        filename: str,
        *,
        project_id: int | None = None,
        launch_id: int | None = None,
        viewer_id: str | None = None,
    ) -> None:
        """Записать успешное открытие отчёта, не ломая caller при ошибке БД.

        Если ``viewer_id`` передан, повторное открытие того же отчёта тем же
        пользователем (включая перезагрузку страницы) не добавит новую строку
        благодаря частичному UNIQUE-индексу ``idx_report_view_unique_per_viewer``.
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    params = {
                        "filename": filename,
                        "project_id": project_id,
                        "launch_id": launch_id,
                        "viewer_id": viewer_id,
                    }
                    sql = (
                        _RECORD_VIEW_WITH_REPORT_SQL
                        if self._report_table_exists(cur)
                        else _RECORD_VIEW_SQL
                    )
                    cur.execute(sql, params)
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - учёт просмотров best-effort
            logger.warning("report_view recording failed for %s: %s", filename, exc)
