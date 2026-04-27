"""Агрегационные запросы PostgreSQL для дашборда использования.

Все запросы фильтруют строки по ``created_at >= now() - interval days``,
чтобы окно выбора (30/60/90 дней) применялось ко всем метрикам единообразно.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


_KPI_SQL = """\
SELECT
  (SELECT COUNT(*) FROM alla.report
     WHERE created_at >= now() - make_interval(days => %(days)s))                                            AS total_reports,
  (SELECT COUNT(*) FROM alla.kb_entry
     WHERE created_at >= now() - make_interval(days => %(days)s))                                            AS total_kb_entries,
  (SELECT COUNT(*) FROM alla.kb_feedback
     WHERE created_at >= now() - make_interval(days => %(days)s) AND vote = 'like')                          AS total_likes,
  (SELECT COUNT(*) FROM alla.kb_feedback
     WHERE created_at >= now() - make_interval(days => %(days)s) AND vote = 'dislike')                       AS total_dislikes,
  (SELECT COUNT(*) FROM alla.merge_rules
     WHERE created_at >= now() - make_interval(days => %(days)s))                                            AS total_merge_rules,
  (SELECT COUNT(DISTINCT project_id) FROM alla.report
     WHERE created_at >= now() - make_interval(days => %(days)s) AND project_id IS NOT NULL)                 AS active_projects;
"""


_PER_PROJECT_SQL = """\
WITH r AS (
  SELECT project_id, COUNT(*) AS reports, MAX(created_at) AS last_report
  FROM alla.report
  WHERE created_at >= now() - make_interval(days => %(days)s)
  GROUP BY project_id
),
k AS (
  SELECT project_id, COUNT(*) AS kb_entries, MAX(created_at) AS last_kb
  FROM alla.kb_entry
  WHERE created_at >= now() - make_interval(days => %(days)s)
  GROUP BY project_id
),
fb AS (
  SELECT e.project_id,
         SUM((f.vote = 'like')::int)    AS likes,
         SUM((f.vote = 'dislike')::int) AS dislikes,
         MAX(f.created_at)              AS last_feedback
  FROM alla.kb_feedback f
  JOIN alla.kb_entry e ON e.entry_id = f.kb_entry_id
  WHERE f.created_at >= now() - make_interval(days => %(days)s)
  GROUP BY e.project_id
),
mr AS (
  SELECT project_id, COUNT(*) AS merge_rules, MAX(created_at) AS last_merge
  FROM alla.merge_rules
  WHERE created_at >= now() - make_interval(days => %(days)s)
  GROUP BY project_id
),
ids AS (
  SELECT project_id FROM r UNION
  SELECT project_id FROM k UNION
  SELECT project_id FROM fb UNION
  SELECT project_id FROM mr
)
SELECT ids.project_id,
       COALESCE(r.reports, 0)      AS reports,
       COALESCE(k.kb_entries, 0)   AS kb_entries,
       COALESCE(fb.likes, 0)       AS likes,
       COALESCE(fb.dislikes, 0)    AS dislikes,
       COALESCE(mr.merge_rules, 0) AS merge_rules,
       GREATEST(
         COALESCE(r.last_report,    'epoch'::timestamptz),
         COALESCE(k.last_kb,        'epoch'::timestamptz),
         COALESCE(fb.last_feedback, 'epoch'::timestamptz),
         COALESCE(mr.last_merge,    'epoch'::timestamptz)
       ) AS last_activity
FROM ids
LEFT JOIN r  USING (project_id)
LEFT JOIN k  USING (project_id)
LEFT JOIN fb USING (project_id)
LEFT JOIN mr USING (project_id)
ORDER BY reports DESC, kb_entries DESC;
"""


_REPORTS_PER_DAY_SQL = """\
SELECT date_trunc('day', created_at)::date AS day, COUNT(*) AS n
FROM alla.report
WHERE created_at >= now() - make_interval(days => %(days)s)
GROUP BY 1 ORDER BY 1;
"""


def gap_fill_series(
    rows: list[tuple[date, int]],
    *,
    days: int,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Заполнить пропущенные дни нулями за последние ``days`` дней.

    Возвращает список ``{"day": "YYYY-MM-DD", "n": int}`` длиной ровно ``days``.
    Pure-функция — БД не нужна, удобно тестировать.
    """
    if today is None:
        today = datetime.now(tz=timezone.utc).date()
    by_day: dict[date, int] = {row[0]: int(row[1]) for row in rows}
    out: list[dict[str, Any]] = []
    start = today - timedelta(days=days - 1)
    for offset in range(days):
        d = start + timedelta(days=offset)
        out.append({"day": d.isoformat(), "n": by_day.get(d, 0)})
    return out


class DashboardStatsStore:
    """Агрегации PostgreSQL для эндпоинта ``/api/v1/dashboard/stats``."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def totals(self, *, days: int) -> dict[str, int]:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_KPI_SQL, {"days": days})
                row = cur.fetchone()
        if row is None:
            return {
                "total_reports": 0,
                "total_kb_entries": 0,
                "total_likes": 0,
                "total_dislikes": 0,
                "total_merge_rules": 0,
                "active_projects": 0,
            }
        return {
            "total_reports": int(row[0] or 0),
            "total_kb_entries": int(row[1] or 0),
            "total_likes": int(row[2] or 0),
            "total_dislikes": int(row[3] or 0),
            "total_merge_rules": int(row[4] or 0),
            "active_projects": int(row[5] or 0),
        }

    def per_project_rollup(self, *, days: int) -> list[dict[str, Any]]:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_PER_PROJECT_SQL, {"days": days})
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            project_id, reports, kb_entries, likes, dislikes, merge_rules, last_activity = row
            last_iso: str | None = None
            if last_activity is not None and last_activity.year > 1970:
                last_iso = last_activity.isoformat()
            out.append({
                "project_id": int(project_id) if project_id is not None else None,
                "reports": int(reports or 0),
                "kb_entries": int(kb_entries or 0),
                "likes": int(likes or 0),
                "dislikes": int(dislikes or 0),
                "merge_rules": int(merge_rules or 0),
                "last_activity": last_iso,
            })
        return out

    def reports_per_day(self, *, days: int) -> list[dict[str, Any]]:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_REPORTS_PER_DAY_SQL, {"days": days})
                rows = cur.fetchall()
        return gap_fill_series(rows, days=days)
