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


class HealthResponse(BaseModel):
    """JSON-ответ GET /health."""

    status: str
    version: str


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

    Запускает полный pipeline: триаж → кластеризация → KB-поиск → KB-push.
    Возвращает объединённый JSON-результат.
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

    return response


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
