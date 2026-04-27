"""PostgreSQL storage for HTML reports.

Таблица ``alla.report`` хранит self-contained HTML-отчёты.
Используется тот же DSN, что и для KB (``ALLURE_KB_POSTGRES_DSN``).

Миграция (выполняется автоматически при первом подключении)::

    CREATE SCHEMA IF NOT EXISTS alla;
    CREATE TABLE IF NOT EXISTS alla.report (
        id          SERIAL       PRIMARY KEY,
        filename    TEXT         NOT NULL UNIQUE,
        launch_id   INTEGER      NOT NULL,
        html        TEXT         NOT NULL,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_report_launch_id ON alla.report(launch_id);
"""

import logging

import psycopg

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
CREATE INDEX IF NOT EXISTS idx_report_launch_id  ON alla.report(launch_id);
CREATE INDEX IF NOT EXISTS idx_report_project_id ON alla.report(project_id);
CREATE INDEX IF NOT EXISTS idx_report_created_at ON alla.report(created_at);
"""


class PostgresReportStore:
    """Save / load HTML reports in PostgreSQL."""

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
    # Public API
    # ------------------------------------------------------------------

    def save(
        self,
        filename: str,
        launch_id: int,
        html: str,
        project_id: int | None = None,
    ) -> None:
        """Insert a new report. Silently replaces if filename already exists."""
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO alla.report (filename, launch_id, html, project_id) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (filename) DO UPDATE "
                    "SET html = EXCLUDED.html, project_id = EXCLUDED.project_id",
                    (filename, launch_id, html, project_id),
                )
            conn.commit()

    def load(self, filename: str) -> str | None:
        """Return HTML content by filename, or ``None`` if not found."""
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT html FROM alla.report WHERE filename = %s",
                    (filename,),
                )
                row = cur.fetchone()
                return row[0] if row else None

    def list_for_launch(self, launch_id: int) -> list[str]:
        """Return filenames for a given launch, newest first."""
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT filename FROM alla.report "
                    "WHERE launch_id = %s ORDER BY created_at DESC",
                    (launch_id,),
                )
                return [row[0] for row in cur.fetchall()]
