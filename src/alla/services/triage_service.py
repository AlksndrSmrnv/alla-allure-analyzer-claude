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

    Получение результатов тестов, извлечение ошибок (трёхуровневый fallback),
    формирование сводки по упавшим тестам.
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
            4. Получить execution-шаги для упавших/сломанных тестов.
            5. Создать FailedTestSummary для каждого упавшего/сломанного теста.
            5.5. Fallback: для тестов без ошибки — запросить GET /api/testresult/{id}.
            6. Вернуть TriageReport.
        """
        # 1. Метаданные запуска
        launch = await self._client.get_launch(launch_id)
        logger.info("Анализ запуска #%d (%s)", launch_id, launch.name or "без названия")

        # 2. Все результаты тестов
        all_results = await self._client.get_all_test_results_for_launch(launch_id)

        # 2.1. Исключить hidden-результаты (retry-попытки, не финальные)
        results = [r for r in all_results if not r.hidden]
        hidden_count = len(all_results) - len(results)
        if hidden_count:
            logger.info(
                "Исключено %d hidden-результатов (retry, не финальная попытка)",
                hidden_count,
            )

        # 3. Подсчёт по статусам (без hidden, но с muted)
        status_counts = Counter(
            self._normalize_status(r.status) for r in results
        )

        # 3.1. Подсчитать muted-падения (включены в status_counts, но не в анализ)
        failure_statuses = TestStatus.failure_statuses()
        muted_failure_count = sum(
            1 for r in results
            if self._normalize_status(r.status) in failure_statuses
            and r.muted
        )

        # 4. Получить execution-данные для упавших/сломанных тестов
        failures_with_execution = await self._fetch_failed_executions(results)

        # 5. Сформировать сводки из результатов + execution-шагов
        failed_tests = [
            self._build_failed_summary(r, steps, launch_id)
            for r, steps in failures_with_execution
        ]

        # 5.5. Fallback: для тестов без ошибки — запросить GET /api/testresult/{id}
        await self._fetch_missing_traces(failed_tests)

        report = TriageReport(
            launch_id=launch_id,
            launch_name=launch.name,
            project_id=launch.project_id,
            total_results=len(results),
            passed_count=status_counts.get(TestStatus.PASSED, 0),
            failed_count=status_counts.get(TestStatus.FAILED, 0),
            broken_count=status_counts.get(TestStatus.BROKEN, 0),
            skipped_count=status_counts.get(TestStatus.SKIPPED, 0),
            unknown_count=status_counts.get(TestStatus.UNKNOWN, 0),
            muted_failure_count=muted_failure_count,
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
            and not r.muted
        ]

        muted_failures = sum(
            1 for r in results
            if self._normalize_status(r.status) in failure_statuses
            and r.muted
        )
        if muted_failures:
            logger.info(
                "Исключено %d muted-падений из анализа",
                muted_failures,
            )

        if not failed_results:
            return []

        logger.info(
            "Получение execution-деталей для %d упавших/сломанных тестов "
            "(параллелизм=%d)",
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
                    "Не удалось получить execution для результата теста %d: %s. "
                    "Ошибка может быть получена через fallback (GET /api/testresult/{id}).",
                    original.id,
                    exec_or_exc,
                )
                final.append((original, []))
            else:
                final.append((original, exec_or_exc))

        return final

    async def _fetch_missing_traces(
        self,
        summaries: list[FailedTestSummary],
    ) -> None:
        """Fallback: для тестов без ошибки — запросить GET /api/testresult/{id}.

        Некоторые тесты имеют все execution steps в статусе passed, а statusDetails
        в пагинированном списке пустой. В таких случаях trace доступен только
        на индивидуальном эндпоинте ``GET /api/testresult/{id}``.

        Мутирует объекты summaries in-place, заполняя status_trace и status_message.
        """
        missing = [s for s in summaries if not s.status_message and not s.status_trace]
        if not missing:
            return

        logger.info(
            "Fallback: %d тестов без информации об ошибке, "
            "запрос GET /api/testresult/{id} для каждого",
            len(missing),
        )

        semaphore = asyncio.Semaphore(self._detail_concurrency)

        async def fetch_one(test_result_id: int) -> TestResultResponse | None:
            async with semaphore:
                try:
                    return await self._client.get_test_result_detail(test_result_id)
                except Exception as exc:
                    logger.warning(
                        "Не удалось получить детали результата теста %d: %s",
                        test_result_id,
                        exc,
                    )
                    return None

        tasks = [fetch_one(s.test_result_id) for s in missing]
        results = await asyncio.gather(*tasks)

        for summary, detail in zip(missing, results):
            if detail is None or not detail.trace:
                continue
            summary.status_trace = detail.trace
            first_line = detail.trace.strip().split("\n", 1)[0]
            if first_line:
                summary.status_message = first_line
            logger.debug(
                "Fallback: получен trace для теста %d из GET /api/testresult/{id}",
                summary.test_result_id,
            )

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
        """Преобразовать результат теста + execution-шаги в сводку для триажа.

        Извлечение ошибки — двухуровневый fallback (третий уровень
        обрабатывается позже в ``_fetch_missing_traces``):
            1. Из execution-шагов (дерево шагов ``GET /api/testresult/{id}/execution``).
            2. Из ``statusDetails`` результата (пагинированный список).
            3. (позже) Из ``trace`` индивидуального результата (``GET /api/testresult/{id}``).
        """
        # Попытка 1: извлечь ошибку из execution-шагов
        status_message, status_trace = self._find_failure_in_steps(execution_steps)

        # Попытка 2 (fallback): из statusDetails самого результата, если есть
        if not status_message and not status_trace:
            if result.status_details and isinstance(result.status_details, dict):
                status_message = result.status_details.get("message")
                status_trace = result.status_details.get("trace")

        logger.debug(
            "Сборка сводки для теста %d: шагов=%d, "
            "сообщение=%s, трейс=%s, status_details результата=%s",
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
            test_start_ms=result.created_date,
        )

    @staticmethod
    def _log_report(report: TriageReport) -> None:
        """Залогировать сводку отчёта триажа."""
        msg = (
            "Запуск #%d (%s): всего=%d | успешно=%d | провалено=%d "
            "| сломано=%d | пропущено=%d | неизвестно=%d"
        )
        args: list[object] = [
            report.launch_id,
            report.launch_name or "без названия",
            report.total_results,
            report.passed_count,
            report.failed_count,
            report.broken_count,
            report.skipped_count,
            report.unknown_count,
        ]
        if report.muted_failure_count:
            msg += " | muted=%d"
            args.append(report.muted_failure_count)
        logger.info(msg, *args)

        if report.failed_tests:
            logger.info("Падения (%d):", len(report.failed_tests))
            for t in report.failed_tests:
                logger.info(
                    "  [%s] %s (ID: %d) %s",
                    t.status.value.upper(),
                    t.name,
                    t.test_result_id,
                    t.link or "",
                )
        else:
            logger.info("Падения не найдены.")
