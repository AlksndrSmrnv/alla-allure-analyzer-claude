"""Сервис записи рекомендаций KB обратно в Allure TestOps через комментарии."""

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
    """Отформатировать KB-совпадения в текст комментария.

    Args:
        matches: Список KB-совпадений для одного кластера.

    Returns:
        Отформатированный текст комментария.
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
    """Записывает рекомендации KB обратно в Allure TestOps через комментарии.

    Добавляет комментарий (``POST /api/comment``) к каждому уникальному
    тест-кейсу, входящему в кластер с KB-совпадениями. Дедупликация:
    один комментарий на уникальный test_case_id в рамках кластера.
    Тесты без test_case_id пропускаются. Ошибки отдельных
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
        """Добавить комментарии для всех тест-кейсов в кластерах с KB-совпадениями.

        Args:
            clustering_report: Отчёт кластеризации.
            kb_results: cluster_id → list[KBMatchResult].
            triage_report: Отчёт триажа (для получения test_case_id).

        Returns:
            KBPushResult со статистикой обновлений.
        """
        # Маппинг test_result_id → test_case_id
        test_case_ids: dict[int, int | None] = {
            t.test_result_id: t.test_case_id for t in triage_report.failed_tests
        }

        # Собрать уникальные (test_case_id, comment_text) для отправки
        comments: dict[int, str] = {}  # test_case_id → comment text
        skipped = 0

        for cluster in clustering_report.clusters:
            matches = kb_results.get(cluster.cluster_id)
            if not matches:
                skipped += len(cluster.member_test_ids)
                continue

            comment_text = format_kb_description(matches)
            if not comment_text:
                skipped += len(cluster.member_test_ids)
                continue

            for test_id in cluster.member_test_ids:
                tc_id = test_case_ids.get(test_id)
                if tc_id is None:
                    logger.warning(
                        "KB push: test_result %d не имеет test_case_id, пропуск",
                        test_id,
                    )
                    skipped += 1
                    continue

                if tc_id in comments:
                    # Дедупликация: этот test_case_id уже запланирован
                    skipped += 1
                    continue

                comments[tc_id] = comment_text

        if not comments:
            logger.info(
                "KB push: нет комментариев для записи "
                "(0 кластеров с KB-совпадениями или нет test_case_id)"
            )
            return KBPushResult(
                total_tests=clustering_report.total_failures,
                updated_count=0,
                failed_count=0,
                skipped_count=skipped,
            )

        logger.info(
            "KB push: отправка комментариев для %d тест-кейсов "
            "(параллелизм=%d)",
            len(comments),
            self._concurrency,
        )

        semaphore = asyncio.Semaphore(self._concurrency)
        updated = 0
        failed = 0

        async def post_one(tc_id: int, text: str) -> bool:
            async with semaphore:
                try:
                    await self._updater.post_comment(tc_id, text)
                    return True
                except Exception as exc:
                    logger.warning(
                        "KB push: не удалось добавить комментарий "
                        "для тест-кейса %d: %s",
                        tc_id,
                        exc,
                    )
                    return False

        tasks = [post_one(tc_id, text) for tc_id, text in comments.items()]
        results = await asyncio.gather(*tasks)

        for success in results:
            if success:
                updated += 1
            else:
                failed += 1

        logger.info(
            "KB push: завершено. Комментариев: %d, ошибок: %d, пропущено: %d",
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
