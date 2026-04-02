"""Вспомогательные функции для CLI и HTTP точек входа."""

import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, cast

from alla.clients.base import LaunchLinksUpdater
from alla.models.common import TestStatus
from alla.models.testops import TestResultResponse
from alla.report.html_report import generate_html_report

if TYPE_CHECKING:
    from alla.config import Settings
    from alla.orchestrator import AnalysisResult

logger = logging.getLogger(__name__)


def format_configuration_error(exc: Exception) -> str:
    """Формирует единообразное сообщение об ошибке конфигурации для CLI/сервера."""
    return (
        f"Ошибка конфигурации: {exc}\n\n"
        f"Обязательные переменные окружения: "
        f"ALLURE_ENDPOINT, ALLURE_TOKEN\n"
        f"Секреты можно получить из Vault Proxy (ALLURE_VAULT_URL).\n"
        f"Подробности см. в .env.example."
    )


def load_settings(*, page_size: int | None = None) -> "Settings":
    """Загружает настройки, разрешает секреты и проверяет обязательные значения."""
    from alla.config import Settings

    settings_cls = cast(Any, Settings)
    settings = cast(
        "Settings",
        settings_cls() if page_size is None else settings_cls(page_size=page_size),
    )
    settings.resolve_secrets()
    settings.validate_required()
    return settings


def build_analysis_response(result: "AnalysisResult") -> dict[str, Any]:
    """Преобразует AnalysisResult в общую JSON-структуру для CLI/сервера."""
    payload: dict[str, Any] = {
        "triage_report": result.triage_report.model_dump(),
        "onboarding": result.onboarding.model_dump(),
    }

    if result.clustering_report is not None:
        payload["clustering_report"] = result.clustering_report.model_dump()

    if result.kb_results:
        payload["kb_matches"] = {
            cluster_id: [match.model_dump() for match in matches]
            for cluster_id, matches in result.kb_results.items()
        }

    if result.kb_push_result is not None:
        payload["kb_push_result"] = asdict(result.kb_push_result)

    if result.llm_result is not None:
        payload["llm_result"] = {
            "total_clusters": result.llm_result.total_clusters,
            "analyzed_count": result.llm_result.analyzed_count,
            "failed_count": result.llm_result.failed_count,
            "skipped_count": result.llm_result.skipped_count,
            "kb_bypass_count": result.llm_result.kb_bypass_count,
            "cluster_analyses": {
                cluster_id: analysis.model_dump()
                for cluster_id, analysis in result.llm_result.cluster_analyses.items()
            },
            "token_usage": asdict(result.llm_result.token_usage),
        }

    if result.llm_push_result is not None:
        payload["llm_push_result"] = asdict(result.llm_push_result)

    if result.llm_launch_summary is not None:
        payload["llm_launch_summary"] = {
            "summary_text": result.llm_launch_summary.summary_text,
            "error": result.llm_launch_summary.error,
            "token_usage": asdict(result.llm_launch_summary.token_usage),
        }

    return payload


def get_feedback_api_url(settings: "Settings") -> str:
    """Возвращает URL API обратной связи только когда база знаний активна."""
    return settings.feedback_server_url if settings.kb_active else ""


def build_html_report_content(
    result: "AnalysisResult",
    *,
    settings: "Settings",
) -> str:
    """Генерирует самодостаточный HTML-отчёт для результата анализа."""
    return generate_html_report(
        result,
        endpoint=settings.endpoint,
        feedback_api_url=get_feedback_api_url(settings),
    )


def save_html_report(path: str | Path, html_content: str) -> None:
    """Записывает содержимое HTML-отчёта в файл."""
    report_path = Path(path)
    report_path.write_text(html_content, encoding="utf-8")
    logger.info("HTML-отчёт сохранён: %s", report_path)


def persist_generated_report(
    *,
    html_content: str,
    launch_id: int,
    report_filename: str | None,
    settings: "Settings",
    report_store: Any = None,
) -> None:
    """Сохраняет сгенерированный отчёт в файловую систему/PostgreSQL при наличии настроек."""
    if settings.reports_dir and report_filename:
        report_path = Path(settings.reports_dir) / report_filename
        report_path.write_text(html_content, encoding="utf-8")
        logger.info("HTML-отчёт сохранён: %s", report_path)

    if report_store is not None and report_filename:
        report_store.save(report_filename, launch_id, html_content)
        logger.info("HTML-отчёт сохранён в PostgreSQL: %s", report_filename)


def resolve_report_url(
    settings: "Settings",
    *,
    report_url_override: str | None = None,
    report_filename: str | None = None,
) -> str:
    """Определяет публичный URL отчёта для CLI/сервера."""
    if report_url_override:
        logger.info("URL отчёта (override): %s", report_url_override)
        return report_url_override

    if report_filename and settings.server_external_url:
        external_url = settings.server_external_url.rstrip("/")
        report_url = f"{external_url}/reports/{report_filename}"
        logger.info("URL отчёта (auto): %s", report_url)
        return report_url

    if settings.report_url:
        logger.info("URL отчёта (config): %s", settings.report_url)
    return settings.report_url


async def attach_report_link(
    client: object,
    *,
    launch_id: int,
    settings: "Settings",
    report_url: str,
) -> None:
    """Прикрепляет ссылку на отчёт к запуску, если клиент это поддерживает."""
    if not report_url:
        return
    if not isinstance(client, LaunchLinksUpdater):
        return

    try:
        await client.patch_launch_links(
            launch_id=launch_id,
            name=settings.report_link_name,
            url=report_url,
        )
        logger.info(
            "Ссылка на отчёт прикреплена к запуску #%d: %s",
            launch_id,
            report_url,
        )
    except Exception as exc:
        logger.warning(
            "Не удалось прикрепить ссылку на HTML-отчёт к запуску #%d: %s",
            launch_id,
            exc,
        )


def filter_failed_results(
    results: Iterable[TestResultResponse],
) -> list[TestResultResponse]:
    """Выбирает упавшие/сломанные результаты тестов для очистки комментариев."""
    failure_statuses = {status.value for status in TestStatus.failure_statuses()}
    return [
        result
        for result in results
        if result.status and result.status.lower() in failure_statuses
    ]


def collect_test_case_ids(
    results: Iterable[TestResultResponse],
) -> tuple[set[int], int]:
    """Собирает уникальные test_case_id и считает результаты без test_case_id."""
    test_case_ids: set[int] = set()
    skipped = 0

    for result in results:
        if result.test_case_id is not None:
            test_case_ids.add(result.test_case_id)
        else:
            skipped += 1

    return test_case_ids, skipped
