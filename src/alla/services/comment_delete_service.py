"""Сервис удаления комментариев alla из Allure TestOps."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from alla.clients.base import CommentManager

logger = logging.getLogger(__name__)

ALLA_COMMENT_PREFIX = "[alla]"


@dataclass(frozen=True)
class DeleteCommentsResult:
    """Результат операции удаления комментариев alla."""

    total_test_cases: int
    comments_found: int
    comments_deleted: int
    comments_failed: int
    skipped_test_cases: int


class CommentDeleteService:
    """Удаляет комментарии alla из Allure TestOps.

    Сканирует комментарии к тест-кейсам, фильтрует по префиксу ``[alla]``
    в теле комментария и удаляет через ``DELETE /api/comment/{id}``.
    Дедупликация по test_case_id, per-test error resilience.
    """

    def __init__(
        self,
        client: CommentManager,
        *,
        concurrency: int = 10,
    ) -> None:
        self._client = client
        self._concurrency = concurrency

    async def delete_alla_comments(
        self,
        test_case_ids: set[int],
        *,
        dry_run: bool = False,
    ) -> DeleteCommentsResult:
        """Найти и удалить все комментарии alla для указанных тест-кейсов.

        Args:
            test_case_ids: Множество ID тест-кейсов для сканирования.
            dry_run: Если True — только подсчитать, не удалять.

        Returns:
            DeleteCommentsResult со статистикой.
        """
        if not test_case_ids:
            return DeleteCommentsResult(
                total_test_cases=0,
                comments_found=0,
                comments_deleted=0,
                comments_failed=0,
                skipped_test_cases=0,
            )

        logger.info(
            "Сканирование комментариев для %d тест-кейсов (dry_run=%s)",
            len(test_case_ids),
            dry_run,
        )

        # Фаза 1: собрать alla-комментарии для всех test_case_ids
        semaphore = asyncio.Semaphore(self._concurrency)
        alla_comment_ids: list[int] = []
        scan_errors = 0

        async def scan_one(tc_id: int) -> list[int] | None:
            """Вернуть ID alla-комментариев или None при ошибке."""
            async with semaphore:
                try:
                    comments = await self._client.get_comments(tc_id)
                    return [
                        c.id
                        for c in comments
                        if c.body and c.body.lstrip().startswith(ALLA_COMMENT_PREFIX)
                    ]
                except Exception as exc:
                    logger.warning(
                        "Не удалось получить комментарии для тест-кейса %d: %s",
                        tc_id,
                        exc,
                    )
                    return None

        scan_tasks = [scan_one(tc_id) for tc_id in test_case_ids]
        scan_results = await asyncio.gather(*scan_tasks)

        for ids in scan_results:
            if ids is None:
                scan_errors += 1
            else:
                alla_comment_ids.extend(ids)

        if scan_errors:
            logger.warning(
                "Не удалось просканировать %d тест-кейсов из %d",
                scan_errors,
                len(test_case_ids),
            )

        logger.info(
            "Найдено %d комментариев alla в %d тест-кейсах",
            len(alla_comment_ids),
            len(test_case_ids),
        )

        if dry_run or not alla_comment_ids:
            return DeleteCommentsResult(
                total_test_cases=len(test_case_ids),
                comments_found=len(alla_comment_ids),
                comments_deleted=0,
                comments_failed=0,
                skipped_test_cases=scan_errors,
            )

        # Фаза 2: удалить найденные комментарии
        deleted = 0
        failed = 0

        async def delete_one(comment_id: int) -> bool:
            async with semaphore:
                try:
                    await self._client.delete_comment(comment_id)
                    return True
                except Exception as exc:
                    logger.warning(
                        "Не удалось удалить комментарий %d: %s",
                        comment_id,
                        exc,
                    )
                    return False

        delete_tasks = [delete_one(cid) for cid in alla_comment_ids]
        delete_results = await asyncio.gather(*delete_tasks)

        for success in delete_results:
            if success:
                deleted += 1
            else:
                failed += 1

        logger.info(
            "Удаление завершено. Удалено: %d, ошибок: %d",
            deleted,
            failed,
        )

        return DeleteCommentsResult(
            total_test_cases=len(test_case_ids),
            comments_found=len(alla_comment_ids),
            comments_deleted=deleted,
            comments_failed=failed,
            skipped_test_cases=scan_errors,
        )
