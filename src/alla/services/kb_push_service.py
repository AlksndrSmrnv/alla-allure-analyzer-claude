"""Сервис записи рекомендаций KB обратно в Allure TestOps."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from alla.clients.base import TestResultsUpdater
from alla.knowledge.models import KBMatchResult
from alla.models.clustering import ClusteringReport
from alla.models.testops import TriageReport

logger = logging.getLogger(__name__)

_ALLA_HEADER = "[alla] Рекомендация по результатам анализа"
_SEPARATOR = "=" * 40


@dataclass(frozen=True)
class KBPushResult:
    """Результат операции push KB-рекомендаций."""

    total_tests: int
    updated_count: int
    failed_count: int
    skipped_count: int


def format_kb_description(matches: list[KBMatchResult]) -> str:
    """Отформатировать KB-совпадения в текст для поля description.

    Args:
        matches: Список KB-совпадений для одного кластера.

    Returns:
        Отформатированный текст description.
    """
    if not matches:
        return ""

    parts: list[str] = [_ALLA_HEADER, _SEPARATOR, ""]

    for i, match in enumerate(matches):
        if i > 0:
            parts.append("")
            parts.append("-" * 40)
            parts.append("")

        entry = match.entry
        parts.append(f"KB: {entry.title} (score: {match.score:.2f})")
        parts.append(
            f"Причина: {entry.root_cause.value} | Срочность: {entry.severity.value}"
        )

        if entry.resolution_steps:
            parts.append("")
            parts.append("Шаги по устранению:")
            for step_num, step in enumerate(entry.resolution_steps, 1):
                parts.append(f"  {step_num}. {step}")

        if match.matched_on:
            parts.append("")
            parts.append(f"Совпадение по: {', '.join(match.matched_on)}")

    return "\n".join(parts)


class KBPushService:
    """Записывает рекомендации KB обратно в Allure TestOps.

    Обновляет поле description для каждого результата теста,
    входящего в кластер с KB-совпадениями. Ошибки отдельных
    обновлений не прерывают весь процесс.
    """

    def __init__(
        self,
        updater: TestResultsUpdater,
        *,
        concurrency: int = 10,
    ) -> None:
        self._updater = updater
        self._concurrency = concurrency

    async def push_kb_results(
        self,
        clustering_report: ClusteringReport,
        kb_results: dict[str, list[KBMatchResult]],
        triage_report: TriageReport,
    ) -> KBPushResult:
        """Обновить description для всех тестов в кластерах с KB-совпадениями.

        Args:
            clustering_report: Отчёт кластеризации.
            kb_results: cluster_id → list[KBMatchResult].
            triage_report: Отчёт триажа (для получения имён тестов).

        Returns:
            KBPushResult со статистикой обновлений.
        """
        # Маппинг test_result_id → name (обязательное поле в PATCH)
        test_names: dict[int, str] = {
            t.test_result_id: t.name for t in triage_report.failed_tests
        }

        updates: dict[int, str] = {}
        skipped = 0

        for cluster in clustering_report.clusters:
            matches = kb_results.get(cluster.cluster_id)
            if not matches:
                skipped += len(cluster.member_test_ids)
                continue

            description = format_kb_description(matches)
            if not description:
                skipped += len(cluster.member_test_ids)
                continue

            for test_id in cluster.member_test_ids:
                updates[test_id] = description

        if not updates:
            logger.info(
                "KB push: нет обновлений для записи "
                "(0 кластеров с KB-совпадениями)"
            )
            return KBPushResult(
                total_tests=clustering_report.total_failures,
                updated_count=0,
                failed_count=0,
                skipped_count=skipped,
            )

        logger.info(
            "KB push: обновление description для %d результатов тестов "
            "(параллелизм=%d)",
            len(updates),
            self._concurrency,
        )

        semaphore = asyncio.Semaphore(self._concurrency)
        updated = 0
        failed = 0

        async def update_one(test_id: int, desc: str) -> bool:
            name = test_names.get(test_id, f"test-result-{test_id}")
            async with semaphore:
                try:
                    await self._updater.update_test_result_description(
                        test_id, desc, name=name,
                    )
                    return True
                except Exception as exc:
                    logger.warning(
                        "KB push: не удалось обновить description "
                        "для теста %d: %s",
                        test_id,
                        exc,
                    )
                    return False

        tasks = [update_one(tid, desc) for tid, desc in updates.items()]
        results = await asyncio.gather(*tasks)

        for success in results:
            if success:
                updated += 1
            else:
                failed += 1

        logger.info(
            "KB push: завершено. Обновлено: %d, ошибок: %d, пропущено: %d",
            updated,
            failed,
            skipped,
        )

        return KBPushResult(
            total_tests=clustering_report.total_failures,
            updated_count=updated,
            failed_count=failed,
            skipped_count=skipped,
        )
