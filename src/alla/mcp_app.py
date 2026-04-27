"""MCP-сервер alla.

Монтируется внутрь FastAPI-приложения ``alla-server`` (см. ``server.py``)
и предоставляет два инструмента для qwen CLI и других MCP-клиентов:

* ``analyze_launch`` — компактный JSON-результат анализа прогона.
* ``analyze_launch_html`` — то же + сохранённый HTML-отчёт и публичный URL.

Транспорт — streamable HTTP по адресу ``/mcp``.
Конфигурация и клиент TestOps берутся из глобального ``server._state``,
поэтому MCP не открывает собственных HTTP-клиентов.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from alla.orchestrator import AnalysisResult

logger = logging.getLogger(__name__)

mcp = FastMCP("alla", stateless_http=True, streamable_http_path="/")


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _build_compact_summary(
    result: "AnalysisResult",
    *,
    report_url: str | None,
) -> dict[str, Any]:
    """Свернуть AnalysisResult в компактный JSON для агента.

    Полный ``AnalysisResult`` слишком большой для контекста LLM, поэтому
    оставляем по кластерам: id/label/size, краткую сигнатуру, top-N KB
    совпадений (title+score), краткий вердикт LLM.
    """
    triage = result.triage_report
    clustering = result.clustering_report
    kb_results = result.kb_results or {}
    llm_result = result.llm_result

    clusters_payload: list[dict[str, Any]] = []
    if clustering is not None:
        for cluster in clustering.clusters:
            kb_for_cluster = kb_results.get(cluster.cluster_id, [])
            llm_analysis = (
                llm_result.cluster_analyses.get(cluster.cluster_id)
                if llm_result is not None
                else None
            )
            clusters_payload.append(
                {
                    "id": cluster.cluster_id,
                    "label": cluster.label,
                    "size": cluster.member_count,
                    "signature": _truncate(
                        cluster.signature.representative_message
                        or cluster.signature.message_pattern,
                        200,
                    ),
                    "example_step_path": cluster.example_step_path,
                    "kb_matches": [
                        {
                            "title": match.entry.title,
                            "score": round(match.score, 3),
                            "category": (
                                match.entry.category.value
                                if match.entry.category
                                else None
                            ),
                        }
                        for match in kb_for_cluster
                    ],
                    "llm_verdict": _truncate(
                        llm_analysis.analysis_text if llm_analysis else None,
                        400,
                    ),
                }
            )

    payload: dict[str, Any] = {
        "launch_id": triage.launch_id,
        "launch_name": triage.launch_name,
        "total_failed": triage.active_failure_count,
        "clusters_count": clustering.cluster_count if clustering else 0,
        "clusters": clusters_payload,
    }
    if result.llm_launch_summary is not None:
        payload["llm_launch_summary"] = _truncate(
            result.llm_launch_summary.summary_text, 1500
        )
    if report_url:
        payload["report_url"] = report_url
    return payload


async def _run_or_translate(
    launch_id: int,
    push_to_testops: bool | None,
) -> "AnalysisResult":
    """Прогнать анализ через server._run_analysis_or_raise и пере-поднять
    HTTPException как обычный RuntimeError — FastMCP отдаст его клиенту
    как JSON-RPC error.
    """
    from fastapi import HTTPException

    from alla.server import _run_analysis_or_raise

    try:
        return await _run_analysis_or_raise(
            launch_id, push_to_testops=push_to_testops
        )
    except HTTPException as exc:
        raise RuntimeError(f"alla {exc.status_code}: {exc.detail}") from exc


@mcp.tool()
async def analyze_launch(
    launch_id: int,
    push_to_testops: bool | None = None,
) -> dict[str, Any]:
    """Проанализировать прогон Allure TestOps по его ID.

    Запускает полный pipeline (триаж → кластеризация → база знаний → LLM),
    тот же путь, что ``POST /api/v1/analyze/{launch_id}``.

    Args:
        launch_id: числовой ID запуска (launch) в Allure TestOps.
        push_to_testops: переопределить ``ALLURE_PUSH_TO_TESTOPS``
            для этого вызова. ``None`` — использовать значение из конфига.

    Returns:
        Компактная сводка: счётчики падений, кластеры (label/size/signature),
        совпадения базы знаний и краткий LLM-вердикт по каждому кластеру.
    """
    result = await _run_or_translate(launch_id, push_to_testops)
    return _build_compact_summary(result, report_url=None)


@mcp.tool()
async def analyze_launch_html(
    launch_id: int,
    push_to_testops: bool | None = None,
) -> dict[str, Any]:
    """Проанализировать прогон и сгенерировать HTML-отчёт.

    Запускает тот же pipeline, что ``analyze_launch``, плюс сохраняет
    self-contained HTML-отчёт (в файловую систему и/или PostgreSQL,
    если настроено) и возвращает публичный URL.

    Args:
        launch_id: числовой ID запуска в Allure TestOps.
        push_to_testops: переопределить ``ALLURE_PUSH_TO_TESTOPS``.

    Returns:
        ``report_url`` — кликабельная ссылка на сохранённый отчёт
        (если задан ``ALLURE_SERVER_EXTERNAL_URL`` + ``ALLURE_REPORTS_DIR``
        или ``ALLURE_REPORTS_POSTGRES``), плюс ``report_filename`` и та же
        компактная сводка, что у ``analyze_launch`` — чтобы агент мог
        ответить пользователю без повторного вызова.
        Если хранилище отчётов не сконфигурировано, в поле ``hint``
        возвращается подсказка.
    """
    from datetime import datetime

    from alla import server as server_module
    from alla.app_support import (
        build_html_report_content,
        persist_generated_report,
        resolve_report_url,
    )

    state = server_module._state
    settings = state.settings

    result = await _run_or_translate(launch_id, push_to_testops)

    report_filename: str | None = None
    if settings.reports_dir or state.report_store:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        report_filename = f"{launch_id}_{timestamp}.html"

    html = build_html_report_content(result, settings=settings)
    persist_generated_report(
        html_content=html,
        launch_id=launch_id,
        report_filename=report_filename,
        settings=settings,
        report_store=state.report_store,
        project_id=result.triage_report.project_id,
    )

    report_url = resolve_report_url(
        settings,
        report_url_override=None,
        report_filename=report_filename,
    )

    payload = _build_compact_summary(result, report_url=report_url or None)
    payload["report_filename"] = report_filename
    if not report_url:
        payload["hint"] = (
            "Чтобы получать публичные ссылки на отчёт, задайте "
            "ALLURE_REPORTS_DIR + ALLURE_SERVER_EXTERNAL_URL "
            "или ALLURE_REPORTS_POSTGRES."
        )
    return payload


def build_mcp_app() -> Any:
    """ASGI-приложение MCP для монтажа в FastAPI (см. ``server.py``)."""
    return mcp.streamable_http_app()
