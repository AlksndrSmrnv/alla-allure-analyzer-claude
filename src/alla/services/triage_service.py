"""Сервис триажа: получение результатов тестов, фильтрация падений, формирование сводки."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from alla.clients.base import TestResultsProvider
from alla.config import Settings
from alla.models.common import TestStatus
from alla.models.testops import (
    ExecutionStep,
    FailedTestSummary,
    TestResultResponse,
    TriageReport,
)

logger = logging.getLogger(__name__)


class TriageService:
    """Оркестрирует процесс триажа упавших тестов.

    Фаза 1 (MVP): получение и формирование сводки по упавшим тестам.
    Будущие фазы добавят кластеризацию, LLM-анализ, поиск по базе знаний.
    """

    def __init__(self, client: TestResultsProvider, settings: Settings) -> None:
        self._client = client
        self._endpoint = str(settings.endpoint).rstrip("/")
        self._detail_concurrency = settings.detail_concurrency

    async def analyze_launch(self, launch_id: int) -> TriageReport:
        """Получить результаты тестов для запуска и сформировать отчёт триажа.

        Шаги:
            1. Получить метаданные запуска (имя, статус закрытия).
            2. Получить все результаты тестов для запуска (пагинация).
            3. Подсчитать результаты по статусам.
            4. Создать FailedTestSummary для каждого упавшего/сломанного теста.
            5. Вернуть TriageReport.
        """
        # 1. Метаданные запуска
        launch = await self._client.get_launch(launch_id)
        logger.info("Analyzing launch #%d (%s)", launch_id, launch.name or "unnamed")

        # 2. Все результаты тестов
        results = await self._client.get_all_test_results_for_launch(launch_id)

        # 3. Подсчёт по статусам
        status_counts = Counter(
            self._normalize_status(r.status) for r in results
        )

        # 4. Получить execution-данные для упавших/сломанных тестов
        failures_with_execution = await self._fetch_failed_executions(results)

        # 5. Сформировать сводки из результатов + execution-шагов
        failed_tests = [
            self._build_failed_summary(r, steps, launch_id)
            for r, steps in failures_with_execution
        ]

        report = TriageReport(
            launch_id=launch_id,
            launch_name=launch.name,
            total_results=len(results),
            passed_count=status_counts.get(TestStatus.PASSED, 0),
            failed_count=status_counts.get(TestStatus.FAILED, 0),
            broken_count=status_counts.get(TestStatus.BROKEN, 0),
            skipped_count=status_counts.get(TestStatus.SKIPPED, 0),
            unknown_count=status_counts.get(TestStatus.UNKNOWN, 0),
            failed_tests=failed_tests,
        )

        self._log_report(report)
        return report

    # --- Внутренние вспомогательные методы ---

    @staticmethod
    def _normalize_status(raw: str | None) -> TestStatus:
        """Преобразовать сырую строку статуса в TestStatus enum, по умолчанию UNKNOWN."""
        if raw is None:
            return TestStatus.UNKNOWN
        try:
            return TestStatus(raw.lower())
        except ValueError:
            return TestStatus.UNKNOWN

    async def _fetch_failed_executions(
        self,
        results: list[TestResultResponse],
    ) -> list[tuple[TestResultResponse, list[ExecutionStep]]]:
        """Получить execution-шаги для упавших/сломанных тестов параллельно.

        Вызывает ``GET /api/testresult/{id}/execution`` для каждого упавшего
        теста. Именно этот эндпоинт содержит ``statusDetails`` с сообщениями
        об ошибках и стек-трейсами. Семафор ограничивает параллелизм.

        Возвращает список пар (TestResultResponse, list[ExecutionStep]).
        """
        failure_statuses = TestStatus.failure_statuses()
        failed_results = [
            r for r in results
            if self._normalize_status(r.status) in failure_statuses
        ]

        if not failed_results:
            return []

        logger.info(
            "Fetching execution details for %d failed/broken tests (concurrency=%d)",
            len(failed_results),
            self._detail_concurrency,
        )

        semaphore = asyncio.Semaphore(self._detail_concurrency)

        async def fetch_one(test_result_id: int) -> list[ExecutionStep]:
            async with semaphore:
                return await self._client.get_test_result_execution(test_result_id)

        tasks = [fetch_one(r.id) for r in failed_results]
        execution_results = await asyncio.gather(*tasks, return_exceptions=True)

        final: list[tuple[TestResultResponse, list[ExecutionStep]]] = []
        for original, exec_or_exc in zip(failed_results, execution_results):
            if isinstance(exec_or_exc, Exception):
                logger.warning(
                    "Failed to fetch execution for test result %d: %s. "
                    "Error details will be unavailable.",
                    original.id,
                    exec_or_exc,
                )
                final.append((original, []))
            else:
                final.append((original, exec_or_exc))

        return final

    @staticmethod
    def _extract_error_from_step(
        step: ExecutionStep,
    ) -> tuple[str | None, str | None]:
        """Извлечь message/trace из шага.

        Allure TestOps может хранить ошибку в двух форматах:
        - Прямые поля ``message`` и ``trace`` на шаге
        - Вложенный dict ``statusDetails`` с ключами ``message``/``trace``
        """
        message = step.message
        trace = step.trace
        if message or trace:
            return message, trace

        if step.status_details and isinstance(step.status_details, dict):
            message = step.status_details.get("message")
            trace = step.status_details.get("trace")
            if message or trace:
                return message, trace

        return None, None

    @staticmethod
    def _find_failure_in_steps(
        steps: list[ExecutionStep],
    ) -> tuple[str | None, str | None]:
        """Рекурсивно найти первый упавший шаг и извлечь message/trace.

        Обходит дерево шагов в глубину. Возвращает (message, trace) из
        первого шага со статусом failed/broken.

        Если явного статуса нет (корневой execution-объект), но есть
        данные об ошибке — тоже извлекает.

        Если ни один шаг не содержит ошибку — возвращает (None, None).
        """
        failure_statuses = {"failed", "broken"}

        # Первый проход: шаги с явным failure-статусом (приоритет)
        for step in steps:
            if step.status and step.status.lower() in failure_statuses:
                message, trace = TriageService._extract_error_from_step(step)
                if message or trace:
                    return message, trace
            # Рекурсия во вложенные шаги
            if step.steps:
                message, trace = TriageService._find_failure_in_steps(step.steps)
                if message or trace:
                    return message, trace

        # Второй проход: шаги без статуса, но с данными об ошибке
        # (корневой execution-объект может не иметь поля status)
        for step in steps:
            if step.status is not None:
                continue
            message, trace = TriageService._extract_error_from_step(step)
            if message or trace:
                return message, trace

        return None, None

    def _build_failed_summary(
        self,
        result: TestResultResponse,
        execution_steps: list[ExecutionStep],
        launch_id: int,
    ) -> FailedTestSummary:
        """Преобразовать результат теста + execution-шаги в сводку для триажа."""
        # Попытка 1: извлечь ошибку из execution-шагов
        status_message, status_trace = self._find_failure_in_steps(execution_steps)

        # Попытка 2 (fallback): из statusDetails самого результата, если есть
        if not status_message and not status_trace:
            if result.status_details and isinstance(result.status_details, dict):
                status_message = result.status_details.get("message")
                status_trace = result.status_details.get("trace")

        logger.debug(
            "Build summary for test %d: exec_steps=%d, "
            "status_message=%s, status_trace=%s, result.status_details=%s",
            result.id,
            len(execution_steps),
            repr(status_message[:100]) if status_message else None,
            repr(status_trace[:100]) if status_trace else None,
            repr(str(result.status_details)[:200]) if result.status_details else None,
        )

        link = (
            f"{self._endpoint}/launch/{launch_id}/testresult/{result.id}"
        )

        return FailedTestSummary(
            test_result_id=result.id,
            name=result.name or f"test-result-{result.id}",
            full_name=result.full_name,
            status=self._normalize_status(result.status),
            category=result.category,
            status_message=status_message,
            status_trace=status_trace,
            execution_steps=execution_steps or None,
            test_case_id=result.test_case_id,
            link=link,
            duration_ms=result.duration,
        )

    @staticmethod
    def _log_report(report: TriageReport) -> None:
        """Залогировать сводку отчёта триажа."""
        logger.info(
            "Launch #%d (%s): %d total | passed=%d failed=%d broken=%d skipped=%d unknown=%d",
            report.launch_id,
            report.launch_name or "unnamed",
            report.total_results,
            report.passed_count,
            report.failed_count,
            report.broken_count,
            report.skipped_count,
            report.unknown_count,
        )

        if report.failed_tests:
            logger.info("Failures (%d):", report.failure_count)
            for t in report.failed_tests:
                logger.info(
                    "  [%s] %s (ID: %d) %s",
                    t.status.value.upper(),
                    t.name,
                    t.test_result_id,
                    t.link or "",
                )
        else:
            logger.info("No failures found.")
