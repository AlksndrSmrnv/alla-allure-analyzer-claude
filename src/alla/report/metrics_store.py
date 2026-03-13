"""PostgreSQL storage for report usage metrics.

Таблица ``alla.report_event`` хранит события использования HTML-отчётов
(открытие, просмотр, взаимодействия).  Используется тот же DSN, что и для
KB (``ALLURE_KB_POSTGRES_DSN``).

Миграция (выполняется автоматически при первом подключении)::

    CREATE SCHEMA IF NOT EXISTS alla;
    CREATE TABLE IF NOT EXISTS alla.report_event (
        id              BIGSERIAL    PRIMARY KEY,
        session_id      TEXT         NOT NULL,
        launch_id       INTEGER      NOT NULL,
        report_filename TEXT,
        project_id      INTEGER,
        event           TEXT         NOT NULL,
        event_ts        TIMESTAMPTZ  NOT NULL,
        meta            JSONB        NOT NULL DEFAULT '{}',
        received_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
    );
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

_ENSURE_TABLE_SQL = """\
CREATE SCHEMA IF NOT EXISTS alla;
CREATE TABLE IF NOT EXISTS alla.report_event (
    id              BIGSERIAL    PRIMARY KEY,
    session_id      TEXT         NOT NULL,
    launch_id       INTEGER      NOT NULL,
    report_filename TEXT,
    project_id      INTEGER,
    event           TEXT         NOT NULL,
    event_ts        TIMESTAMPTZ  NOT NULL,
    meta            JSONB        NOT NULL DEFAULT '{}',
    received_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_report_event_launch
    ON alla.report_event(launch_id);
CREATE INDEX IF NOT EXISTS idx_report_event_received
    ON alla.report_event(received_at);
CREATE INDEX IF NOT EXISTS idx_report_event_session
    ON alla.report_event(session_id);
"""

ALLOWED_EVENTS = frozenset({
    "report_open",
    "report_viewed",
    "scroll_depth",
    "cluster_expand",
    "link_click",
    "copy_text",
    "feedback_interaction",
})


class PostgresMetricsStore:
    """Save / query report usage events in PostgreSQL."""

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
        logger.info("alla.report_event table ready")

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record_events(
        self,
        session_id: str,
        launch_id: int,
        events: list[dict[str, Any]],
        *,
        report_filename: str | None = None,
        project_id: int | None = None,
    ) -> int:
        """Insert a batch of events.  Returns count of accepted rows."""
        if not events:
            return 0

        rows: list[tuple[Any, ...]] = []
        for ev in events:
            event_name = ev.get("event", "")
            if event_name not in ALLOWED_EVENTS:
                continue
            ts = ev.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            event_ts = datetime.fromtimestamp(ts, tz=timezone.utc)
            meta = ev.get("meta") or {}
            rows.append((
                session_id,
                launch_id,
                report_filename,
                project_id,
                event_name,
                event_ts,
                psycopg.types.json.Json(meta),
            ))

        if not rows:
            return 0

        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO alla.report_event "
                    "(session_id, launch_id, report_filename, project_id, "
                    " event, event_ts, meta) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    rows,
                )
            conn.commit()

        return len(rows)

    # ------------------------------------------------------------------
    # Read — per-launch
    # ------------------------------------------------------------------

    def get_launch_metrics(self, launch_id: int) -> dict[str, Any]:
        """Aggregate metrics for a single launch."""
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                # Events by type
                cur.execute(
                    "SELECT event, count(*) AS cnt "
                    "FROM alla.report_event WHERE launch_id = %s "
                    "GROUP BY event",
                    (launch_id,),
                )
                events_by_type: dict[str, int] = {
                    r["event"]: r["cnt"] for r in cur.fetchall()
                }

                # Session-level stats
                cur.execute(
                    "SELECT session_id, "
                    "  min(event_ts) AS first_ts, "
                    "  max(event_ts) AS last_ts, "
                    "  bool_or(event = 'report_viewed') AS viewed, "
                    "  bool_or(event IN ("
                    "    'scroll_depth','cluster_expand','link_click',"
                    "    'copy_text','feedback_interaction'"
                    "  )) AS interacted "
                    "FROM alla.report_event WHERE launch_id = %s "
                    "GROUP BY session_id",
                    (launch_id,),
                )
                sessions = cur.fetchall()

                total_sessions = len(sessions)
                sessions_viewed = sum(1 for s in sessions if s["viewed"])
                sessions_interacted = sum(1 for s in sessions if s["interacted"])

                durations = []
                for s in sessions:
                    if s["first_ts"] and s["last_ts"]:
                        d = (s["last_ts"] - s["first_ts"]).total_seconds()
                        if d > 0:
                            durations.append(d)

                avg_duration = (
                    round(sum(durations) / len(durations), 1)
                    if durations
                    else 0.0
                )

                # Referrers from report_open meta
                cur.execute(
                    "SELECT meta->>'referrer' AS ref, count(*) AS cnt "
                    "FROM alla.report_event "
                    "WHERE launch_id = %s AND event = 'report_open' "
                    "  AND meta->>'referrer' IS NOT NULL "
                    "GROUP BY meta->>'referrer'",
                    (launch_id,),
                )
                referrers: dict[str, int] = {
                    r["ref"]: r["cnt"] for r in cur.fetchall()
                }

                # Feedback actions (from alla.kb_feedback, no duplication)
                feedback_actions = self._get_feedback_actions(cur, launch_id)

        return {
            "launch_id": launch_id,
            "total_sessions": total_sessions,
            "sessions_viewed": sessions_viewed,
            "sessions_interacted": sessions_interacted,
            "avg_session_duration_sec": avg_duration,
            "events_by_type": events_by_type,
            "referrers": referrers,
            "feedback_actions": feedback_actions,
        }

    # ------------------------------------------------------------------
    # Read — per-project
    # ------------------------------------------------------------------

    def get_project_metrics(
        self,
        project_id: int,
        days: int = 30,
    ) -> dict[str, Any]:
        """Aggregate metrics for a project over a time period."""
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                # Launches with at least one report_open event
                cur.execute(
                    "SELECT count(DISTINCT launch_id) AS cnt "
                    "FROM alla.report_event "
                    "WHERE project_id = %s "
                    "  AND event = 'report_open' "
                    "  AND received_at >= now() - make_interval(days => %s)",
                    (project_id, days),
                )
                row = cur.fetchone()
                launches_opened = row["cnt"] if row else 0

                # Session-level stats
                cur.execute(
                    "SELECT session_id, "
                    "  bool_or(event = 'report_viewed') AS viewed, "
                    "  bool_or(event IN ("
                    "    'scroll_depth','cluster_expand','link_click',"
                    "    'copy_text','feedback_interaction'"
                    "  )) AS interacted "
                    "FROM alla.report_event "
                    "WHERE project_id = %s "
                    "  AND received_at >= now() - make_interval(days => %s) "
                    "GROUP BY session_id",
                    (project_id, days),
                )
                sessions = cur.fetchall()

                total_sessions = len(sessions)
                sessions_interacted = sum(1 for s in sessions if s["interacted"])
                engagement_rate = (
                    round(sessions_interacted / total_sessions, 2)
                    if total_sessions > 0
                    else 0.0
                )

                # Daily breakdown
                cur.execute(
                    "SELECT date_trunc('day', received_at)::date AS day, "
                    "  count(DISTINCT session_id) AS sessions, "
                    "  count(DISTINCT session_id) FILTER ("
                    "    WHERE event = 'report_viewed'"
                    "  ) AS viewed, "
                    "  count(DISTINCT session_id) FILTER ("
                    "    WHERE event IN ("
                    "      'scroll_depth','cluster_expand','link_click',"
                    "      'copy_text','feedback_interaction'"
                    "    )"
                    "  ) AS interacted "
                    "FROM alla.report_event "
                    "WHERE project_id = %s "
                    "  AND received_at >= now() - make_interval(days => %s) "
                    "GROUP BY day ORDER BY day DESC",
                    (project_id, days),
                )
                daily = [
                    {
                        "date": str(r["day"]),
                        "sessions": r["sessions"],
                        "viewed": r["viewed"],
                        "interacted": r["interacted"],
                    }
                    for r in cur.fetchall()
                ]

        return {
            "project_id": project_id,
            "period_days": days,
            "launches_opened": launches_opened,
            "total_sessions": total_sessions,
            "engagement_rate": engagement_rate,
            "daily_sessions": daily,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_feedback_actions(
        cur: psycopg.Cursor[Any],
        launch_id: int,
    ) -> dict[str, int]:
        """Count feedback actions from alla.kb_feedback for the launch."""
        try:
            cur.execute(
                "SELECT "
                "  count(*) FILTER (WHERE vote = 'like') AS likes, "
                "  count(*) FILTER (WHERE vote = 'dislike') AS dislikes "
                "FROM alla.kb_feedback WHERE launch_id = %s",
                (launch_id,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "likes": row["likes"],
                    "dislikes": row["dislikes"],
                }
        except psycopg.errors.UndefinedTable:
            # kb_feedback table may not exist if feedback is not configured
            pass
        return {"likes": 0, "dislikes": 0}
