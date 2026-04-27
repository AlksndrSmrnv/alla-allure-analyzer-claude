"""HTTP-сервер alla — REST API для анализа запусков Allure TestOps."""

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, TypeVar, cast

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

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
from alla.utils.step_paths import normalize_step_path
from alla.utils.text_normalization import canonicalize_kb_error_example

if TYPE_CHECKING:
    from alla.config import Settings
    from alla.knowledge.postgres_feedback import PostgresFeedbackStore
    from alla.knowledge.merge_rules_store import PostgresMergeRulesStore
    from alla.orchestrator import AnalysisResult

logger = logging.getLogger(__name__)
ModelT = TypeVar("ModelT", bound=BaseModel)
ResultT = TypeVar("ResultT")


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
        self.feedback_store: Any = None
        self.merge_rules_store: Any = None
        self.dashboard_store: Any = None
        self.project_names_cache: dict[int, str] = {}
        self.project_names_expires_at: float = 0.0


_state = _AppState()


# --- Lifespan ---


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
    _state.report_store = None
    _state.feedback_store = None
    _state.merge_rules_store = None

    if settings.reports_dir:
        from pathlib import Path

        Path(settings.reports_dir).mkdir(parents=True, exist_ok=True)
        logger.info("Директория отчётов: %s", Path(settings.reports_dir).resolve())

    if settings.reports_postgres and settings.kb_postgres_dsn:
        from alla.report.report_store import PostgresReportStore

        _state.report_store = PostgresReportStore(dsn=settings.kb_postgres_dsn)
        logger.info("Хранилище отчётов: PostgreSQL")

    # MCP session manager должен жить столько же, сколько FastAPI-приложение.
    # Starlette не пробрасывает lifespan смонтированных приложений, поэтому
    # стартуем менеджер вручную здесь.
    from alla.mcp_app import mcp as _mcp

    async with _mcp.session_manager.run():
        logger.info("MCP сервер смонтирован: %s/mcp", settings.server_external_url or "")
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


def _settings() -> "Settings":
    """Return initialized server settings."""
    return cast("Settings", _state.settings)


def _effective_settings(push_to_testops: bool | None = None) -> "Settings":
    """Return settings, optionally overriding push_to_testops per request."""
    settings = _settings()
    if push_to_testops is None:
        return settings
    return settings.model_copy(update={"push_to_testops": push_to_testops})


async def _run_analysis_or_raise(
    launch_id: int,
    *,
    push_to_testops: bool | None = None,
) -> "AnalysisResult":
    """Run orchestrator analysis and map domain errors to HTTP errors."""
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
            settings=_effective_settings(push_to_testops),
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
    push_to_testops: bool | None = None,
) -> dict[str, Any]:
    """Анализ запуска — эквивалент ``alla <launch_id> --output-format json``.

    Запускает полный pipeline: триаж → кластеризация → KB-поиск →
    LLM-анализ → LLM-push → KB-push (fallback). Возвращает объединённый JSON-результат.

    Query parameter ``push_to_testops`` переопределяет ``ALLURE_PUSH_TO_TESTOPS``.
    """
    result = await _run_analysis_or_raise(
        launch_id,
        push_to_testops=push_to_testops,
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
    push_to_testops: bool | None = None,
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
        push_to_testops=push_to_testops,
    )

    from datetime import datetime

    report_filename: str | None = None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    need_filename = _state.settings.reports_dir or _state.report_store
    if need_filename:
        report_filename = f"{launch_id}_{timestamp}.html"

    html = build_html_report_content(
        result,
        settings=_state.settings,
    )
    persist_generated_report(
        html_content=html,
        launch_id=launch_id,
        report_filename=report_filename,
        settings=_state.settings,
        report_store=_state.report_store,
        project_id=result.triage_report.project_id,
    )

    effective_report_url = resolve_report_url(
        _state.settings,
        report_url_override=report_url or None,
        report_filename=report_filename,
    )
    if not effective_report_url:
        logger.warning(
            "Ссылка на отчёт не будет прикреплена к запуску #%d: "
            "задайте ALLURE_SERVER_EXTERNAL_URL + ALLURE_REPORTS_DIR "
            "или ALLURE_REPORT_URL",
            launch_id,
        )

    await attach_report_link(
        _state.client,
        launch_id=launch_id,
        settings=_state.settings,
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


# --- KB Feedback endpoints ---


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
        names = await client.list_projects()
    except Exception as exc:  # noqa: BLE001 - сетевые/HTTP ошибки разные
        logger.warning("Не удалось получить список проектов из TestOps: %s", exc)
        return _state.project_names_cache or {}
    _state.project_names_cache = names
    _state.project_names_expires_at = now + _PROJECT_NAMES_TTL
    return names


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


def _make_slug(title: str, error_example: str, step_path: str | None = None) -> str:
    """Сгенерировать slug из заголовка + хэша error_example (+ step_path при наличии).

    Формат: <slugified_title>_<8 hex chars of sha256(signature_material)>
    """
    import hashlib
    import re

    base = re.sub(r"[^a-z0-9]+", "_", title.lower())
    base = base.strip("_")[:50] or "kb_entry"
    signature_material = error_example
    normalized_step_path = normalize_step_path(step_path)
    if normalized_step_path:
        signature_material = f"{error_example}\n---\n{normalized_step_path}"
    suffix = hashlib.sha256(signature_material.encode()).hexdigest()[:8]
    return f"{base}_{suffix}"


@app.post("/api/v1/kb/entries", status_code=201)
def create_kb_entry(request: dict[str, Any]) -> dict[str, Any]:
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
    if not req.title:
        first_line = (req.error_example or "").splitlines()[0][:100] if req.error_example else ""
        req.title = first_line or "KB Entry"
    if not req.id:
        req.id = _make_slug(req.title, req.error_example or "", req.step_path)

    entry = KBEntry(
        id=req.id,
        title=req.title,
        description=req.description,
        error_example=req.error_example,
        step_path=req.step_path,
        category=req.category,
        resolution_steps=req.resolution_steps,
    )

    entry_id = _run_kb_action(lambda: store.create_kb_entry(entry, req.project_id))

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


# --- Dashboard ---


@app.get("/api/v1/dashboard/stats")
async def dashboard_stats(days: int = 30) -> dict[str, Any]:
    """Агрегированная статистика использования alla из PostgreSQL.

    Окно времени (`days`) применяется ко всем метрикам: отчётам, KB-записям,
    лайкам/дизлайкам и merge-правилам.
    """
    from datetime import datetime, timezone

    days = max(1, min(int(days), 365))
    store = _get_dashboard_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Дашборд требует ALLURE_KB_POSTGRES_DSN",
        )
    kpis = store.totals(days=days)
    per_project_raw = store.per_project_rollup(days=days)
    series = store.reports_per_day(days=days)
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
        "days": days,
        "kpis": kpis,
        "per_project": per_project,
        "series": series,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    """Self-contained HTML-страница дашборда использования."""
    from alla.dashboard.html_view import render_dashboard_html_shell

    return HTMLResponse(content=render_dashboard_html_shell(), headers=_build_csp_headers())


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
