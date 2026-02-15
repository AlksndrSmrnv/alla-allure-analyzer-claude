"""Точка входа CLI для агента триажа alla."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from alla import __version__

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alla",
        description="AI-агент триажа упавших тестов — анализ результатов из Allure TestOps",
    )
    parser.add_argument(
        "launch_id",
        nargs="?",
        type=int,
        help="ID запуска для анализа",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Уровень логирования (переопределяет ALLURE_LOG_LEVEL)",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Формат вывода (по умолчанию: text)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=None,
        help="Результатов на страницу (переопределяет ALLURE_PAGE_SIZE)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"alla {__version__}",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    """Собрать зависимости и запустить триаж. Возвращает код выхода."""
    # Отложенные импорты — чтобы --help работал быстро
    from alla.clients.auth import AllureAuthManager
    from alla.clients.testops_client import AllureTestOpsClient
    from alla.config import Settings
    from alla.exceptions import AllaError, ConfigurationError
    from alla.logging_config import setup_logging

    # 1. Загрузка настроек
    try:
        overrides: dict[str, object] = {}
        if args.page_size is not None:
            overrides["page_size"] = args.page_size
        settings = Settings(**overrides)  # type: ignore[arg-type]
    except Exception as exc:
        # pydantic-settings выбрасывает ValidationError при отсутствии обязательных полей
        print(
            f"Ошибка конфигурации: {exc}\n\n"
            f"Обязательные переменные окружения: "
            f"ALLURE_ENDPOINT, ALLURE_TOKEN\n"
            f"Подробности см. в .env.example.",
            file=sys.stderr,
        )
        return 2

    # 2. Настройка логирования
    log_level = args.log_level or settings.log_level
    setup_logging(log_level)

    # 3. Определение ID запуска
    launch_id = args.launch_id
    if launch_id is None:
        logger.error(
            "Не указан launch_id. Передайте его позиционным аргументом: alla <launch_id>"
        )
        return 2

    # 4. Запуск триажа
    auth = AllureAuthManager(
        endpoint=settings.endpoint,
        api_token=settings.token,
        timeout=settings.request_timeout,
        ssl_verify=settings.ssl_verify,
    )

    try:
        async with AllureTestOpsClient(settings, auth) as client:
            from alla.orchestrator import analyze_launch

            result = await analyze_launch(
                launch_id=launch_id,
                client=client,
                settings=settings,
                updater=client,
            )
            report = result.triage_report
            clustering_report = result.clustering_report
            kb_results = result.kb_results
            kb_push_result = result.kb_push_result
            llm_result = result.llm_result
            llm_push_result = result.llm_push_result

    except ConfigurationError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 2
    except AllaError as exc:
        logger.error("Ошибка: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
        return 130
    except BaseException:
        await auth.close()
        raise

    # 6. Вывод отчёта
    if args.output_format == "json":
        import json
        from dataclasses import asdict

        output = {"triage_report": report.model_dump()}
        if clustering_report is not None:
            output["clustering_report"] = clustering_report.model_dump()
        if kb_results:
            output["kb_matches"] = {
                cid: [m.model_dump() for m in matches]
                for cid, matches in kb_results.items()
            }
        if kb_push_result is not None:
            output["kb_push_result"] = asdict(kb_push_result)
        if llm_result is not None:
            output["llm_result"] = {
                "total_clusters": llm_result.total_clusters,
                "analyzed_count": llm_result.analyzed_count,
                "failed_count": llm_result.failed_count,
                "skipped_count": llm_result.skipped_count,
                "cluster_analyses": {
                    cid: a.model_dump()
                    for cid, a in llm_result.cluster_analyses.items()
                },
            }
        if llm_push_result is not None:
            output["llm_push_result"] = asdict(llm_push_result)
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    else:
        _print_text_report(report)
        if clustering_report is not None:
            _print_clustering_report(
                clustering_report, kb_results, llm_result,
                failed_tests=report.failed_tests,
            )
        if kb_push_result is not None:
            print(
                f"[KB Push] Комментариев: {kb_push_result.updated_count}"
                f" | Ошибок: {kb_push_result.failed_count}"
                f" | Пропущено: {kb_push_result.skipped_count}"
            )
        if llm_result is not None:
            print(
                f"[LLM] Проанализировано: {llm_result.analyzed_count}"
                f" | Ошибок: {llm_result.failed_count}"
                f" | Пропущено: {llm_result.skipped_count}"
            )
        if llm_push_result is not None:
            print(
                f"[LLM Push] Комментариев: {llm_push_result.updated_count}"
                f" | Ошибок: {llm_push_result.failed_count}"
                f" | Пропущено: {llm_push_result.skipped_count}"
            )

    return 0


def _print_text_report(report: TriageReport) -> None:  # noqa: F821
    """Вывод человекочитаемого отчёта триажа в stdout."""

    print()
    print("=== Отчёт триажа Allure ===")
    launch_label = f"Запуск: #{report.launch_id}"
    if report.launch_name:
        launch_label += f" ({report.launch_name})"
    print(launch_label)
    print(
        f"Всего: {report.total_results}"
        f" | Успешно: {report.passed_count}"
        f" | Провалено: {report.failed_count}"
        f" | Сломано: {report.broken_count}"
        f" | Пропущено: {report.skipped_count}"
        f" | Неизвестно: {report.unknown_count}"
    )
    print()

    if report.failed_tests:
        print(f"Падения ({report.failure_count}):")
        for t in report.failed_tests:
            print(f"  [{t.status.value.upper()}]  {t.name} (ID: {t.test_result_id})")
            if t.link:
                print(f"            {t.link}")
            if t.status_message:
                # Обрезка длинных сообщений
                msg = t.status_message
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                print(f"            {msg}")
            if t.log_snippet:
                log_lines = t.log_snippet.strip().splitlines()
                print(f"            [LOG] {len(log_lines)} строк лога")
    else:
        print("Падения не найдены.")

    print()


def _print_clustering_report(
    report: ClusteringReport,  # noqa: F821
    kb_results: dict[str, list] | None = None,
    llm_result: LLMAnalysisResult | None = None,  # noqa: F821
    failed_tests: list[FailedTestSummary] | None = None,  # noqa: F821
) -> None:
    """Вывод отчёта кластеризации ошибок в stdout."""
    from alla.utils.log_utils import has_explicit_errors

    test_by_id: dict[int, FailedTestSummary] = {}
    if failed_tests:
        test_by_id = {t.test_result_id: t for t in failed_tests}

    print(
        f"=== Кластеры падений "
        f"({report.cluster_count} уникальных проблем из {report.total_failures} падений) ==="
    )
    print()

    for i, cluster in enumerate(report.clusters, 1):
        cluster_lines = [
            f"Кластер #{i} ({cluster.member_count} тестов)",
        ]
        if cluster.example_message:
            msg = _normalize_single_line(cluster.example_message)
            if len(msg) > 200:
                msg = msg[:200] + "..."
            cluster_lines.append(f"Пример: {msg}")
        cluster_lines.extend(
            _wrap_test_ids(cluster.member_test_ids)
        )

        # Факт наличия лога
        if test_by_id and cluster.representative_test_id is not None:
            rep = test_by_id.get(cluster.representative_test_id)
            log_snippet = rep.log_snippet if rep else None
            has_log = bool(log_snippet and log_snippet.strip())
            if has_log:
                has_errors = has_explicit_errors(log_snippet)
                if has_errors:
                    cluster_lines.append("Лог: найден, содержит ошибки")
                else:
                    cluster_lines.append("Лог: найден, без явных ошибок")
            else:
                cluster_lines.append("Лог: отсутствует")

        # KB-совпадения
        matches = (kb_results or {}).get(cluster.cluster_id, [])
        if matches:
            cluster_lines.append("")
            count_label = _pluralize_matches(len(matches))
            cluster_lines.append(f"База знаний ({count_label}):")
            for m in matches:
                cluster_lines.append(
                    f"  [{m.score:.2f}] {m.entry.title}"
                )
                cluster_lines.append(
                    f"         Категория: {m.entry.category.value}"
                )
                for step in m.entry.resolution_steps[:2]:
                    step_text = step if len(step) <= 80 else step[:77] + "..."
                    cluster_lines.append(f"         -> {step_text}")

        # LLM-анализ
        if llm_result is not None:
            analysis = llm_result.cluster_analyses.get(cluster.cluster_id)
            if analysis and analysis.analysis_text:
                cluster_lines.append("")
                cluster_lines.append("LLM-анализ:")
                for line in analysis.analysis_text.split("\n"):
                    cluster_lines.append(f"  {line}")

        for line in _render_box(cluster_lines):
            print(line)
        print()


def _wrap_test_ids(
    test_ids: list[int],
    max_width: int = 80,
) -> list[str]:
    """Отформатировать список ID тестов с переносом строк."""
    prefix = "Тесты: "
    indent = " " * len(prefix)

    all_ids = [str(tid) for tid in test_ids]
    lines: list[str] = []
    current = prefix

    for i, tid in enumerate(all_ids):
        separator = ", " if i > 0 else ""
        candidate = current + separator + tid

        if len(candidate) > max_width and current != prefix and current != indent:
            lines.append(current + ",")
            current = indent + tid
        else:
            current = candidate

    lines.append(current)
    return lines


def _normalize_single_line(value: str) -> str:
    """Схлопнуть переводы строк/табуляцию в одну строку для рамочного вывода."""
    return " ".join(value.replace("\t", " ").split())


def _pluralize_matches(count: int) -> str:
    """Склонение слова 'совпадение' по числу."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} совпадение"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} совпадения"
    return f"{count} совпадений"


def _wrap_text(text: str, max_width: int, indent: str = "") -> list[str]:
    """Разбить длинную строку на несколько строк с переносом по словам.
    
    Args:
        text: Исходный текст для переноса.
        max_width: Максимальная ширина строки.
        indent: Отступ для продолжения (continuation lines).
    
    Returns:
        Список строк, каждая не длиннее max_width.
    """
    if max_width <= 0:
        return [text]

    if len(text) <= max_width:
        return [text]

    # Сохраняем ведущие пробелы для первой строки
    stripped = text.lstrip()
    first_line_indent = text[: len(text) - len(stripped)]

    words = stripped.split()
    lines: list[str] = []
    current_line = ""

    for word in words:
        # Определяем текущий отступ: для первой строки — исходный, для остальных — переданный
        current_indent = first_line_indent if not lines and not current_line else ""
        
        # Проверяем, поместится ли слово
        if not current_line:
            candidate = current_indent + word
        else:
            candidate = current_line + " " + word

        if len(candidate) <= max_width:
            current_line = candidate
        else:
            # Сохраняем текущую строку, если она не пуста
            if current_line:
                lines.append(current_line)
            # Выбираем отступ: first_line_indent для первого слова, indent для остальных
            word_indent = first_line_indent if not lines else indent
            current_line = word_indent + word
            
            # Если даже одно слово не помещается — принудительный разрыв
            # Используем безопасный отступ, чтобы избежать бесконечного цикла
            safe_indent = indent if len(indent) < max_width else ""
            while len(current_line) > max_width:
                lines.append(current_line[:max_width])
                current_line = safe_indent + current_line[max_width:]

    if current_line:
        lines.append(current_line)

    return lines if lines else [text]


def _render_box(lines: list[str], max_width: int = 100) -> list[str]:
    """Отрендерить список строк в Unicode-рамку с переносом длинных строк.
    
    Args:
        lines: Строки для отображения внутри рамки.
        max_width: Максимальная ширина содержимого (по умолчанию 100).
    
    Returns:
        Список строк с Unicode-рамкой.
    """
    if not lines:
        return []

    # Переносим длинные строки
    wrapped_lines: list[str] = []
    for line in lines:
        if len(line) <= max_width:
            wrapped_lines.append(line)
        else:
            # Определяем отступ на основе начала строки
            stripped = line.lstrip()
            leading_spaces = len(line) - len(stripped)
            indent = " " * leading_spaces if leading_spaces > 0 else "  "
            wrapped_lines.extend(_wrap_text(line, max_width, indent))

    width = max(len(line) for line in wrapped_lines) if wrapped_lines else 0
    top = f"╔{'═' * (width + 2)}╗"
    bottom = f"╚{'═' * (width + 2)}╝"
    body = [f"║ {line.ljust(width)} ║" for line in wrapped_lines]

    return [top, *body, bottom]


def build_delete_parser() -> argparse.ArgumentParser:
    """Создать парсер для команды ``alla delete``."""
    parser = argparse.ArgumentParser(
        prog="alla delete",
        description="Удалить комментарии alla из Allure TestOps для указанного запуска",
    )
    parser.add_argument(
        "launch_id",
        type=int,
        help="ID запуска, для тестов которого удалить комментарии alla",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, какие комментарии будут удалены, без фактического удаления",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Уровень логирования (переопределяет ALLURE_LOG_LEVEL)",
    )
    return parser


async def async_delete(args: argparse.Namespace) -> int:
    """Удалить комментарии alla для указанного запуска. Возвращает код выхода."""
    from alla.clients.auth import AllureAuthManager
    from alla.clients.base import CommentManager
    from alla.clients.testops_client import AllureTestOpsClient
    from alla.config import Settings
    from alla.exceptions import AllaError, ConfigurationError
    from alla.logging_config import setup_logging
    from alla.services.comment_delete_service import CommentDeleteService

    # 1. Загрузка настроек
    try:
        settings = Settings()
    except Exception as exc:
        print(
            f"Ошибка конфигурации: {exc}\n\n"
            f"Обязательные переменные окружения: "
            f"ALLURE_ENDPOINT, ALLURE_TOKEN\n"
            f"Подробности см. в .env.example.",
            file=sys.stderr,
        )
        return 2

    # 2. Настройка логирования
    log_level = args.log_level or settings.log_level
    setup_logging(log_level)

    launch_id = args.launch_id
    dry_run = args.dry_run

    # 3. Получить тесты и удалить комментарии
    auth = AllureAuthManager(
        endpoint=settings.endpoint,
        api_token=settings.token,
        timeout=settings.request_timeout,
        ssl_verify=settings.ssl_verify,
    )

    try:  # noqa: SIM117 — нужен отдельный try для auth.close() в except BaseException
        async with AllureTestOpsClient(settings, auth) as client:
            if not isinstance(client, CommentManager):
                logger.error("Клиент не поддерживает управление комментариями")
                return 1

            # Получить все результаты тестов для запуска
            all_results = await client.get_all_test_results_for_launch(launch_id)

            # Фильтр: только failed/broken
            failure_statuses = {"failed", "broken"}
            failed_results = [
                r for r in all_results
                if r.status and r.status.lower() in failure_statuses
            ]

            # Собрать уникальные test_case_id
            test_case_ids: set[int] = set()
            skipped = 0
            for r in failed_results:
                if r.test_case_id is not None:
                    test_case_ids.add(r.test_case_id)
                else:
                    skipped += 1

            # Вывести заголовок
            mode_label = " (DRY RUN)" if dry_run else ""
            print()
            print(f"=== Удаление комментариев alla{mode_label} ===")
            print(f"Запуск: #{launch_id}")
            print(
                f"Упавших тестов: {len(failed_results)}"
                f" | Уникальных test_case_id: {len(test_case_ids)}"
            )

            if not test_case_ids:
                print("Нет тест-кейсов для обработки.")
                print()
                return 0

            # Удалить комментарии
            service = CommentDeleteService(
                client,
                concurrency=settings.detail_concurrency,
            )
            result = await service.delete_alla_comments(
                test_case_ids,
                dry_run=dry_run,
            )

            # Вывод результата
            print()
            if dry_run:
                print(f"Было бы удалено комментариев: {result.comments_found}")
            else:
                print(
                    f"Найдено комментариев alla: {result.comments_found}"
                )
                print(
                    f"Удалено: {result.comments_deleted}"
                    f" | Ошибок: {result.comments_failed}"
                    f" | Пропущено (нет test_case_id): {skipped}"
                )
            print()

    except ConfigurationError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 2
    except AllaError as exc:
        logger.error("Ошибка: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
        return 130
    except BaseException:
        await auth.close()
        raise

    return 0


def main() -> None:
    """Синхронная точка входа для CLI."""
    if len(sys.argv) > 1 and sys.argv[1] == "delete":
        parser = build_delete_parser()
        args = parser.parse_args(sys.argv[2:])
        exit_code = asyncio.run(async_delete(args))
    else:
        parser = build_parser()
        args = parser.parse_args()
        exit_code = asyncio.run(async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
