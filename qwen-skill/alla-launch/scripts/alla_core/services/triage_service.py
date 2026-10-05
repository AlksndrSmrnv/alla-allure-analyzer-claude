"""Сервис триажа: получение результатов тестов, фильтрация падений, формирование сводки."""

import asyncio
import logging
from collections import Counter

from alla_core.clients.base import TestResultsProvider
from alla_core.config import Settings
from alla_core.models.common import TestStatus
from alla_core.models.testops import (
    AttemptSummary,
    ExecutionStep,
    FailedTestSummary,
    PassedAfterRetry,
    RetryInfo,
    TestResultResponse,
    TriageReport,
)
from alla_core.services.retry_linking import RetryLinks, link_attempts
from alla_core.utils.text_normalization import repeat_key

logger = logging.getLogger(__name__)


# Ошибки попыток: не больше стольких последних попыток на тест, первая строка — до
# стольких символов.
MAX_ATTEMPTS_PER_TEST = 5
ATTEMPT_MESSAGE_CHARS = 300


def _diagnostic_text(value) -> str | None:
    """Unvalidated statusDetails values must not bypass model field types."""
    return value if isinstance(value, str) and value.strip() else None


def _first_line(text: str | None) -> str | None:
    """Первая непустая строка без краевых пробелов; ``None`` — текста нет."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return None


def _attempt_line(status_details: object, trace: str | None) -> str | None:
    """Первая строка ошибки попытки целиком: из ``statusDetails.message``, иначе
    ``.trace``, иначе верхнего ``trace``. По ней сравниваются ошибки (до обрезки)."""
    details = status_details if isinstance(status_details, dict) else {}
    return (
        _first_line(_diagnostic_text(details.get("message")))
        or _first_line(_diagnostic_text(details.get("trace")))
        or _first_line(_diagnostic_text(trace))
    )


def _clip_attempt_line(line: str | None) -> str | None:
    """Строка для run.json: длиннее :data:`ATTEMPT_MESSAGE_CHARS` — с «…»."""
    if line and len(line) > ATTEMPT_MESSAGE_CHARS:
        return line[:ATTEMPT_MESSAGE_CHARS - 1].rstrip() + "…"
    return line


def _attempt_message(status_details: object, trace: str | None) -> str | None:
    """Первая строка ошибки попытки, обрезанная для показа."""
    return _clip_attempt_line(_attempt_line(status_details, trace))


class TriageService:
    """Оркестрирует процесс триажа упавших тестов.

    Получение результатов тестов, извлечение ошибок (трёхуровневый fallback),
    формирование сводки по упавшим тестам.
    """

    def __init__(self, client: TestResultsProvider, settings: Settings) -> None:
        self._client = client
        self._endpoint = str(settings.endpoint).rstrip("/")
        self._detail_concurrency = settings.detail_concurrency
        self._retry_max_detail_requests = settings.retry_max_detail_requests

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

        # 2.1. Связать hidden-результаты (попытки до финального) с финальными — до
        # фильтрации; статистика и активные падения считаются без hidden, как раньше.
        links = link_attempts(all_results)
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

        # 5.6. Попытки активных падений с ошибками и прошедшие после повтора
        retries = await self._attach_attempts(failed_tests, links, results, launch_id)

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
            retries=retries,
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
        gathered_results = await asyncio.gather(*tasks, return_exceptions=True)
        execution_results: list[list[ExecutionStep] | BaseException] = [
            item if isinstance(item, BaseException) else list(item)
            for item in gathered_results
        ]

        return [
            self._collect_execution_result(original, exec_or_exc)
            for original, exec_or_exc in zip(failed_results, execution_results)
        ]

    @staticmethod
    def _collect_execution_result(
        result: TestResultResponse,
        execution_result: list[ExecutionStep] | BaseException,
    ) -> tuple[TestResultResponse, list[ExecutionStep]]:
        """Нормализовать результат fetch execution в пару (result, steps)."""
        if isinstance(execution_result, BaseException):
            logger.warning(
                "Не удалось получить execution для результата теста %d: %s. "
                "Ошибка может быть получена через fallback (GET /api/testresult/{id}).",
                result.id,
                execution_result,
            )
            return result, []
        return result, execution_result

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
        missing = [
            s for s in summaries
            if not s.status_message and not s.status_trace
        ]
        if not missing:
            return

        logger.info(
            "Fallback: %d тестов без message/trace, "
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
            if not summary.status_message:
                first_line = detail.trace.strip().split("\n", 1)[0]
                if first_line:
                    summary.status_message = first_line
            logger.debug(
                "Fallback: получен trace для теста %d из GET /api/testresult/{id}",
                summary.test_result_id,
            )

    async def _attach_attempts(
        self,
        summaries: list[FailedTestSummary],
        links: RetryLinks,
        results: list[TestResultResponse],
        launch_id: int,
    ) -> RetryInfo:
        """Заполнить ``attempts`` активных падений и найти прошедшие после повтора.

        Ошибка попытки — из ``statusDetails`` списка результатов; нет её там — из
        ``GET /api/testresult/{id}``, не больше ``retry_max_detail_requests`` запросов на
        прогон (по порядку падений). Берутся последние :data:`MAX_ATTEMPTS_PER_TEST`
        попыток теста. Сбой запроса — ошибка попытки неизвестна, разбор продолжается.
        """
        info = links.info()
        failure_statuses = TestStatus.failure_statuses()
        pending: list[AttemptSummary] = []
        # Полные первые строки ошибок попыток по id: «та же ошибка» сравнивается до
        # обрезки, иначе различие после ATTEMPT_MESSAGE_CHARS (код ошибки) терялось бы.
        lines: dict[int, str] = {}
        for summary in summaries:
            attempts = links.attempts.get(summary.test_result_id, [])
            recent = attempts[-MAX_ATTEMPTS_PER_TEST:]
            summary.attempts_omitted = len(attempts) - len(recent)
            for attempt in recent:
                line = _attempt_line(attempt.status_details, attempt.trace)
                item = AttemptSummary(
                    test_result_id=attempt.id,
                    status=self._normalize_status(attempt.status),
                    message=_clip_attempt_line(line),
                )
                if line is not None:
                    lines[attempt.id] = line
                summary.attempts.append(item)
                if item.status in failure_statuses:
                    info.errors_total += 1
                    if item.message is None:
                        pending.append(item)

        allowed = pending[:self._retry_max_detail_requests]
        info.errors_capped = len(pending) - len(allowed)
        if allowed:
            logger.info(
                "Ошибки повторов: запрос GET /api/testresult/{id} для %d попыток", len(allowed),
            )
        semaphore = asyncio.Semaphore(self._detail_concurrency)

        async def fetch_one(item: AttemptSummary) -> None:
            async with semaphore:
                try:
                    detail = await self._client.get_test_result_detail(item.test_result_id)
                except Exception as exc:
                    logger.warning(
                        "Не удалось получить ошибку попытки %d: %s", item.test_result_id, exc,
                    )
                    return
            line = _attempt_line(detail.status_details, detail.trace)
            item.message = _clip_attempt_line(line)
            if line is not None:
                lines[item.test_result_id] = line

        await asyncio.gather(*(fetch_one(item) for item in allowed))

        for summary in summaries:
            # Первая строка финального падения выбирается так же, как у попыток.
            final = _attempt_line({"message": summary.status_message}, summary.status_trace)
            for item in summary.attempts:
                if item.status not in failure_statuses:
                    continue
                line = lines.get(item.test_result_id)
                if line is not None:
                    info.errors_known += 1
                    if final is not None:
                        item.same_as_final = repeat_key(line) == repeat_key(final)

        info.passed_after_retry = self._passed_after_retry(links, results, launch_id)
        return info

    def _passed_after_retry(
        self,
        links: RetryLinks,
        results: list[TestResultResponse],
        launch_id: int,
    ) -> list[PassedAfterRetry]:
        """Финальный ``passed`` с неудачными попытками; ошибка — только из списка результатов."""
        failure_statuses = TestStatus.failure_statuses()
        found: list[PassedAfterRetry] = []
        for result in results:
            if self._normalize_status(result.status) != TestStatus.PASSED:
                continue
            failed = [
                attempt for attempt in links.attempts.get(result.id, [])
                if self._normalize_status(attempt.status) in failure_statuses
            ]
            if not failed:
                continue
            found.append(PassedAfterRetry(
                test_result_id=result.id,
                name=result.name or f"test-result-{result.id}",
                full_name=result.full_name,
                link=f"{self._endpoint}/launch/{launch_id}/testresult/{result.id}",
                failed_attempts=len(failed),
                message=next(
                    (message for attempt in failed
                     if (message := _attempt_message(attempt.status_details, attempt.trace))),
                    None,
                ),
            ))
        return found

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
            message = _diagnostic_text(step.status_details.get("message"))
            trace = _diagnostic_text(step.status_details.get("trace"))
            if message or trace:
                return message, trace

        return None, None

    @staticmethod
    def _find_failure_details_in_steps(
        steps: list[ExecutionStep],
        _ancestors: list[str] | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """Рекурсивно найти упавшую цепочку и извлечь message/trace/breadcrumb.

        Стратегия — depth-first по первой найденной failure-цепочке.
        ``step_path`` — хлебные крошки до **самого глубокого** failed/broken-узла
        этой ветки, разделённые « → ». А вот ``message``/``trace`` берутся с
        **ближайшего внешнего** failed-шага, у которого они есть: внешний
        обычно содержит полную ошибку (assertion-префикс), а вложенные —
        обрезанный фрагмент того же сообщения. Если у внешнего failed-шага
        своих message/trace нет — каждое из них независимо подтягивается с
        глубокого failed-узла.

        Если самый глубокий failed-узел сам без message/trace, они также
        могут подтянуться со statusless-обёртки с ``statusDetails`` (см. ветку
        не-failed-родителя ниже).

        Если явного статуса нет (корневой execution-объект), но есть
        данные об ошибке — тоже извлекает.

        Если ни один шаг не содержит ошибку — возвращает (None, None, None).
        """
        ancestors = _ancestors or []
        failure_statuses = {"failed", "broken"}

        # Первый проход: шаги с явным failure-статусом (приоритет)
        for step in steps:
            current_path = [*ancestors, step.name] if step.name else list(ancestors)
            is_failed = bool(step.status and step.status.lower() in failure_statuses)

            if is_failed:
                own_message, own_trace = TriageService._extract_error_from_step(step)
                own_breadcrumb = " → ".join(current_path) if current_path else None

                # Сначала ищем более глубокий failed-шаг внутри текущего —
                # это нужно, чтобы step_path вёл до самого вложенного падения.
                # Но сам message/trace предпочитаем брать с внешнего failed-шага:
                # вложенные часто содержат лишь урезанный фрагмент той же ошибки
                # без assertion-префикса, что портит «Пример ошибки» в отчёте.
                if step.steps:
                    deeper_msg, deeper_trace, deeper_breadcrumb = (
                        TriageService._find_failure_details_in_steps(
                            step.steps, current_path,
                        )
                    )
                    if deeper_breadcrumb is not None:
                        return (
                            own_message or deeper_msg,
                            own_trace or deeper_trace,
                            deeper_breadcrumb,
                        )

                # Глубже failed-шагов нет — возвращаем текущий
                if own_message or own_trace or own_breadcrumb:
                    return own_message, own_trace, own_breadcrumb
            elif step.steps:
                # Не-failed родитель: рекурсия с включением имени в путь.
                # Если у вложенного failed-шага нет своего message или trace,
                # каждый из них независимо подтягивается с этого родителя —
                # он может быть statusless wrapper с statusDetails. Логика
                # симметрична с failed-веткой выше.
                message, trace, breadcrumb = TriageService._find_failure_details_in_steps(
                    step.steps, current_path,
                )
                if breadcrumb is not None:
                    if not message or not trace:
                        own_message, own_trace = TriageService._extract_error_from_step(step)
                        return message or own_message, trace or own_trace, breadcrumb
                    return message, trace, breadcrumb

        # Второй проход: шаги без статуса, но с данными об ошибке
        # (корневой execution-объект может не иметь поля status)
        for step in steps:
            if step.status is not None:
                continue
            message, trace = TriageService._extract_error_from_step(step)
            if message or trace:
                current_path = [*ancestors, step.name] if step.name else list(ancestors)
                breadcrumb = " → ".join(current_path) if current_path else None
                return message, trace, breadcrumb

        return None, None, None

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
        status_message, status_trace, failed_step_path = self._find_failure_details_in_steps(
            execution_steps,
        )

        # Попытка 2 (fallback): из statusDetails — заполнить отсутствующие поля
        if result.status_details and isinstance(result.status_details, dict):
            if not status_message:
                status_message = _diagnostic_text(result.status_details.get("message"))
            if not status_trace:
                status_trace = _diagnostic_text(result.status_details.get("trace"))

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
            failed_step_path=failed_step_path,
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
