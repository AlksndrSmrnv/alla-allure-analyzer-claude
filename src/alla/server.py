"""HTTP-сервер alla — REST API для анализа запусков Allure TestOps."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, TypeVar, cast
from uuid import uuid4

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from alla import __version__
from alla.app_support import (
    attach_report_link,
    build_analysis_response,
    build_html_report_content,
    collect_test_case_ids,
    filter_failed_results,
    format_configuration_error,
    load_settings,
    persist_generated_report,
    resolve_report_url,
)
from alla.knowledge.slug import make_kb_slug
from alla.utils.text_normalization import canonicalize_kb_error_example

if TYPE_CHECKING:
    from alla.config import Settings
    from alla.knowledge.models import KBEntry
    from alla.knowledge.postgres_feedback import PostgresFeedbackStore
    from alla.knowledge.merge_rules_store import PostgresMergeRulesStore
    from alla.orchestrator import AnalysisResult

logger = logging.getLogger(__name__)
ModelT = TypeVar("ModelT", bound=BaseModel)
ResultT = TypeVar("ResultT")


class _McpNoSlashRedirectMiddleware:
    """Провести /mcp в смонтированное MCP-приложение без видимого 307 redirect."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            rewritten_scope = cast(Scope, {**scope, "path": "/mcp/"})
            raw_path = rewritten_scope.get("raw_path")
            if isinstance(raw_path, bytes) and raw_path.endswith(b"/mcp"):
                rewritten_scope["raw_path"] = raw_path + b"/"
            scope = rewritten_scope

        await self.app(scope, receive, send)


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
    mcp: bool = True


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
        self.skill_report_store: Any = None
        self.report_view_store: Any = None
        self.feedback_store: Any = None
        self.merge_rules_store: Any = None
        self.dashboard_store: Any = None
        self.project_names_cache: dict[int, str] = {}
        self.project_names_expires_at: float = 0.0


_state = _AppState()


def _reset_lazy_stores_and_caches() -> None:
    """Сбросить ленивые storage-объекты и кэши, завязанные на Settings."""
    _state.report_store = None
    _state.skill_report_store = None
    _state.report_view_store = None
    _state.feedback_store = None
    _state.merge_rules_store = None
    _state.dashboard_store = None
    _state.project_names_cache = {}
    _state.project_names_expires_at = 0.0


# --- Жизненный цикл ---


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    """Инициализация при старте, очистка при остановке."""
    from alla.clients.auth import AllureAuthManager
    from alla.clients.testops_client import AllureTestOpsClient
    from alla.logging_config import setup_logging

    settings = load_settings()
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
    _reset_lazy_stores_and_caches()

    # Пересобираем process-wide LLM rate-coordinator с актуальными настройками,
    # чтобы он использовал свежие llm_concurrency/llm_request_delay.
    from alla.services.llm_rate_coordinator import reset_coordinator

    reset_coordinator()

    if settings.reports_dir:
        from pathlib import Path

        Path(settings.reports_dir).mkdir(parents=True, exist_ok=True)
        logger.info("Директория отчётов: %s", Path(settings.reports_dir).resolve())

    if settings.reports_postgres and settings.kb_postgres_dsn:
        from alla.report.report_store import PostgresReportStore

        _state.report_store = PostgresReportStore(dsn=settings.kb_postgres_dsn)
        logger.info("Хранилище отчётов: PostgreSQL")

    if settings.kb_postgres_dsn:
        from alla.report.report_store import PostgresReportViewStore

        _state.report_view_store = PostgresReportViewStore(dsn=settings.kb_postgres_dsn)
        logger.info("Учёт просмотров отчётов: PostgreSQL")

    # MCP session manager должен жить столько же, сколько FastAPI-приложение.
    # Starlette не пробрасывает lifespan смонтированных приложений, поэтому
    # стартуем менеджер вручную здесь.
    from alla.mcp_app import mcp as _mcp

    async with _mcp.session_manager.run():
        logger.info("MCP сервер смонтирован: %s/mcp", settings.server_external_url or "")
        yield

    logger.info("alla server останавливается")
    await client.close()


# --- FastAPI-приложение ---


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
    expose_headers=["X-Report-URL"],
)
app.add_middleware(_McpNoSlashRedirectMiddleware)

# MCP-эндпоинт для qwen CLI и других MCP-клиентов.
# Транспорт streamable HTTP, инструменты определены в alla/mcp_app.py.
from alla.mcp_app import build_mcp_app  # noqa: E402

app.mount("/mcp", build_mcp_app())


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


# Cookie, по которой дедуплицируются просмотры одного отчёта одним
# пользователем (перезагрузка страницы не добавляет +1 в дашборд).
_VIEWER_COOKIE_NAME = "alla_viewer_id"
_VIEWER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # ~1 год


def _resolve_viewer_id(request: Request) -> tuple[str, bool]:
    """Вернуть viewer_id из cookie или сгенерировать новый.

    Второй элемент кортежа = True, если cookie нужно выставить в ответе
    (т.е. её ещё не было).
    """
    existing = request.cookies.get(_VIEWER_COOKIE_NAME)
    if existing:
        return existing, False
    return uuid4().hex, True


def _set_viewer_cookie(response: Response, viewer_id: str) -> None:
    response.set_cookie(
        key=_VIEWER_COOKIE_NAME,
        value=viewer_id,
        max_age=_VIEWER_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


async def _record_report_view_best_effort(
    filename: str,
    *,
    viewer_id: str | None = None,
) -> None:
    """Записать просмотр отчёта, не влияя на успешную отдачу HTML."""
    store = _state.report_view_store
    if store is None:
        return
    try:
        await asyncio.to_thread(
            lambda: store.record_view(filename, viewer_id=viewer_id)
        )
    except Exception as exc:  # noqa: BLE001 - отдача отчёта важнее учёта
        logger.warning("report_view recording failed for %s: %s", filename, exc)


def _get_report_content_store() -> Any:
    """Стор для чтения/записи HTML-отчётов в ``alla.report``.

    Возвращает (по приоритету):
    1. ``_state.report_store`` — если включён ``ALLURE_REPORTS_POSTGRES``
       (его же использует основной analyze-flow);
    2. лениво создаваемый и кэшируемый стор, если задан DSN — нужен для
       skill-flow, который сохраняет отчёт в БД независимо от
       ``reports_postgres``, и для отдачи такого отчёта через ``/reports``.

    Кэшируется в отдельном поле ``skill_report_store``, чтобы не менять
    gating основного analyze-flow (он смотрит ровно на ``report_store``).
    Если стор создать/проинициализировать не удалось — возвращает ``None``.
    """
    if _state.report_store is not None:
        return _state.report_store
    settings = _state.settings
    if settings is None or not settings.kb_active:
        return None
    if _state.skill_report_store is None:
        from alla.report.report_store import PostgresReportStore

        try:
            _state.skill_report_store = PostgresReportStore(dsn=settings.kb_postgres_dsn)
        except Exception as exc:  # noqa: BLE001 - DDL/права/сеть
            logger.warning("Не удалось инициализировать report store: %s", exc)
            return None
    return _state.skill_report_store


def _settings() -> "Settings":
    """Вернуть инициализированные настройки сервера."""
    return cast("Settings", _state.settings)


def _effective_settings(
    *,
    push_comments: bool | None = None,
    push_report_link: bool | None = None,
) -> "Settings":
    """Вернуть настройки, опционально переопределив push-флаги на запрос."""
    settings = _settings()
    updates: dict[str, bool] = {}
    if push_comments is not None:
        updates["push_comments"] = push_comments
    if push_report_link is not None:
        updates["push_report_link"] = push_report_link
    if not updates:
        return settings
    return settings.model_copy(update=updates)


async def _run_analysis_or_raise(
    launch_id: int,
    *,
    push_comments: bool | None = None,
    push_report_link: bool | None = None,
) -> "AnalysisResult":
    """Запустить анализ orchestrator и смэппить доменные ошибки в HTTP-ошибки."""
    from alla.exceptions import (
        AllureApiError,
        AuthenticationError,
        ConfigurationError,
        KnowledgeBaseError,
        PaginationLimitError,
    )
    from alla.orchestrator import analyze_launch as run_analysis

    try:
        return await run_analysis(
            launch_id=launch_id,
            client=_state.client,
            settings=_effective_settings(
                push_comments=push_comments,
                push_report_link=push_report_link,
            ),
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
async def analyze_launch(
    launch_id: int,
    push_comments: bool | None = None,
    push_report_link: bool | None = None,
) -> dict[str, Any]:
    """Анализ запуска — эквивалент ``alla <launch_id> --output-format json``.

    Запускает полный pipeline: триаж → кластеризация → KB-поиск →
    LLM-анализ → LLM-push → KB-push (fallback). Возвращает объединённый JSON-результат.

    Query-параметр ``push_comments`` переопределяет ``ALLURE_PUSH_COMMENTS``;
    ``push_report_link`` — ``ALLURE_PUSH_REPORT_LINK``.
    """
    result = await _run_analysis_or_raise(
        launch_id,
        push_comments=push_comments,
        push_report_link=push_report_link,
    )
    return build_analysis_response(result)


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
async def analyze_launch_html(
    launch_id: int,
    report_url: str = "",
    push_comments: bool | None = None,
    push_report_link: bool | None = None,
) -> HTMLResponse:
    """Анализ запуска — возвращает self-contained HTML-отчёт.

    Запускает полный pipeline (триаж → кластеризация → KB → LLM) и
    возвращает готовый HTML-файл. Используется Jenkins-пайплайном:
    результат сохраняется как ``alla-report.html`` и публикуется как артефакт.

    Query parameter ``report_url`` — URL артефакта в Jenkins. Если задан,
    прикрепляется к прогону в Allure TestOps через ``PATCH /api/launch/{id}``
    (секция Links), чтобы ссылка на HTML-отчёт была видна прямо в TestOps UI.
    Переопределяет ``ALLURE_REPORT_URL`` из конфига.
    """
    result = await _run_analysis_or_raise(
        launch_id,
        push_comments=push_comments,
        push_report_link=push_report_link,
    )
    effective_settings = _effective_settings(
        push_comments=push_comments,
        push_report_link=push_report_link,
    )

    from datetime import datetime

    report_filename: str | None = None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    need_filename = effective_settings.reports_dir or _state.report_store
    if need_filename:
        report_filename = f"{launch_id}_{timestamp}.html"

    html = build_html_report_content(
        result,
        settings=effective_settings,
    )
    persist_generated_report(
        html_content=html,
        launch_id=launch_id,
        report_filename=report_filename,
        settings=effective_settings,
        report_store=_state.report_store,
        project_id=result.triage_report.project_id,
        analysis_result=result,
    )

    effective_report_url = resolve_report_url(
        effective_settings,
        report_url_override=report_url or None,
        report_filename=report_filename,
    )
    if not effective_report_url and effective_settings.push_report_link:
        logger.warning(
            "Ссылка на отчёт не будет прикреплена к запуску #%d: "
            "задайте ALLURE_SERVER_EXTERNAL_URL + ALLURE_REPORTS_DIR "
            "или ALLURE_REPORT_URL",
            launch_id,
        )

    if effective_report_url and effective_settings.push_report_link:
        await attach_report_link(
            _state.client,
            launch_id=launch_id,
            settings=effective_settings,
            report_url=effective_report_url,
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
async def get_report(filename: str, request: Request) -> HTMLResponse:
    """Отдать ранее сгенерированный HTML-отчёт по имени файла."""
    from pathlib import Path

    if "/" in filename or "\\" in filename or not filename.endswith(".html"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    viewer_id, set_cookie = _resolve_viewer_id(request)

    def _build_response(content: str) -> HTMLResponse:
        response = HTMLResponse(content=content, headers=_build_csp_headers())
        if set_cookie:
            _set_viewer_cookie(response, viewer_id)
        return response

    # Попробовать PostgreSQL (включая отчёты, сохранённые skill-flow при
    # выключенном ALLURE_REPORTS_POSTGRES).
    content_store = _get_report_content_store()
    if content_store is not None:
        content = content_store.load(filename)
        if content is not None:
            await _record_report_view_best_effort(filename, viewer_id=viewer_id)
            return _build_response(content)

    # Fallback на файловую систему.
    if _state.settings.reports_dir:
        report_path = Path(_state.settings.reports_dir) / filename
        if report_path.is_file():
            content = report_path.read_text(encoding="utf-8")
            await _record_report_view_best_effort(filename, viewer_id=viewer_id)
            return _build_response(content)

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
    from alla.clients.base import CommentCleanupProvider
    from alla.exceptions import AllureApiError, AuthenticationError, PaginationLimitError
    from alla.services.comment_delete_service import CommentDeleteService

    client = _state.client
    if not isinstance(client, CommentCleanupProvider):
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

    failed_results = filter_failed_results(all_results)
    test_case_ids, skipped = collect_test_case_ids(failed_results)

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


# --- Эндпоинты KB feedback ---


def _get_feedback_store() -> "PostgresFeedbackStore | None":
    """Вернуть PostgresFeedbackStore или None (ленивая инициализация)."""
    settings = _state.settings
    if settings is None or not settings.kb_active:
        return None

    if _state.feedback_store is None:
        from alla.knowledge.postgres_feedback import PostgresFeedbackStore

        _state.feedback_store = PostgresFeedbackStore(dsn=settings.kb_postgres_dsn)
    return cast("PostgresFeedbackStore", _state.feedback_store)


def _require_feedback_store(detail: str) -> "PostgresFeedbackStore":
    """Вернуть feedback store или поднять 501 с осмысленным сообщением."""
    store = _get_feedback_store()
    if store is None:
        raise HTTPException(status_code=501, detail=detail)
    return store


def _get_merge_rules_store() -> "PostgresMergeRulesStore | None":
    """Вернуть PostgresMergeRulesStore или None (ленивая инициализация)."""
    settings = _state.settings
    if settings is None or not settings.kb_active:
        return None

    if _state.merge_rules_store is None:
        from alla.knowledge.merge_rules_store import PostgresMergeRulesStore

        _state.merge_rules_store = PostgresMergeRulesStore(dsn=settings.kb_postgres_dsn)
    return cast("PostgresMergeRulesStore", _state.merge_rules_store)


def _require_merge_rules_store(detail: str) -> "PostgresMergeRulesStore":
    """Вернуть merge rules store или поднять 501 с осмысленным сообщением."""
    store = _get_merge_rules_store()
    if store is None:
        raise HTTPException(status_code=501, detail=detail)
    return store


_PROJECT_NAMES_TTL = 600.0  # секунд


def _get_dashboard_store() -> Any:
    """Вернуть DashboardStatsStore или None (ленивая инициализация)."""
    settings = _state.settings
    if settings is None or not settings.kb_active:
        return None
    if _state.dashboard_store is None:
        from alla.dashboard.stats_store import DashboardStatsStore

        _state.dashboard_store = DashboardStatsStore(dsn=settings.kb_postgres_dsn)
    return _state.dashboard_store


async def _get_project_names_cached() -> dict[int, str]:
    """Получить ``{project_id: name}`` из TestOps с TTL-кэшем.

    На любую ошибку TestOps возвращает прошлый кэш (если был) или ``{}``.
    Дашборд продолжит работать с лейблами вида ``Project #N``.
    """
    import time

    now = time.monotonic()
    if _state.project_names_cache and now < _state.project_names_expires_at:
        return _state.project_names_cache
    client = _state.client
    if client is None:
        return _state.project_names_cache or {}
    try:
        names = cast(dict[int, str], await client.list_projects())
    except Exception as exc:  # noqa: BLE001 - сетевые/HTTP ошибки разные
        logger.warning("Не удалось получить список проектов из TestOps: %s", exc)
        return _state.project_names_cache or {}
    _state.project_names_cache = names
    _state.project_names_expires_at = now + _PROJECT_NAMES_TTL
    return names


def _read_dashboard_stats(
    store: Any,
    *,
    window: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Синхронно прочитать dashboard-агрегации; вызывается из threadpool."""
    return (
        store.totals(window=window),
        store.per_project_rollup(window=window),
        store.reports_per_day(window=window),
    )


def _read_project_reports(
    store: Any,
    *,
    project_id: int | None,
    window: Any,
    limit: int,
) -> list[dict[str, Any]]:
    """Синхронно прочитать список отчётов проекта; вызывается из threadpool."""
    return store.reports_for_project(
        project_id=project_id,
        window=window,
        limit=limit,
    )


def _parse_request(model_cls: type[ModelT], request: dict[str, Any]) -> ModelT:
    """Преобразовать сырой JSON body в pydantic-модель или поднять 422."""
    try:
        return model_cls(**request)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _run_kb_action(action: Callable[[], ResultT]) -> ResultT:
    """Выполнить KB/storage action и отобразить domain error в HTTP 500."""
    from alla.exceptions import KnowledgeBaseError

    try:
        return action()
    except KnowledgeBaseError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/v1/merge-rules")
def create_merge_rules(request: dict[str, Any]) -> dict[str, Any]:
    """Создать или обновить правила объединения кластеров для проекта."""
    store = _require_merge_rules_store(
        "Merge rules require ALLURE_KB_POSTGRES_DSN to be set",
    )

    from alla.knowledge.merge_rules_models import MergeRulesRequest, MergeRulesResponse

    merge_request = _parse_request(MergeRulesRequest, request)
    rules, created_count, updated_count = _run_kb_action(
        lambda: store.save_rules(
            project_id=merge_request.project_id,
            pairs=merge_request.pairs,
            launch_id=merge_request.launch_id,
        )
    )

    return MergeRulesResponse(
        rules=rules,
        created_count=created_count,
        updated_count=updated_count,
    ).model_dump()


@app.get("/api/v1/merge-rules")
def list_merge_rules(project_id: int) -> dict[str, Any]:
    """Вернуть все merge rules для указанного проекта."""
    store = _require_merge_rules_store(
        "Merge rules require ALLURE_KB_POSTGRES_DSN to be set",
    )

    from alla.knowledge.merge_rules_models import MergeRulesListResponse

    rules = _run_kb_action(lambda: store.load_rules(project_id))
    return MergeRulesListResponse(rules=rules).model_dump()


@app.delete("/api/v1/merge-rules/{rule_id}")
def delete_merge_rule(rule_id: int) -> dict[str, Any]:
    """Удалить merge rule по rule_id."""
    store = _require_merge_rules_store(
        "Merge rules require ALLURE_KB_POSTGRES_DSN to be set",
    )

    from alla.knowledge.merge_rules_models import MergeRuleDeleteResponse

    deleted = _run_kb_action(lambda: store.delete_rule(rule_id))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Merge rule {rule_id} not found")

    return MergeRuleDeleteResponse(rule_id=rule_id, deleted=True).model_dump()


@app.post("/api/v1/kb/feedback")
def submit_feedback(request: dict[str, Any]) -> dict[str, Any]:
    """Записать like/dislike для KB-совпадения из HTML-отчёта.

    Привязка выполняется по stable issue signature, а audit_text хранится
    только для последующего разбора человеком.
    """
    store = _require_feedback_store(
        "Feedback requires ALLURE_KB_POSTGRES_DSN to be set",
    )

    from alla.knowledge.feedback_models import FeedbackRequest

    fb_request = _parse_request(FeedbackRequest, request)
    response = _run_kb_action(lambda: store.record_vote(fb_request))

    return response.model_dump()


@app.post("/api/v1/kb/entries", status_code=201)
def create_kb_entry(response: Response, request: dict[str, Any]) -> dict[str, Any]:
    """Создать новую запись KB из HTML-отчёта."""
    store = _require_feedback_store("KB entry creation requires postgres backend")

    from alla.knowledge.feedback_models import CreateKBEntryRequest, CreateKBEntryResponse
    from alla.knowledge.models import KBEntry

    req = _parse_request(CreateKBEntryRequest, request)

    req.error_example = canonicalize_kb_error_example(req.error_example or "")
    req.step_path = req.step_path.strip() if req.step_path else None
    if req.step_path == "":
        req.step_path = None

    # Авто-генерация title и id если не указаны
    title = req.title or ""
    if not title:
        first_line = (req.error_example or "").splitlines()[0][:100] if req.error_example else ""
        title = first_line or "KB Entry"
    slug = req.id or make_kb_slug(title, req.error_example or "", req.step_path)

    entry = KBEntry(
        id=slug,
        title=title,
        description=req.description,
        error_example=req.error_example,
        step_path=req.step_path,
        category=req.category,
        resolution_steps=req.resolution_steps,
    )

    entry_id = _run_kb_action(lambda: store.create_kb_entry(entry, req.project_id))

    if entry_id is None:
        # ON CONFLICT DO NOTHING — slug+project уже существует. Это нормальный
        # сценарий retry после потери ответа (Failed to fetch на стороне браузера).
        # Если payload совпадает с существующей записью — возвращаем её id как
        # идемпотентный успех. Если payload отличается — это настоящий конфликт.
        existing = _run_kb_action(
            lambda: store.find_kb_entry_by_slug(slug, req.project_id)
        )
        if existing is not None and _kb_entry_matches(existing, entry):
            response.status_code = 200
            return CreateKBEntryResponse(
                entry_id=existing.entry_id or 0,
                id=existing.id,
                title=existing.title,
                category=existing.category,
                created=False,
            ).model_dump()
        raise HTTPException(
            status_code=409,
            detail=f"KB entry with slug '{slug}' already exists "
            f"for project_id={req.project_id}",
        )

    resp = CreateKBEntryResponse(
        entry_id=entry_id,
        id=slug,
        title=title,
        category=req.category,
        created=True,
    )
    return resp.model_dump()


def _kb_entry_matches(existing: "KBEntry", incoming: "KBEntry") -> bool:
    """Совпадают ли пользовательские поля двух KB-записей.

    Сравниваем то, что юзер мог изменить через форму. entry_id/project_id/
    created_at не входят: они либо суррогатные, либо контекстные.
    """
    return (
        existing.title == incoming.title
        and existing.description == incoming.description
        and existing.error_example == incoming.error_example
        and existing.step_path == incoming.step_path
        and existing.category == incoming.category
        and list(existing.resolution_steps) == list(incoming.resolution_steps)
    )


@app.put("/api/v1/kb/entries/{entry_id}")
def update_kb_entry(entry_id: int, request: dict[str, Any]) -> dict[str, Any]:
    """Обновить существующую запись базы знаний по entry_id."""
    store = _require_feedback_store("KB entry update requires postgres backend")

    allowed = {
        "title",
        "description",
        "error_example",
        "step_path",
        "category",
        "resolution_steps",
    }
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
    if "step_path" in fields:
        raw_step_path = fields["step_path"]
        if raw_step_path is None:
            fields["step_path"] = None
        else:
            fields["step_path"] = str(raw_step_path).strip() or None

    updated = _run_kb_action(lambda: store.update_kb_entry(entry_id, fields))

    if not updated:
        raise HTTPException(status_code=404, detail=f"Entry {entry_id} not found")

    return {"entry_id": entry_id, "updated": True}


@app.delete("/api/v1/kb/entries/{entry_id}", response_model=None)
def delete_kb_entry(entry_id: int, force: bool = False) -> dict[str, Any] | JSONResponse:
    """Удалить KB-запись, защищая записи с feedback от случайного удаления."""
    store = _require_feedback_store("KB entry deletion requires postgres backend")

    from alla.knowledge.feedback_models import KBEntryDeleteResponse

    feedback_count = _run_kb_action(lambda: store.count_feedback_for_entry(entry_id))
    if feedback_count > 0 and not force:
        detail = (
            f"Cannot delete: kb_entry has {feedback_count} feedback votes. "
            "Pass force=true to cascade."
        )
        return JSONResponse(
            status_code=409,
            content={"detail": detail, "feedback_count": feedback_count},
            headers={"X-Feedback-Count": str(feedback_count)},
        )

    deleted = _run_kb_action(lambda: store.delete_kb_entry(entry_id))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Entry {entry_id} not found")

    return KBEntryDeleteResponse(entry_id=entry_id, deleted=True).model_dump()


@app.get("/api/v1/kb/entries")
def list_kb_entries(project_id: int | None = None) -> dict[str, Any]:
    """Вернуть KB-записи, видимые глобально или для указанного проекта."""
    store = _require_feedback_store("KB entry listing requires postgres backend")

    entries = _run_kb_action(lambda: store.list_kb_entries(project_id))
    payload: list[dict[str, Any]] = []
    for entry in entries:
        item = entry.model_dump(mode="json")
        item["error_example_chars"] = len(entry.error_example or "")
        item["resolution_steps_count"] = len(entry.resolution_steps)
        payload.append(item)

    return {"count": len(payload), "entries": payload}


@app.post("/api/v1/kb/feedback/resolve")
def resolve_feedback(request: dict[str, Any]) -> dict[str, Any]:
    """Найти актуальные голоса для пар (entry_id, issue_signature_hash).

    Используется HTML-отчётом при загрузке для инициализации кнопок.
    Учитывает только новые exact-memory записи; legacy feedback без
    issue_signature_hash здесь не участвует.

    Body: {"items": [{"kb_entry_id": 123, "issue_signature_hash": "..."}]}
    Response: {"votes": {"123": {"vote": "like"}}}
    """
    store = _require_feedback_store("Requires postgres backend")

    from alla.knowledge.feedback_models import FeedbackResolveRequest

    req = _parse_request(FeedbackResolveRequest, request)

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


# --- Дашборд ---


def _resolve_dashboard_window(days: int, date_param: str | None) -> Any:
    """Построить DateWindow из query-параметров `days` / `date`."""
    from datetime import date as date_cls

    from alla.dashboard.stats_store import DateWindow

    if date_param:
        try:
            d = date_cls.fromisoformat(date_param)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Параметр date должен быть в формате YYYY-MM-DD",
            )
        return DateWindow.from_day(d)
    return DateWindow.from_days(max(1, min(int(days), 365)))


@app.get("/api/v1/dashboard/stats")
async def dashboard_stats(
    days: int = 30,
    date: str | None = None,
) -> dict[str, Any]:
    """Агрегированная статистика использования alla из PostgreSQL.

    Окно времени задаётся либо параметром ``days`` (последние N дней,
    clamp [1, 365]), либо ``date=YYYY-MM-DD`` (один календарный день UTC).
    Параметр ``date`` имеет приоритет над ``days``.
    """
    from datetime import datetime, timezone

    window = _resolve_dashboard_window(days, date)
    store = _get_dashboard_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Дашборд требует ALLURE_KB_POSTGRES_DSN",
        )
    kpis, per_project_raw, series = await asyncio.to_thread(
        _read_dashboard_stats,
        store,
        window=window,
    )
    names = await _get_project_names_cached()

    per_project: list[dict[str, Any]] = []
    for row in per_project_raw:
        pid = row["project_id"]
        if pid is None:
            project_name = "Без привязки к проекту"
        else:
            project_name = names.get(pid) or f"Project #{pid}"
        per_project.append({**row, "project_name": project_name})

    return {
        "window": window.descriptor(),
        "days": window.days_value if window.kind == "days" else None,
        "date": window.day_value.isoformat() if window.kind == "day" and window.day_value else None,
        "kpis": kpis,
        "per_project": per_project,
        "series": series,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/dashboard/projects/{project_id}/reports")
async def dashboard_project_reports(
    project_id: int,
    days: int = 30,
    date: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Список отчётов проекта в окне дашборда.

    Спец-значение ``project_id=0`` соответствует «Без привязки к проекту»
    (``project_id IS NULL`` в БД).
    """
    window = _resolve_dashboard_window(days, date)
    store = _get_dashboard_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Дашборд требует ALLURE_KB_POSTGRES_DSN",
        )
    pid: int | None = None if project_id == 0 else int(project_id)
    limit = max(1, min(int(limit), 1000))
    reports = await asyncio.to_thread(
        _read_project_reports,
        store,
        project_id=pid,
        window=window,
        limit=limit,
    )
    return {
        "project_id": pid,
        "window": window.descriptor(),
        "reports": reports,
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    """Самодостаточная HTML-страница дашборда использования."""
    from alla.dashboard.html_view import render_dashboard_html_shell

    return HTMLResponse(content=render_dashboard_html_shell(), headers=_build_csp_headers())


# --- Skill pipeline endpoints (DSN живёт только на сервере) ---
#
# Скилл-скрипты (`alla-skill/scripts/`) больше не коннектятся к PostgreSQL
# напрямую — TestOps-триаж они делают локально (токен пользователя), а всю
# работу с БД (skill_run, KB lookup, merge rules, отчёты) делегируют сюда.
# DSN задаётся только в окружении сервера.


def _require_skill_dsn() -> str:
    """Вернуть DSN или поднять 501, если PostgreSQL не настроен на сервере."""
    settings = _state.settings
    if settings is None or not settings.kb_active:
        raise HTTPException(
            status_code=501,
            detail="Skill pipeline requires ALLURE_KB_POSTGRES_DSN on the server",
        )
    return cast(str, settings.kb_postgres_dsn)


def _load_skill_run(dsn: str, run_id: int) -> Any:
    """Загрузить skill_run или поднять 404."""
    from alla.services.skill_state_service import SkillStateError, load_run

    try:
        return load_run(dsn=dsn, run_id=run_id)
    except SkillStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/v1/skill/runs")
def skill_create_run(request: dict[str, Any]) -> dict[str, Any]:
    """Persist skill-run: merge rules → KB lookup → onboarding → create_run.

    Body: ``{"triage_report": {...}, "clustering_report": {...} | null}``
    (сериализованные модели, посчитанные локально из TestOps-триажа).
    Возвращает компактную сводку прогона с ``run_id``.
    """
    dsn = _require_skill_dsn()
    settings = _settings()

    from alla.models.clustering import ClusteringReport
    from alla.models.testops import TriageReport
    from alla.orchestrator import apply_merge_rules_phase, build_onboarding_state
    from alla.services.kb_lookup_service import KBStageResult, lookup_kb_for_clusters
    from alla.services.skill_api_service import build_run_summary
    from alla.services.skill_state_service import SkillStateError, create_run

    triage_payload = request.get("triage_report")
    if not isinstance(triage_payload, dict):
        raise HTTPException(status_code=422, detail="triage_report is required")
    try:
        report = TriageReport.model_validate(triage_payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid triage_report: {exc}")

    clustering_payload = request.get("clustering_report")
    clustering_report = None
    if clustering_payload is not None:
        try:
            clustering_report = ClusteringReport.model_validate(clustering_payload)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid clustering_report: {exc}"
            )

    clustering_report = apply_merge_rules_phase(report, clustering_report, settings)
    try:
        kb_stage = lookup_kb_for_clusters(report, clustering_report, settings)
    except Exception as exc:
        logger.warning("Skill run: KB lookup failed: %s", exc)
        kb_stage = KBStageResult()

    onboarding = build_onboarding_state(
        settings,
        report.project_id,
        clustering_report,
        kb_entries=kb_stage.kb_entries,
    )

    try:
        run_id = create_run(
            dsn=dsn,
            triage_report=report,
            clustering_report=clustering_report,
            kb_stage=kb_stage,
            onboarding=onboarding,
        )
    except SkillStateError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return build_run_summary(run_id, report, clustering_report, kb_stage.kb_results)


@app.get("/api/v1/skill/runs/{run_id}")
def skill_get_run(run_id: int) -> dict[str, Any]:
    """Вернуть полную сериализацию skill_run для локального восстановления."""
    dsn = _require_skill_dsn()

    from alla.services.skill_api_service import serialize_run

    return serialize_run(_load_skill_run(dsn, run_id))


@app.get("/api/v1/skill/runs/{run_id}/clusters/{cluster_id}/context")
def skill_cluster_context(
    run_id: int,
    cluster_id: str,
    max_log_chars: int = 8000,
    max_message_chars: int = 2000,
    max_trace_chars: int = 400,
) -> dict[str, Any]:
    """Контекст + готовый промпт для анализа одного кластера."""
    dsn = _require_skill_dsn()

    from alla.services.skill_api_service import build_cluster_context

    skill_run = _load_skill_run(dsn, run_id)
    ctx = build_cluster_context(
        skill_run,
        cluster_id,
        max_message_chars=max_message_chars,
        max_trace_chars=max_trace_chars,
        max_log_chars=max_log_chars,
    )
    if ctx is None:
        raise HTTPException(
            status_code=404,
            detail=f"cluster_id={cluster_id!r} not found in run {run_id}",
        )
    return ctx


@app.post("/api/v1/skill/runs/{run_id}/summary-context")
def skill_summary_context(
    run_id: int,
    request: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    """Промпт + контекст для итогового launch summary.

    Опц. body ``{"clusters": {cluster_id: {"analysis_text": ...}}}`` —
    промежуточные анализы до submit. Иначе берётся сохранённый анализ.
    """
    dsn = _require_skill_dsn()

    from alla.services.skill_api_service import build_summary_context

    intermediate = request.get("clusters") if isinstance(request, dict) else None
    if intermediate is not None and not isinstance(intermediate, dict):
        raise HTTPException(
            status_code=422,
            detail="'clusters' must be an object {cluster_id: {analysis_text: ...}}",
        )

    skill_run = _load_skill_run(dsn, run_id)
    ctx = build_summary_context(skill_run, intermediate)
    if ctx is None:
        raise HTTPException(
            status_code=404,
            detail=f"run {run_id} has no ClusteringReport",
        )
    return ctx


@app.post("/api/v1/skill/runs/{run_id}/analysis")
def skill_submit_analysis(run_id: int, request: dict[str, Any]) -> dict[str, Any]:
    """Принять агентский анализ кластеров и сохранить в skill_run."""
    dsn = _require_skill_dsn()

    from alla.services.agent_analysis_adapter import (
        AgentAnalysisError,
        validate_agent_payload,
    )
    from alla.services.skill_state_service import SkillStateError, save_agent_analysis

    skill_run = _load_skill_run(dsn, run_id)
    expected_ids: list[str] = []
    if skill_run.clustering_report is not None:
        expected_ids = [c.cluster_id for c in skill_run.clustering_report.clusters]

    try:
        missing, extra = validate_agent_payload(
            request, expected_cluster_ids=expected_ids
        )
    except AgentAnalysisError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid agent payload: {exc}")

    summary_text = ""
    if isinstance(request, dict):
        summary_text = (request.get("launch_summary") or {}).get("summary_text") or ""

    try:
        save_agent_analysis(
            dsn=dsn,
            run_id=run_id,
            agent_analysis=request,
            agent_summary_text=summary_text,
        )
    except SkillStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return {
        "ok": True,
        "run_id": run_id,
        "clusters_received": len(request.get("clusters", {})),
        "clusters_expected": len(expected_ids),
        "missing_cluster_ids": missing,
        "extra_cluster_ids": extra,
    }


@app.post("/api/v1/skill/runs/{run_id}/report")
def skill_generate_report(run_id: int) -> dict[str, Any]:
    """Сгенерировать HTML-отчёт, сохранить в alla.report и вернуть HTML.

    FS-сохранение (reports_dir / --out) остаётся на стороне клиента —
    это путь на машине пользователя, поэтому HTML возвращается в ответе.
    """
    dsn = _require_skill_dsn()
    settings = _settings()

    import datetime as dt

    from alla.app_support import calculate_llm_token_usage
    from alla.services.skill_api_service import (
        build_analysis_result,
        interactive_disabled_reasons,
    )
    from alla.services.skill_state_service import SkillStateError, record_error, save_report

    skill_run = _load_skill_run(dsn, run_id)

    try:
        result = build_analysis_result(skill_run, settings)
        html = build_html_report_content(result, settings=settings)
    except Exception as exc:
        try:
            record_error(
                dsn=dsn,
                run_id=run_id,
                error={"step": "generate_html", "message": str(exc)},
            )
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to generate HTML: {exc}")

    timestamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = f"alla_launch_{skill_run.launch_id}_run_{skill_run.run_id}_{timestamp}.html"

    # Сохранение в БД — best-effort: ошибка инициализации стора (DDL/права)
    # или самого save не должна валить endpoint, ведь клиент может писать
    # HTML на свой диск (--out). saved_to_db честно отражает реальный исход.
    saved_to_db = False
    try:
        store = _get_report_content_store()
        if store is not None:
            store.save(
                filename,
                skill_run.launch_id,
                html,
                skill_run.project_id,
                token_usage=calculate_llm_token_usage(result),
            )
            saved_to_db = True
    except Exception as exc:
        logger.warning("Skill report: не удалось сохранить в БД: %s", exc)

    # `/reports/<file>` ссылку отдаём только если отчёт реально лёг в БД
    # (иначе сервер вернул бы по ней 404). Без серверной копии — статический
    # ALLURE_REPORT_URL (обычно пусто), а HTML всё равно возвращается клиенту.
    if saved_to_db:
        report_url = resolve_report_url(settings, report_filename=filename)
    else:
        report_url = resolve_report_url(settings)

    try:
        save_report(
            dsn=dsn,
            run_id=run_id,
            report_filename=filename,
            report_url=report_url or None,
        )
    except SkillStateError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "ok": True,
        "run_id": run_id,
        "report_filename": filename,
        "report_url": report_url,
        "saved_to_db": saved_to_db,
        "interactive_disabled_reasons": interactive_disabled_reasons(settings),
        "html": html,
        "html_size_bytes": len(html.encode("utf-8")),
    }


@app.post("/api/v1/skill/runs/{run_id}/push-result")
def skill_save_push_result(run_id: int, request: dict[str, Any]) -> dict[str, Any]:
    """Зафиксировать результат push'а в TestOps (push делает клиент локально)."""
    dsn = _require_skill_dsn()

    from alla.services.skill_state_service import SkillStateError, save_push_result

    try:
        save_push_result(dsn=dsn, run_id=run_id, push_result=request)
    except SkillStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return {"ok": True, "run_id": run_id}


def main() -> None:
    """Точка входа консольного скрипта alla-server."""
    import sys

    try:
        settings = load_settings()
    except Exception as exc:
        print(format_configuration_error(exc), file=sys.stderr)
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
