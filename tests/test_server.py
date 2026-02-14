"""Тесты FastAPI-сервера: эндпоинты, маппинг ошибок, сериализация ответов."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import alla
from alla.exceptions import (
    AllureApiError,
    AuthenticationError,
    ConfigurationError,
    KnowledgeBaseError,
    PaginationLimitError,
)
from alla.models.common import TestStatus
from alla.models.testops import (
    CommentResponse,
    FailedTestSummary,
    TestResultResponse,
    TriageReport,
)
from alla.orchestrator import AnalysisResult
from alla.server import _AppState, app
from alla.services.comment_delete_service import DeleteCommentsResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_triage_report() -> TriageReport:
    return TriageReport(
        launch_id=123,
        launch_name="Test Run",
        total_results=10,
        passed_count=8,
        failed_count=2,
    )


def _make_analysis_result(**overrides) -> AnalysisResult:
    defaults = {
        "triage_report": _make_triage_report(),
        "clustering_report": None,
        "kb_results": {},
        "kb_push_result": None,
        "llm_result": None,
        "llm_push_result": None,
    }
    defaults.update(overrides)
    return AnalysisResult(**defaults)


class _MockClient:
    """Минимальный клиент, реализующий нужные протоколы."""

    def __init__(
        self,
        *,
        test_results: list[TestResultResponse] | None = None,
        comments_by_tc: dict[int, list[CommentResponse]] | None = None,
        raise_on_get_all: Exception | None = None,
    ) -> None:
        self._test_results = test_results or []
        self._comments_by_tc = comments_by_tc or {}
        self._raise_on_get_all = raise_on_get_all
        self.delete_calls: list[int] = []

    async def get_all_test_results_for_launch(self, launch_id: int) -> list[TestResultResponse]:
        if self._raise_on_get_all:
            raise self._raise_on_get_all
        return self._test_results

    async def get_comments(self, test_case_id: int) -> list[CommentResponse]:
        return self._comments_by_tc.get(test_case_id, [])

    async def delete_comment(self, comment_id: int) -> None:
        self.delete_calls.append(comment_id)

    async def post_comment(self, test_case_id: int, body: str) -> None:
        pass


class _NonCommentClient:
    """Клиент, не реализующий CommentManager."""

    async def get_all_test_results_for_launch(self, launch_id: int) -> list[TestResultResponse]:
        return []


def _setup_state(client: Any = None, settings: Any = None) -> None:
    """Установить _state сервера напрямую."""
    from alla.server import _state

    _state.client = client or _MockClient()
    _state.settings = settings or _DummySettings()
    _state.auth = None


@dataclass
class _DummySettings:
    """Минимальный Settings-заглушка для сервера."""
    detail_concurrency: int = 5


@pytest.fixture
def _http_client():
    """httpx.AsyncClient для тестирования FastAPI через ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_returns_ok(_http_client) -> None:
    """GET /health → 200, status=ok."""
    async with _http_client as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == alla.__version__


# ---------------------------------------------------------------------------
# POST /api/v1/analyze/{launch_id} — успех
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_success(monkeypatch, _http_client) -> None:
    """POST /api/v1/analyze/123 → 200, triage_report в ответе."""
    _setup_state()

    async def mock_analyze(launch_id, client, settings, *, updater=None):
        return _make_analysis_result()

    monkeypatch.setattr("alla.orchestrator.analyze_launch", mock_analyze)

    async with _http_client as client:
        resp = await client.post("/api/v1/analyze/123")

    assert resp.status_code == 200
    data = resp.json()
    assert data["triage_report"]["launch_id"] == 123
    assert data["triage_report"]["total_results"] == 10


@pytest.mark.asyncio
async def test_analyze_includes_clustering(monkeypatch, _http_client) -> None:
    """clustering_report в ответе, если он есть в результате."""
    _setup_state()

    from alla.models.clustering import ClusteringReport

    clustering = ClusteringReport(
        launch_id=123, total_failures=5, cluster_count=2, clusters=[],
    )

    async def mock_analyze(launch_id, client, settings, *, updater=None):
        return _make_analysis_result(clustering_report=clustering)

    monkeypatch.setattr("alla.orchestrator.analyze_launch", mock_analyze)

    async with _http_client as client:
        resp = await client.post("/api/v1/analyze/123")

    assert resp.status_code == 200
    data = resp.json()
    assert data["clustering_report"]["total_failures"] == 5
    assert data["clustering_report"]["cluster_count"] == 2


# ---------------------------------------------------------------------------
# POST /api/v1/analyze/{launch_id} — error mappings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [
        (AuthenticationError("bad token"), 401),
        (AllureApiError(404, "not found", "/api/launch/999"), 404),
        (AllureApiError(500, "internal error", "/api/testresult"), 502),
        (ConfigurationError("missing var"), 400),
        (KnowledgeBaseError("bad yaml"), 500),
        (PaginationLimitError("too many pages"), 502),
    ],
)
async def test_analyze_error_mapping(
    monkeypatch, _http_client, exception, expected_status,
) -> None:
    """Ошибки из orchestrator маппятся в правильные HTTP-коды."""
    _setup_state()

    async def mock_analyze(launch_id, client, settings, *, updater=None):
        raise exception

    monkeypatch.setattr("alla.orchestrator.analyze_launch", mock_analyze)

    async with _http_client as client:
        resp = await client.post("/api/v1/analyze/123")

    assert resp.status_code == expected_status
    assert "detail" in resp.json()


# ---------------------------------------------------------------------------
# DELETE /api/v1/comments/{launch_id} — успех
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_comments_success(monkeypatch, _http_client) -> None:
    """DELETE /api/v1/comments/123 → 200, корректные counts."""
    mock_client = _MockClient(
        test_results=[
            TestResultResponse(id=1, status="failed", test_case_id=100),
            TestResultResponse(id=2, status="broken", test_case_id=200),
            TestResultResponse(id=3, status="passed"),
        ],
        comments_by_tc={
            100: [CommentResponse(id=10, body="[alla] text", test_case_id=100)],
            200: [],
        },
    )
    _setup_state(client=mock_client)

    async with _http_client as client:
        resp = await client.delete("/api/v1/comments/123")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_test_cases"] == 2
    assert data["comments_found"] == 1
    assert data["comments_deleted"] == 1
    assert data["dry_run"] is False


@pytest.mark.asyncio
async def test_delete_comments_dry_run(monkeypatch, _http_client) -> None:
    """?dry_run=true → комментарии найдены, но не удалены."""
    mock_client = _MockClient(
        test_results=[
            TestResultResponse(id=1, status="failed", test_case_id=100),
        ],
        comments_by_tc={
            100: [CommentResponse(id=10, body="[alla] text", test_case_id=100)],
        },
    )
    _setup_state(client=mock_client)

    async with _http_client as client:
        resp = await client.delete("/api/v1/comments/123?dry_run=true")

    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["comments_found"] == 1
    assert data["comments_deleted"] == 0


# ---------------------------------------------------------------------------
# DELETE /api/v1/comments/{launch_id} — ошибки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_comments_auth_error(_http_client) -> None:
    """AuthenticationError при получении результатов → 401."""
    mock_client = _MockClient(
        raise_on_get_all=AuthenticationError("bad token"),
    )
    _setup_state(client=mock_client)

    async with _http_client as client:
        resp = await client.delete("/api/v1/comments/123")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_comments_not_found(_http_client) -> None:
    """AllureApiError(404) → 404."""
    mock_client = _MockClient(
        raise_on_get_all=AllureApiError(404, "not found", "/api/testresult"),
    )
    _setup_state(client=mock_client)

    async with _http_client as client:
        resp = await client.delete("/api/v1/comments/123")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_comments_non_comment_manager_returns_500(_http_client) -> None:
    """Клиент не реализует CommentManager → 500."""
    _setup_state(client=_NonCommentClient())

    async with _http_client as client:
        resp = await client.delete("/api/v1/comments/123")

    assert resp.status_code == 500
    assert "Клиент не поддерживает" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delete_comments_skips_tests_without_tc_id(_http_client) -> None:
    """Тесты без test_case_id → skipped_test_cases > 0."""
    mock_client = _MockClient(
        test_results=[
            TestResultResponse(id=1, status="failed", test_case_id=100),
            TestResultResponse(id=2, status="failed", test_case_id=None),
        ],
        comments_by_tc={100: []},
    )
    _setup_state(client=mock_client)

    async with _http_client as client:
        resp = await client.delete("/api/v1/comments/123")

    assert resp.status_code == 200
    data = resp.json()
    assert data["skipped_test_cases"] == 1
