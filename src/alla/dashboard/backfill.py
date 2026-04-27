"""Бэкфилл колонки ``alla.report.project_id`` для существующих строк.

После миграции колонка ``project_id`` пустая для всех ранее сохранённых
отчётов. Скрипт собирает уникальные ``launch_id`` без проекта, тянет
``project_id`` из TestOps (``GET /api/launch/{id}``) и обновляет строки
батчем. Идемпотентен — повторный запуск пропустит уже заполненные.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import psycopg

from alla.models.testops import LaunchResponse

logger = logging.getLogger(__name__)


@dataclass
class BackfillReport:
    total: int = 0      # сколько уникальных launch_id найдено
    resolved: int = 0   # сколько успешно получили project_id
    skipped: int = 0    # сколько лончей не имеют project_id (например, удалены)
    failed: int = 0     # сколько вызовов TestOps упали с ошибкой
    rows_updated: int = 0  # количество строк alla.report, которые были обновлены

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "resolved": self.resolved,
            "skipped": self.skipped,
            "failed": self.failed,
            "rows_updated": self.rows_updated,
        }


class _LaunchFetcher(Protocol):
    async def get_launch(self, launch_id: int) -> LaunchResponse: ...


def _list_unattributed_launch_ids(dsn: str, *, limit: int | None = None) -> list[int]:
    sql = (
        "SELECT DISTINCT launch_id FROM alla.report "
        "WHERE project_id IS NULL ORDER BY launch_id"
    )
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [int(row[0]) for row in cur.fetchall()]


def _apply_updates(dsn: str, mapping: dict[int, int]) -> int:
    if not mapping:
        return 0
    rows = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for launch_id, project_id in mapping.items():
                cur.execute(
                    "UPDATE alla.report SET project_id = %s "
                    "WHERE launch_id = %s AND project_id IS NULL",
                    (project_id, launch_id),
                )
                rows += cur.rowcount or 0
        conn.commit()
    return rows


async def backfill_report_projects(
    client: _LaunchFetcher,
    *,
    dsn: str,
    concurrency: int = 5,
    dry_run: bool = False,
    limit: int | None = None,
) -> BackfillReport:
    """Заполнить ``alla.report.project_id`` для строк, где он NULL."""
    launch_ids = _list_unattributed_launch_ids(dsn, limit=limit)
    report = BackfillReport(total=len(launch_ids))
    if not launch_ids:
        logger.info("Backfill: записей без project_id не найдено")
        return report

    semaphore = asyncio.Semaphore(max(1, concurrency))
    mapping: dict[int, int] = {}

    async def fetch(launch_id: int) -> None:
        async with semaphore:
            try:
                launch = await client.get_launch(launch_id)
            except Exception as exc:  # noqa: BLE001 - сетевые/HTTP ошибки разные
                logger.warning("Backfill: не удалось получить launch %d: %s", launch_id, exc)
                report.failed += 1
                return
            if launch.project_id is None:
                logger.info("Backfill: launch %d без project_id, пропускаем", launch_id)
                report.skipped += 1
                return
            mapping[launch_id] = int(launch.project_id)
            report.resolved += 1

    await asyncio.gather(*(fetch(lid) for lid in launch_ids))

    if dry_run:
        logger.info(
            "Backfill (dry-run): нашли %d, разрешили %d, пропустили %d, ошибок %d",
            report.total, report.resolved, report.skipped, report.failed,
        )
        return report

    rows = _apply_updates(dsn, mapping)
    report.rows_updated = rows
    logger.info(
        "Backfill: обновлено %d строк по %d лончам (пропущено %d, ошибок %d)",
        rows, report.resolved, report.skipped, report.failed,
    )
    return report
