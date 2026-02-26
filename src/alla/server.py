"""HTTP-сервер alla — REST API для анализа запусков Allure TestOps."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from alla import __version__

logger = logging.getLogger(__name__)


# --- Модели ответов ---


class AnalysisResponse(BaseModel):
    """JSON-ответ POST /api/v1/analyze/{launch_id}."""

    triage_report: dict[str, Any]
    clustering_report: dict[str, Any] | None = None
    kb_matches: dict[str, list[dict[str, Any]]] | None = None
    kb_push_result: dict[str, Any] | None = None
    llm_result: dict[str, Any] | None = None
    llm_push_result: dict[str, Any] | None = None
    llm_launch_summary: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    """JSON-ответ GET /health."""

    status: str
    version: str


class DeleteCommentsResponse(BaseModel):
    """JSON-ответ DELETE /api/v1/comments/{launch_id}."""

    total_test_cases: int
    comments_found: int
    comments_deleted: int
    comments_failed: int
    skipped_test_cases: int
    dry_run: bool


class ErrorResponse(BaseModel):
    """Стандартный ответ при ошибке."""

    detail: str


# --- Состояние приложения ---


class _AppState:
    """Долгоживущие объекты, разделяемые между запросами."""

    def __init__(self) -> None:
        self.settings: Any = None
        self.client: Any = None
        self.auth: Any = None


_state = _AppState()


# --- Lifespan ---


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    """Инициализация при старте, очистка при остановке."""
    from alla.clients.auth import AllureAuthManager
    from alla.clients.testops_client import AllureTestOpsClient
    from alla.config import Settings
    from alla.logging_config import setup_logging

    settings = Settings()
    setup_logging(settings.log_level)

    logger.info("alla server v%s запускается", __version__)

    auth = AllureAuthManager(
        endpoint=settings.endpoint,
        api_token=settings.token,
        timeout=settings.request_timeout,
        ssl_verify=settings.ssl_verify,
    )
    client = AllureTestOpsClient(settings, auth)

    _state.settings = settings
    _state.client = client
    _state.auth = auth

    yield

    logger.info("alla server останавливается")
    await client.close()


# --- FastAPI ---


app = FastAPI(
    title="alla",
    description="AI-агент триажа упавших тестов — REST API",
    version=__version__,
    lifespan=_lifespan,
)

# CORS: HTML-отчёт может открываться с file:// или с Jenkins (другой origin).
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Маршруты ---


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Проверка работоспособности сервера."""
    return HealthResponse(status="ok", version=__version__)


@app.post(
    "/api/v1/analyze/{launch_id}",
    response_model=AnalysisResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Ошибка аутентификации"},
        404: {"model": ErrorResponse, "description": "Запуск не найден"},
        502: {"model": ErrorResponse, "description": "Ошибка Allure TestOps API"},
    },
)
async def analyze_launch(launch_id: int) -> dict[str, Any]:
    """Анализ запуска — эквивалент ``alla <launch_id> --output-format json``.

    Запускает полный pipeline: триаж → кластеризация → KB-поиск →
    LLM-анализ → LLM-push → KB-push (fallback). Возвращает объединённый JSON-результат.
    """
    from alla.exceptions import (
        AllureApiError,
        AuthenticationError,
        ConfigurationError,
        KnowledgeBaseError,
        PaginationLimitError,
    )
    from alla.orchestrator import analyze_launch as run_analysis

    try:
        result = await run_analysis(
            launch_id=launch_id,
            client=_state.client,
            settings=_state.settings,
            updater=_state.client,
        )
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except AllureApiError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KnowledgeBaseError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except PaginationLimitError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    response: dict[str, Any] = {
        "triage_report": result.triage_report.model_dump(),
    }

    if result.clustering_report is not None:
        response["clustering_report"] = result.clustering_report.model_dump()

    if result.kb_results:
        response["kb_matches"] = {
            cid: [m.model_dump() for m in matches]
            for cid, matches in result.kb_results.items()
        }

    if result.kb_push_result is not None:
        response["kb_push_result"] = asdict(result.kb_push_result)

    if result.llm_result is not None:
        response["llm_result"] = {
            "total_clusters": result.llm_result.total_clusters,
            "analyzed_count": result.llm_result.analyzed_count,
            "failed_count": result.llm_result.failed_count,
            "skipped_count": result.llm_result.skipped_count,
            "cluster_analyses": {
                cid: a.model_dump()
                for cid, a in result.llm_result.cluster_analyses.items()
            },
        }

    if result.llm_push_result is not None:
        response["llm_push_result"] = asdict(result.llm_push_result)

    if result.llm_launch_summary is not None:
        response["llm_launch_summary"] = {
            "summary_text": result.llm_launch_summary.summary_text,
            "error": result.llm_launch_summary.error,
        }

    return response


@app.delete(
    "/api/v1/comments/{launch_id}",
    response_model=DeleteCommentsResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Ошибка аутентификации"},
        404: {"model": ErrorResponse, "description": "Запуск не найден"},
        502: {"model": ErrorResponse, "description": "Ошибка Allure TestOps API"},
    },
)
async def delete_comments(launch_id: int, dry_run: bool = False) -> dict[str, Any]:
    """Удалить комментарии alla для тестов указанного запуска.

    Сканирует failed/broken тесты запуска, находит комментарии с префиксом
    ``[alla]`` и удаляет их. Query parameter ``?dry_run=true`` для
    предварительного просмотра без фактического удаления.
    """
    from alla.clients.base import CommentManager
    from alla.exceptions import AllureApiError, AuthenticationError, PaginationLimitError
    from alla.services.comment_delete_service import CommentDeleteService

    client = _state.client
    if not isinstance(client, CommentManager):
        raise HTTPException(
            status_code=500,
            detail="Клиент не поддерживает управление комментариями",
        )

    try:
        all_results = await client.get_all_test_results_for_launch(launch_id)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except AllureApiError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    except PaginationLimitError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    failure_statuses = {"failed", "broken"}
    failed_results = [
        r for r in all_results
        if r.status and r.status.lower() in failure_statuses
    ]

    test_case_ids: set[int] = set()
    skipped = 0
    for r in failed_results:
        if r.test_case_id is not None:
            test_case_ids.add(r.test_case_id)
        else:
            skipped += 1

    service = CommentDeleteService(
        client,
        concurrency=_state.settings.detail_concurrency,
    )

    try:
        result = await service.delete_alla_comments(
            test_case_ids,
            dry_run=dry_run,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {
        "total_test_cases": result.total_test_cases,
        "comments_found": result.comments_found,
        "comments_deleted": result.comments_deleted,
        "comments_failed": result.comments_failed,
        "skipped_test_cases": skipped,
        "dry_run": dry_run,
    }


# --- KB Feedback endpoints ---


def _get_feedback_store():
    """Вернуть PostgresFeedbackStore или None (ленивая инициализация)."""
    settings = _state.settings
    if settings is None or not settings.kb_feedback_enabled:
        return None

    if not hasattr(_state, "_feedback_store"):
        from alla.knowledge.postgres_feedback import PostgresFeedbackStore

        _state._feedback_store = PostgresFeedbackStore(dsn=settings.kb_postgres_dsn)
    return _state._feedback_store


@app.post("/api/v1/kb/feedback")
def submit_feedback(request: dict[str, Any]) -> dict[str, Any]:
    """Записать like/dislike для KB-совпадения из HTML-отчёта."""
    store = _get_feedback_store()
    if store is None:
        raise HTTPException(
            status_code=501,
            detail="Feedback requires ALLURE_KB_FEEDBACK_ENABLED=true "
            "and ALLURE_KB_POSTGRES_DSN to be set",
        )

    from alla.knowledge.feedback_models import FeedbackRequest

    try:
        fb_request = FeedbackRequest(**request)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    from alla.exceptions import KnowledgeBaseError

    try:
        response = store.record_vote(fb_request)
    except KnowledgeBaseError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return response.model_dump()


@app.post("/api/v1/kb/entries", status_code=201)
def create_kb_entry(request: dict[str, Any]) -> dict[str, Any]:
    """Создать новую запись KB из HTML-отчёта."""
    store = _get_feedback_store()
    if store is None:
        raise HTTPException(
            status_code=501,
            detail="KB entry creation requires postgres backend",
        )

    from alla.knowledge.feedback_models import CreateKBEntryRequest, CreateKBEntryResponse
    from alla.knowledge.models import KBEntry

    try:
        req = CreateKBEntryRequest(**request)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    entry = KBEntry(
        id=req.id,
        title=req.title,
        description=req.description,
        error_example=req.error_example,
        category=req.category,
        resolution_steps=req.resolution_steps,
    )

    from alla.exceptions import KnowledgeBaseError

    try:
        entry_id = store.create_kb_entry(entry, req.project_id)
    except KnowledgeBaseError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if entry_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"KB entry with slug '{req.id}' already exists "
            f"for project_id={req.project_id}",
        )

    # Инвалидация KB-кэша — новая запись должна быть видна сразу
    from alla.orchestrator import _kb_cache

    _kb_cache.clear()

    resp = CreateKBEntryResponse(
        entry_id=entry_id,
        id=req.id,
        title=req.title,
        category=req.category,
        created=True,
    )
    return resp.model_dump()


@app.get("/api/v1/kb/feedback/{error_fingerprint}")
def get_feedback_for_fingerprint(
    error_fingerprint: str,
) -> dict[str, str]:
    """Получить текущие голоса для error_fingerprint.

    Используется HTML-отчётом для инициализации состояния кнопок.
    Возвращает ``{entry_id: "like"|"dislike"}``.
    """
    store = _get_feedback_store()
    if store is None:
        raise HTTPException(status_code=501, detail="Requires postgres backend")

    votes = store.get_votes_for_fingerprint(error_fingerprint)
    return {str(k): v.value for k, v in votes.items()}


def main() -> None:
    """Точка входа консольного скрипта alla-server."""
    import sys

    from alla.config import Settings

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
        sys.exit(2)

    import uvicorn

    uvicorn.run(
        "alla.server:app",
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
