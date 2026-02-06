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
        help="ID запуска для анализа (переопределяет ALLURE_LAUNCH_ID если задан)",
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
    from alla.services.triage_service import TriageService

    # 1. Загрузка настроек
    try:
        overrides: dict[str, object] = {}
        if args.page_size is not None:
            overrides["page_size"] = args.page_size
        settings = Settings(**overrides)  # type: ignore[arg-type]
    except Exception as exc:
        # pydantic-settings выбрасывает ValidationError при отсутствии обязательных полей
        print(
            f"Configuration error: {exc}\n\n"
            f"Required env vars: ALLURE_ENDPOINT, ALLURE_TOKEN, ALLURE_PROJECT_ID\n"
            f"See .env.example for details.",
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
            "No launch_id provided. Pass it as a positional argument: alla <launch_id>"
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
            service = TriageService(client, settings)
            report = await service.analyze_launch(launch_id)
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        return 2
    except AllaError as exc:
        logger.error("Error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130

    # 5. Вывод отчёта
    if args.output_format == "json":
        print(report.model_dump_json(indent=2))
    else:
        _print_text_report(report)

    return 0


def _print_text_report(report: TriageReport) -> None:  # noqa: F821
    """Вывод человекочитаемого отчёта триажа в stdout."""

    print()
    print("=== Allure Triage Report ===")
    launch_label = f"Launch: #{report.launch_id}"
    if report.launch_name:
        launch_label += f" ({report.launch_name})"
    print(launch_label)
    print(
        f"Total: {report.total_results}"
        f" | Passed: {report.passed_count}"
        f" | Failed: {report.failed_count}"
        f" | Broken: {report.broken_count}"
        f" | Skipped: {report.skipped_count}"
        f" | Unknown: {report.unknown_count}"
    )
    print()

    if report.failed_tests:
        print(f"Failures ({report.failure_count}):")
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
    else:
        print("No failures found.")

    print()


def main() -> None:
    """Синхронная точка входа для CLI."""
    parser = build_parser()
    args = parser.parse_args()
    exit_code = asyncio.run(async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
