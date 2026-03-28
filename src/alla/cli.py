"""Точка входа CLI для агента триажа alla."""

import argparse
import asyncio
import logging
import sys

from alla import __version__
from alla.cli_output import (
    print_clustering_report,
    print_launch_summary,
    print_text_report,
)

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
        "--launch-name",
        dest="launch_name",
        default=None,
        help=(
            "Название запуска (альтернатива позиционному launch_id). "
            "Инструмент найдёт ID через API: "
            "GET /api/launch?projectId=X&sort=created_date,DESC. "
            "ID проекта берётся из --project-id или ALLURE_PROJECT_ID."
        ),
    )
    parser.add_argument(
        "--project-id",
        dest="project_id",
        type=int,
        default=None,
        help=(
            "ID проекта в Allure TestOps (переопределяет ALLURE_PROJECT_ID). "
            "Используется при поиске запуска по имени (--launch-name)."
        ),
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
        "--html-report-file",
        dest="html_report_file",
        default=None,
        metavar="PATH",
        help="Сохранить HTML-отчёт в указанный файл (например: alla-report.html)",
    )
    parser.add_argument(
        "--report-url",
        dest="report_url",
        default=None,
        metavar="URL",
        help=(
            "URL HTML-отчёта для прикрепления к запуску в Allure TestOps "
            "(переопределяет ALLURE_REPORT_URL). "
            "Требует --html-report-file. "
            "Пример: http://jenkins/job/alla/33/artifact/alla-report.html"
        ),
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
    from alla.app_support import (
        attach_report_link,
        build_analysis_response,
        build_html_report_content,
        format_configuration_error,
        load_settings,
        resolve_report_url,
        save_html_report,
    )
    from alla.clients.auth import AllureAuthManager
    from alla.clients.testops_client import AllureTestOpsClient
    from alla.exceptions import AllaError, ConfigurationError
    from alla.logging_config import setup_logging

    # 1. Загрузка настроек
    try:
        settings = load_settings(page_size=args.page_size)
    except (ConfigurationError, Exception) as exc:
        print(format_configuration_error(exc), file=sys.stderr)
        return 2

    # 2. Настройка логирования
    log_level = args.log_level or settings.log_level
    setup_logging(log_level)

    # 3. Определение ID запуска
    launch_id = args.launch_id
    launch_name = getattr(args, "launch_name", None)

    if launch_id is None and not launch_name:
        logger.error(
            "Не указан launch_id или --launch-name. "
            "Примеры: alla 12345  или  alla --launch-name 'Прогон тестов'"
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
            # Резолв имени запуска в ID через API (если передан --launch-name)
            if launch_id is None and launch_name:
                # --project-id имеет приоритет над ALLURE_PROJECT_ID
                effective_project_id = getattr(args, "project_id", None) or settings.project_id
                launch_id = await client.find_launch_by_name(
                    launch_name,
                    project_id=effective_project_id,
                )

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
            llm_launch_summary = result.llm_launch_summary

            # HTML-отчёт + прикрепление ссылки к запуску
            # (внутри async with — клиент ещё открыт, нужен для PATCH)
            html_report_file = getattr(args, "html_report_file", None) or f"alla_report_{launch_id}.html"
            html_content = build_html_report_content(result, settings=settings)
            save_html_report(html_report_file, html_content)

            report_url = resolve_report_url(
                settings,
                report_url_override=getattr(args, "report_url", None),
            )
            await attach_report_link(
                client,
                launch_id=launch_id,
                settings=settings,
                report_url=report_url,
            )

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

        output = build_analysis_response(result)
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    else:
        from alla.models.onboarding import OnboardingMode

        print_text_report(report)
        if result.onboarding.mode == OnboardingMode.GUIDED:
            print(
                "[Onboarding] Проект ещё не обучен. "
                "Откройте HTML-отчёт и добавьте знание хотя бы для одного из крупных кластеров."
            )
            print()
        elif result.onboarding.mode == OnboardingMode.KB_NOT_CONFIGURED:
            print(
                "[Onboarding] Проектная память отключена. "
                "Задайте ALLURE_KB_POSTGRES_DSN, чтобы сохранять знания по проекту."
            )
            print()
        if clustering_report is not None:
            print_clustering_report(
                clustering_report, kb_results, llm_result,
                failed_tests=report.failed_tests,
            )
        if llm_launch_summary is not None and llm_launch_summary.summary_text:
            print_launch_summary(llm_launch_summary.summary_text)
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
    from alla.app_support import (
        collect_test_case_ids,
        filter_failed_results,
        format_configuration_error,
        load_settings,
    )
    from alla.clients.auth import AllureAuthManager
    from alla.clients.base import CommentManager
    from alla.clients.testops_client import AllureTestOpsClient
    from alla.exceptions import AllaError, ConfigurationError
    from alla.logging_config import setup_logging
    from alla.services.comment_delete_service import CommentDeleteService

    # 1. Загрузка настроек
    try:
        settings = load_settings()
    except (ConfigurationError, Exception) as exc:
        print(format_configuration_error(exc), file=sys.stderr)
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

            failed_results = filter_failed_results(all_results)
            test_case_ids, skipped = collect_test_case_ids(failed_results)

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
