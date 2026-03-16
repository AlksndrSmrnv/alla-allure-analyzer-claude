"""HTTP-сервер alla — REST API для анализа запусков Allure TestOps."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from alla import __version__
from alla.utils.text_normalization import canonicalize_kb_error_example

logger = logging.getLogger(__name__)


# --- Модели ответов ---


class AnalysisResponse(BaseModel):
    """JSON-ответ POST /api/v1/analyze/{launch_id}."""

    triage_report: dict[str, Any]
    onboarding: dict[str, Any] | None = None
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
        self.report_store: Any = None


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
    settings.resolve_secrets()
    settings.validate_required()
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

    if settings.reports_dir:
        from pathlib import Path

        Path(settings.reports_dir).mkdir(parents=True, exist_ok=True)
        logger.info("Директория отчётов: %s", Path(settings.reports_dir).resolve())

    if settings.reports_postgres and settings.kb_postgres_dsn:
        from alla.report.report_store import PostgresReportStore

        _state.report_store = PostgresReportStore(dsn=settings.kb_postgres_dsn)
        logger.info("Хранилище отчётов: PostgreSQL")

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


# --- Вспомогательные функции ---


def _build_csp_headers() -> dict[str, str]:
    """CSP-заголовки для HTML-отчётов с feedback API."""
    if not _state.settings.kb_active or not _state.settings.feedback_server_url:
        return {}
    feedback_url = _state.settings.feedback_server_url
    return {
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' {feedback_url}; "
            "img-src 'self' data:;"
        )
    }


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
        "onboarding": result.onboarding.model_dump(),
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
            "kb_bypass_count": result.llm_result.kb_bypass_count,
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


@app.get(
    "/api/v1/launch/resolve",
    responses={
        401: {"model": ErrorResponse, "description": "Ошибка аутентификации"},
        404: {"model": ErrorResponse, "description": "Запуск не найден"},
        502: {"model": ErrorResponse, "description": "Ошибка Allure TestOps API"},
    },
)
async def resolve_launch(name: str, project_id: int | None = None) -> dict[str, int]:
    """Найти ID запуска по точному совпадению имени.

    Используется Jenkins-пайплайном для резолва ``launchName`` из вебхука в числовой ID.
    Возвращает ``{"launch_id": 12345}``.
    """
    from alla.exceptions import AllaError, AllureApiError, AuthenticationError

    try:
        launch_id = await _state.client.find_launch_by_name(name, project_id=project_id)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except AllureApiError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    except AllaError as exc:
        # find_launch_by_name бросает AllaError (не AllureApiError), когда запуск не найден по имени
        raise HTTPException(status_code=404, detail=str(exc))

    return {"launch_id": launch_id}


@app.post(
    "/api/v1/analyze/{launch_id}/html",
    response_class=HTMLResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Ошибка аутентификации"},
        404: {"model": ErrorResponse, "description": "Запуск не найден"},
        502: {"model": ErrorResponse, "description": "Ошибка Allure TestOps API"},
    },
)
async def analyze_launch_html(launch_id: int, report_url: str = "") -> HTMLResponse:
    """Анализ запуска — возвращает self-contained HTML-отчёт.

    Запускает полный pipeline (триаж → кластеризация → KB → LLM) и
    возвращает готовый HTML-файл. Используется Jenkins-пайплайном:
    результат сохраняется как ``alla-report.html`` и публикуется как артефакт.

    Query parameter ``report_url`` — URL артефакта в Jenkins. Если задан,
    прикрепляется к прогону в Allure TestOps через ``PATCH /api/launch/{id}``
    (секция Links), чтобы ссылка на HTML-отчёт была видна прямо в TestOps UI.
    Переопределяет ``ALLURE_REPORT_URL`` из конфига.
    """
    from alla.clients.base import LaunchLinksUpdater
    from alla.exceptions import (
        AllureApiError,
        AuthenticationError,
        ConfigurationError,
        KnowledgeBaseError,
        PaginationLimitError,
    )
    from alla.orchestrator import analyze_launch as run_analysis
    from alla.report.html_report import generate_html_report

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

    feedback_api_url = ""
    if _state.settings.kb_active:
        feedback_api_url = _state.settings.feedback_server_url

    metrics_api_url = ""
    if _state.settings.metrics_active:
        metrics_api_url = (
            _state.settings.feedback_server_url
            or _state.settings.server_external_url
        )
        if not metrics_api_url:
            logger.warning(
                "ALLURE_METRICS_ENABLED=true, но ни ALLURE_FEEDBACK_SERVER_URL, "
                "ни ALLURE_SERVER_EXTERNAL_URL не заданы — JS-трекинг метрик "
                "не будет встроен в отчёт",
            )

    # Сгенерировать имя файла до создания HTML — оно встраивается в JS метрик.
    from datetime import datetime

    report_filename: str | None = None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    need_filename = (
        _state.settings.reports_dir
        or _state.report_store
        or _state.settings.metrics_active
    )
    if need_filename:
        report_filename = f"{launch_id}_{timestamp}.html"

    html = generate_html_report(
        result,
        endpoint=_state.settings.endpoint,
        feedback_api_url=feedback_api_url,
        metrics_api_url=metrics_api_url,
        report_filename=report_filename,
    )

    if _state.settings.reports_dir and report_filename:
        from pathlib import Path

        report_path = Path(_state.settings.reports_dir) / report_filename
        report_path.write_text(html, encoding="utf-8")
        logger.info("HTML-отчёт сохранён: %s", report_path)

    if _state.report_store and report_filename:
        _state.report_store.save(report_filename, launch_id, html)
        logger.info("HTML-отчёт сохранён в PostgreSQL: %s", report_filename)

    # Прикрепить ссылку на HTML-отчёт к прогону в TestOps.
    # Приоритет: query param → auto из server_external_url (только если reports_dir задан) → config fallback.
    if report_url:
        effective_report_url = report_url
        logger.info("URL отчёта (query param): %s", effective_report_url)
    elif _state.settings.server_external_url and report_filename:
        ext = _state.settings.server_external_url.rstrip("/")
        effective_report_url = f"{ext}/reports/{report_filename}"
        logger.info("URL отчёта (auto): %s", effective_report_url)
    else:
        effective_report_url = _state.settings.report_url
        if not effective_report_url:
            logger.warning(
                "Ссылка на отчёт не будет прикреплена к запуску #%d: "
                "задайте ALLURE_SERVER_EXTERNAL_URL + ALLURE_REPORTS_DIR "
                "или ALLURE_REPORT_URL",
                launch_id,
            )

    if effective_report_url and isinstance(_state.client, LaunchLinksUpdater):
        try:
            await _state.client.patch_launch_links(
                launch_id=launch_id,
                name=_state.settings.report_link_name,
                url=effective_report_url,
            )
            logger.info(
                "Ссылка на отчёт прикреплена к запуску #%d: %s",
                launch_id,
                effective_report_url,
            )
        except Exception as exc:
            logger.warning(
                "Не удалось прикрепить ссылку на HTML-отчёт к запуску #%d: %s",
                launch_id,
                exc,
            )

    headers = _build_csp_headers()
    if effective_report_url:
        headers["X-Report-URL"] = effective_report_url
    return HTMLResponse(content=html, headers=headers)


@app.get(
    "/reports/{filename}",
    response_class=HTMLResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Некорректное имя файла"},
        404: {"model": ErrorResponse, "description": "Отчёт не найден"},
    },
)
async def get_report(filename: str) -> HTMLResponse:
    """Отдать ранее сгенерированный HTML-отчёт по имени файла."""
    from pathlib import Path

    if "/" in filename or "\\" in filename or not filename.endswith(".html"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Попробовать PostgreSQL.
    if _state.report_store:
        content = _state.report_store.load(filename)
        if content is not None:
            return HTMLResponse(content=content, headers=_build_csp_headers())

    # Fallback на файловую систему.
    if _state.settings.reports_dir:
        report_path = Path(_state.settings.reports_dir) / filename
        if report_path.is_file():
            content = report_path.read_text(encoding="utf-8")
            return HTMLResponse(content=content, headers=_build_csp_headers())

    raise HTTPException(status_code=404, detail=f"Отчёт '{filename}' не найден")


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
    if settings is None or not settings.kb_active:
        return None

    if not hasattr(_state, "_feedback_store"):
        from alla.knowledge.postgres_feedback import PostgresFeedbackStore

        _state._feedback_store = PostgresFeedbackStore(dsn=settings.kb_postgres_dsn)
    return _state._feedback_store


@app.post("/api/v1/kb/feedback")
def submit_feedback(request: dict[str, Any]) -> dict[str, Any]:
    """Записать like/dislike для KB-совпадения из HTML-отчёта.

    Привязка выполняется по stable issue signature, а audit_text хранится
    только для последующего разбора человеком.
    """
    store = _get_feedback_store()
    if store is None:
        raise HTTPException(
            status_code=501,
            detail="Feedback requires ALLURE_KB_POSTGRES_DSN to be set",
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


def _make_slug(title: str, error_example: str) -> str:
    """Сгенерировать slug из заголовка + хэша error_example.

    Формат: <slugified_title>_<8 hex chars of sha256(error_example)>
    """
    import hashlib
    import re

    base = re.sub(r"[^a-z0-9]+", "_", title.lower())
    base = base.strip("_")[:50] or "kb_entry"
    suffix = hashlib.sha256(error_example.encode()).hexdigest()[:8]
    return f"{base}_{suffix}"


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

    req.error_example = canonicalize_kb_error_example(req.error_example or "")

    # Авто-генерация title и id если не указаны
    if not req.title:
        first_line = (req.error_example or "").splitlines()[0][:100] if req.error_example else ""
        req.title = first_line or "KB Entry"
    if not req.id:
        req.id = _make_slug(req.title, req.error_example or "")

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

    resp = CreateKBEntryResponse(
        entry_id=entry_id,
        id=req.id,
        title=req.title,
        category=req.category,
        created=True,
    )
    return resp.model_dump()


@app.put("/api/v1/kb/entries/{entry_id}")
def update_kb_entry(entry_id: int, request: dict[str, Any]) -> dict[str, Any]:
    """Обновить существующую запись базы знаний по entry_id."""
    store = _get_feedback_store()
    if store is None:
        raise HTTPException(
            status_code=501,
            detail="KB entry update requires postgres backend",
        )

    allowed = {"title", "description", "error_example", "category", "resolution_steps"}
    fields = {k: v for k, v in request.items() if k in allowed}
    if not fields:
        raise HTTPException(status_code=422, detail="No valid fields to update")

    if "category" in fields:
        from alla.knowledge.models import RootCauseCategory
        try:
            RootCauseCategory(fields["category"])
        except ValueError:
            valid = [e.value for e in RootCauseCategory]
            raise HTTPException(
                status_code=422,
                detail=f"Invalid category '{fields['category']}'. Valid: {valid}",
            )

    if "error_example" in fields and fields["error_example"]:
        fields["error_example"] = canonicalize_kb_error_example(fields["error_example"])

    from alla.exceptions import KnowledgeBaseError

    try:
        updated = store.update_kb_entry(entry_id, fields)
    except KnowledgeBaseError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not updated:
        raise HTTPException(status_code=404, detail=f"Entry {entry_id} not found")

    return {"entry_id": entry_id, "updated": True}


@app.post("/api/v1/kb/feedback/resolve")
def resolve_feedback(request: dict[str, Any]) -> dict[str, Any]:
    """Найти актуальные голоса для пар (entry_id, issue_signature_hash).

    Используется HTML-отчётом при загрузке для инициализации кнопок.
    Учитывает только новые exact-memory записи; legacy feedback без
    issue_signature_hash здесь не участвует.

    Body: {"items": [{"kb_entry_id": 123, "issue_signature_hash": "..."}]}
    Response: {"votes": {"123": {"vote": "like"}}}
    """
    store = _get_feedback_store()
    if store is None:
        raise HTTPException(status_code=501, detail="Requires postgres backend")

    from alla.knowledge.feedback_models import FeedbackResolveRequest

    try:
        req = FeedbackResolveRequest(**request)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    items = [
        (
            it.kb_entry_id,
            it.issue_signature_hash,
            it.issue_signature_version,
            f"{it.kb_entry_id}:{it.cluster_id}",
        )
        for it in req.items
    ]
    resolved = store.resolve_votes(items)

    votes: dict[str, dict[str, object]] = {}
    for resolve_key, (vote, fb_id) in resolved.items():
        votes[resolve_key] = {
            "vote": vote.value,
            "feedback_id": fb_id,
        }

    return {"votes": votes}


# --- Report Metrics endpoints ---


def _get_metrics_store():
    """Вернуть PostgresMetricsStore или None (ленивая инициализация)."""
    settings = _state.settings
    if settings is None or not settings.metrics_active:
        return None

    if not hasattr(_state, "_metrics_store"):
        from alla.report.metrics_store import PostgresMetricsStore

        _state._metrics_store = PostgresMetricsStore(dsn=settings.kb_postgres_dsn)
    return _state._metrics_store


@app.post("/api/v1/metrics/events")
def record_metrics_events(request: dict[str, Any]) -> dict[str, Any]:
    """Принять batch событий использования HTML-отчёта.

    Вызывается из встроенного JS в HTML-отчёте. Best-effort:
    невалидные события молча пропускаются.
    """
    store = _get_metrics_store()
    if store is None:
        raise HTTPException(
            status_code=501,
            detail="Metrics requires ALLURE_METRICS_ENABLED=true and ALLURE_KB_POSTGRES_DSN",
        )

    session_id = request.get("session_id", "")
    launch_id = request.get("launch_id")
    events = request.get("events", [])

    if not session_id or not isinstance(session_id, str) or len(session_id) > 32:
        raise HTTPException(status_code=400, detail="Invalid session_id")
    if not isinstance(launch_id, int) or launch_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid launch_id")
    if not isinstance(events, list) or len(events) == 0 or len(events) > 50:
        raise HTTPException(status_code=400, detail="events: 1-50 items required")

    accepted = store.record_events(
        session_id=session_id,
        launch_id=launch_id,
        events=events,
        report_filename=request.get("report_filename"),
        project_id=request.get("project_id"),
    )
    return {"accepted": accepted}


@app.get("/api/v1/metrics/launch/{launch_id}")
def get_launch_metrics(launch_id: int) -> dict[str, Any]:
    """Агрегированные метрики использования отчётов для запуска."""
    store = _get_metrics_store()
    if store is None:
        raise HTTPException(
            status_code=501,
            detail="Metrics requires ALLURE_METRICS_ENABLED=true and ALLURE_KB_POSTGRES_DSN",
        )
    return store.get_launch_metrics(launch_id)


@app.get("/api/v1/metrics/project/{project_id}")
def get_project_metrics(project_id: int, days: int = 30) -> dict[str, Any]:
    """Агрегированные метрики использования отчётов для проекта."""
    store = _get_metrics_store()
    if store is None:
        raise HTTPException(
            status_code=501,
            detail="Metrics requires ALLURE_METRICS_ENABLED=true and ALLURE_KB_POSTGRES_DSN",
        )
    if days < 1 or days > 90:
        raise HTTPException(status_code=400, detail="days must be between 1 and 90")
    return store.get_project_metrics(project_id, days=days)


def main() -> None:
    """Точка входа консольного скрипта alla-server."""
    import sys

    from alla.config import Settings

    try:
        settings = Settings()
        settings.resolve_secrets()
        settings.validate_required()
    except Exception as exc:
        print(
            f"Ошибка конфигурации: {exc}\n\n"
            f"Обязательные переменные окружения: "
            f"ALLURE_ENDPOINT, ALLURE_TOKEN\n"
            f"Секреты можно получить из Vault Proxy (ALLURE_VAULT_URL).\n"
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
