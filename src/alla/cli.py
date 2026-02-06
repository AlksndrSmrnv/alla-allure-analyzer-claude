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
            f"Ошибка конфигурации: {exc}\n\n"
            f"Обязательные переменные окружения: "
            f"ALLURE_ENDPOINT, ALLURE_TOKEN, ALLURE_PROJECT_ID\n"
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
            service = TriageService(client, settings)
            report = await service.analyze_launch(launch_id)
    except ConfigurationError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 2
    except AllaError as exc:
        logger.error("Ошибка: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
        return 130

    # 5. Кластеризация ошибок
    clustering_report = None
    if settings.clustering_enabled and report.failed_tests:
        from alla.services.clustering_service import ClusteringConfig, ClusteringService

        clustering_service = ClusteringService(
            ClusteringConfig(similarity_threshold=settings.clustering_threshold)
        )
        clustering_report = clustering_service.cluster_failures(
            launch_id, report.failed_tests,
        )

    # 6. Вывод отчёта
    if args.output_format == "json":
        import json

        output = {"triage_report": report.model_dump()}
        if clustering_report is not None:
            output["clustering_report"] = clustering_report.model_dump()
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    else:
        _print_text_report(report)
        if clustering_report is not None:
            _print_clustering_report(clustering_report)

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
    else:
        print("Падения не найдены.")

    print()


def _print_clustering_report(report: ClusteringReport) -> None:  # noqa: F821
    """Вывод отчёта кластеризации ошибок в stdout."""

    print(
        f"=== Кластеры падений "
        f"({report.cluster_count} уникальных проблем из {report.total_failures} падений) ==="
    )
    print()

    for i, cluster in enumerate(report.clusters, 1):
        cluster_lines = [
            f"Кластер #{i}: {cluster.label} ({cluster.member_count} тестов)",
            f"ID кластера: {cluster.cluster_id}",
        ]
        if cluster.example_message:
            msg = _normalize_single_line(cluster.example_message)
            if len(msg) > 200:
                msg = msg[:200] + "..."
            cluster_lines.append(f"Пример: {msg}")
        ids_str = ", ".join(str(tid) for tid in cluster.member_test_ids[:10])
        if len(cluster.member_test_ids) > 10:
            ids_str += ", ..."
        cluster_lines.append(f"Тесты: {ids_str}")

        for line in _render_box(cluster_lines):
            print(line)
        print()


def _normalize_single_line(value: str) -> str:
    """Схлопнуть переводы строк/табуляцию в одну строку для рамочного вывода."""
    return " ".join(value.replace("\t", " ").split())


def _render_box(lines: list[str]) -> list[str]:
    """Отрендерить список строк в Unicode-рамку."""
    if not lines:
        return []

    width = max(len(line) for line in lines)
    top = f"╔{'═' * (width + 2)}╗"
    bottom = f"╚{'═' * (width + 2)}╝"
    body = [f"║ {line.ljust(width)} ║" for line in lines]

    return [top, *body, bottom]


def main() -> None:
    """Синхронная точка входа для CLI."""
    parser = build_parser()
    args = parser.parse_args()
    exit_code = asyncio.run(async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
