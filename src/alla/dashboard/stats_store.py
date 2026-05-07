"""Агрегационные запросы PostgreSQL для дашборда использования.

Все запросы фильтруют строки по полузакрытому интервалу
``[window.start_ts, window.end_ts)``, чтобы окно выбора (последние N дней или
конкретный календарный день) применялось ко всем метрикам единообразно.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DateWindow:
    """Полузакрытый интервал ``[start_ts, end_ts)`` для агрегаций дашборда."""

    start_ts: datetime
    end_ts: datetime
    kind: str  # 'days' | 'day'
    days_value: int | None = None
    day_value: date | None = None

    @classmethod
    def from_days(cls, days: int, *, now: datetime | None = None) -> "DateWindow":
        if days <= 0:
            days = 1
        end = now or datetime.now(tz=timezone.utc)
        start = end - timedelta(days=days)
        return cls(start_ts=start, end_ts=end, kind="days", days_value=days)

    @classmethod
    def from_day(cls, d: date, *, tz: timezone | None = None) -> "DateWindow":
        tzinfo = tz or timezone.utc
        start = datetime.combine(d, time.min, tzinfo=tzinfo)
        end = start + timedelta(days=1)
        return cls(start_ts=start, end_ts=end, kind="day", day_value=d)

    def series_dates(self, *, today: date | None = None) -> list[date]:
        """Календарные даты для отрисовки series-чарта."""
        if self.kind == "day" and self.day_value is not None:
            return [self.day_value]
        days = self.days_value or 1
        anchor = today or datetime.now(tz=timezone.utc).date()
        start = anchor - timedelta(days=days - 1)
        return [start + timedelta(days=offset) for offset in range(days)]

    def descriptor(self) -> dict[str, Any]:
        if self.kind == "day" and self.day_value is not None:
            return {"kind": "day", "value": self.day_value.isoformat()}
        return {"kind": "days", "value": int(self.days_value or 0)}


_KPI_SQL = """\
WITH peak AS (
  SELECT date_trunc('day', created_at)::date AS d, COUNT(*) AS c
  FROM alla.report
  WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s
  GROUP BY 1
  ORDER BY c DESC, d DESC
  LIMIT 1
)
SELECT
  (SELECT COUNT(*) FROM alla.report
     WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s)                                            AS total_reports,
  (SELECT COUNT(*) FROM alla.kb_entry
     WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s)                                            AS total_kb_entries,
  (SELECT COUNT(*) FROM alla.merge_rules
     WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s)                                            AS total_merge_rules,
  (SELECT COUNT(DISTINCT project_id) FROM alla.report
     WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s AND project_id IS NOT NULL)                 AS active_projects,
  (SELECT COUNT(DISTINCT launch_id) FROM alla.report
     WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s)                                            AS unique_launches,
  (SELECT COALESCE(SUM(llm_total_tokens), 0) FROM alla.report
     WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s
       AND llm_total_tokens IS NOT NULL)                                                                      AS llm_total_tokens,
  (SELECT COALESCE(ROUND(AVG(llm_total_tokens))::bigint, 0) FROM alla.report
     WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s
       AND llm_total_tokens IS NOT NULL)                                                                      AS llm_avg_tokens_per_run,
  (SELECT COUNT(llm_total_tokens) FROM alla.report
     WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s)                                            AS llm_reports_with_usage,
  (SELECT ROUND(AVG(analysis_duration_ms))::bigint FROM alla.report
     WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s
       AND analysis_duration_ms IS NOT NULL)                                                                  AS avg_analysis_duration_ms,
  (SELECT d FROM peak)                                                                                        AS peak_day,
  (SELECT c FROM peak)                                                                                        AS peak_day_count;
"""


_PER_PROJECT_SQL = """\
WITH r AS (
  SELECT project_id,
         COUNT(*) AS reports,
         SUM(llm_total_tokens) FILTER (WHERE llm_total_tokens IS NOT NULL) AS llm_total_tokens,
         ROUND(AVG(llm_total_tokens))::bigint AS llm_avg_tokens_per_run,
         COUNT(llm_total_tokens) AS llm_reports_with_usage,
         ROUND(AVG(analysis_duration_ms))::bigint AS avg_analysis_duration_ms,
         MAX(created_at) AS last_report
  FROM alla.report
  WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s
  GROUP BY project_id
),
k AS (
  SELECT project_id, COUNT(*) AS kb_entries, MAX(created_at) AS last_kb
  FROM alla.kb_entry
  WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s
  GROUP BY project_id
),
mr AS (
  SELECT project_id, COUNT(*) AS merge_rules, MAX(created_at) AS last_merge
  FROM alla.merge_rules
  WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s
  GROUP BY project_id
),
ids AS (
  SELECT project_id FROM r UNION
  SELECT project_id FROM k UNION
  SELECT project_id FROM mr
)
SELECT ids.project_id,
       COALESCE(r.reports, 0)      AS reports,
       COALESCE(k.kb_entries, 0)   AS kb_entries,
       COALESCE(mr.merge_rules, 0) AS merge_rules,
       COALESCE(r.llm_total_tokens, 0)        AS llm_total_tokens,
       COALESCE(r.llm_avg_tokens_per_run, 0)  AS llm_avg_tokens_per_run,
       COALESCE(r.llm_reports_with_usage, 0)  AS llm_reports_with_usage,
       r.avg_analysis_duration_ms             AS avg_analysis_duration_ms,
       GREATEST(
         COALESCE(r.last_report,    'epoch'::timestamptz),
         COALESCE(k.last_kb,        'epoch'::timestamptz),
         COALESCE(mr.last_merge,    'epoch'::timestamptz)
       ) AS last_activity
FROM ids
LEFT JOIN r  ON ids.project_id IS NOT DISTINCT FROM r.project_id
LEFT JOIN k  ON ids.project_id IS NOT DISTINCT FROM k.project_id
LEFT JOIN mr ON ids.project_id IS NOT DISTINCT FROM mr.project_id
ORDER BY reports DESC, kb_entries DESC;
"""


_REPORTS_PER_DAY_SQL = """\
SELECT date_trunc('day', created_at)::date AS day, COUNT(*) AS n
FROM alla.report
WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s
GROUP BY 1 ORDER BY 1;
"""


_REPORTS_FOR_PROJECT_SQL = """\
SELECT filename, launch_id, created_at, llm_total_tokens, analysis_duration_ms
FROM alla.report
WHERE created_at >= %(start_ts)s AND created_at < %(end_ts)s
  AND project_id IS NOT DISTINCT FROM %(project_id)s
ORDER BY created_at DESC
LIMIT %(limit)s;
"""


def gap_fill_series(
    rows: list[tuple[date, int]],
    *,
    series_dates: list[date],
) -> list[dict[str, Any]]:
    """Заполнить пропущенные дни нулями для заданного списка календарных дат."""
    by_day: dict[date, int] = {row[0]: int(row[1]) for row in rows}
    return [{"day": d.isoformat(), "n": by_day.get(d, 0)} for d in series_dates]


class DashboardStatsStore:
    """Агрегации PostgreSQL для эндпоинта ``/api/v1/dashboard/stats``."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _params(self, window: DateWindow) -> dict[str, Any]:
        return {"start_ts": window.start_ts, "end_ts": window.end_ts}

    def totals(self, *, window: DateWindow) -> dict[str, Any]:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_KPI_SQL, self._params(window))
                row = cur.fetchone()
        if row is None:
            return {
                "total_reports": 0,
                "total_kb_entries": 0,
                "total_merge_rules": 0,
                "active_projects": 0,
                "unique_launches": 0,
                "llm_total_tokens": 0,
                "llm_avg_tokens_per_run": 0,
                "llm_reports_with_usage": 0,
                "avg_analysis_duration_ms": None,
                "peak_day": None,
                "peak_day_count": 0,
            }
        peak_day_value = row[9].isoformat() if row[9] is not None else None
        return {
            "total_reports": int(row[0] or 0),
            "total_kb_entries": int(row[1] or 0),
            "total_merge_rules": int(row[2] or 0),
            "active_projects": int(row[3] or 0),
            "unique_launches": int(row[4] or 0),
            "llm_total_tokens": int(row[5] or 0),
            "llm_avg_tokens_per_run": int(row[6] or 0),
            "llm_reports_with_usage": int(row[7] or 0),
            "avg_analysis_duration_ms": int(row[8]) if row[8] is not None else None,
            "peak_day": peak_day_value,
            "peak_day_count": int(row[10] or 0),
        }

    def per_project_rollup(self, *, window: DateWindow) -> list[dict[str, Any]]:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_PER_PROJECT_SQL, self._params(window))
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            (
                project_id,
                reports,
                kb_entries,
                merge_rules,
                llm_total_tokens,
                llm_avg_tokens_per_run,
                llm_reports_with_usage,
                avg_analysis_duration_ms,
                last_activity,
            ) = row
            last_iso: str | None = None
            if last_activity is not None and last_activity.year > 1970:
                last_iso = last_activity.isoformat()
            out.append({
                "project_id": int(project_id) if project_id is not None else None,
                "reports": int(reports or 0),
                "kb_entries": int(kb_entries or 0),
                "merge_rules": int(merge_rules or 0),
                "llm_total_tokens": int(llm_total_tokens or 0),
                "llm_avg_tokens_per_run": int(llm_avg_tokens_per_run or 0),
                "llm_reports_with_usage": int(llm_reports_with_usage or 0),
                "avg_analysis_duration_ms": (
                    int(avg_analysis_duration_ms)
                    if avg_analysis_duration_ms is not None
                    else None
                ),
                "last_activity": last_iso,
            })
        return out

    def reports_per_day(self, *, window: DateWindow) -> list[dict[str, Any]]:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_REPORTS_PER_DAY_SQL, self._params(window))
                rows = cur.fetchall()
        return gap_fill_series(rows, series_dates=window.series_dates())

    def reports_for_project(
        self,
        *,
        project_id: int | None,
        window: DateWindow,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        params = {
            **self._params(window),
            "project_id": project_id,
            "limit": int(limit),
        }
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_REPORTS_FOR_PROJECT_SQL, params)
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            filename, launch_id, created_at, llm_total_tokens, analysis_duration_ms = row
            out.append({
                "filename": str(filename),
                "launch_id": int(launch_id) if launch_id is not None else None,
                "created_at": created_at.isoformat() if created_at is not None else None,
                "llm_total_tokens": (
                    int(llm_total_tokens) if llm_total_tokens is not None else None
                ),
                "analysis_duration_ms": (
                    int(analysis_duration_ms)
                    if analysis_duration_ms is not None
                    else None
                ),
            })
        return out
