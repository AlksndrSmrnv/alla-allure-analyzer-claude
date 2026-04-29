"""Тесты FastAPI-сервера: эндпоинты, маппинг ошибок, сериализация ответов."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

import alla
from alla.knowledge.feedback_models import FeedbackResponse, FeedbackVote
from alla.knowledge.merge_rules_models import MergeRule
from alla.exceptions import (
    AllureApiError,
    AuthenticationError,
    ConfigurationError,
    KnowledgeBaseError,
    PaginationLimitError,
)
from alla.models.testops import (
    CommentResponse,
    TestResultResponse as ResultResponse,
    TriageReport,
)
from alla.orchestrator import AnalysisResult
from alla.server import _McpNoSlashRedirectMiddleware, _make_slug, app


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
        test_results: list[ResultResponse] | None = None,
        comments_by_tc: dict[int, list[CommentResponse]] | None = None,
        raise_on_get_all: Exception | None = None,
    ) -> None:
        self._test_results = test_results or []
        self._comments_by_tc = comments_by_tc or {}
        self._raise_on_get_all = raise_on_get_all
        self.delete_calls: list[int] = []

    async def get_all_test_results_for_launch(self, launch_id: int) -> list[ResultResponse]:
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

    async def get_all_test_results_for_launch(self, launch_id: int) -> list[ResultResponse]:
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


def test_mcp_mount_exposes_transport_at_documented_path() -> None:
    """Mounted MCP transport is available at /mcp, not /mcp/mcp."""
    mount = next(route for route in app.routes if getattr(route, "path", None) == "/mcp")
    inner_paths = {getattr(route, "path", None) for route in mount.app.routes}

    assert "/" in inner_paths
    assert "/mcp" not in inner_paths


@pytest.mark.asyncio
async def test_mcp_exact_path_is_rewritten_without_redirect() -> None:
    """POST /mcp reaches the mounted app as /mcp/ without a client-visible 307."""
    seen_scope: dict[str, Any] = {}

    async def downstream(scope, receive, send) -> None:
        seen_scope.update(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = _McpNoSlashRedirectMiddleware(downstream)
    transport = httpx.ASGITransport(app=middleware)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/mcp")

    assert resp.status_code == 204
    assert seen_scope["path"] == "/mcp/"
    assert seen_scope["raw_path"] == b"/mcp/"


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
    assert data["onboarding"]["mode"] == "normal"


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
            ResultResponse(id=1, status="failed", test_case_id=100),
            ResultResponse(id=2, status="broken", test_case_id=200),
            ResultResponse(id=3, status="passed"),
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
            ResultResponse(id=1, status="failed", test_case_id=100),
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
            ResultResponse(id=1, status="failed", test_case_id=100),
            ResultResponse(id=2, status="failed", test_case_id=None),
        ],
        comments_by_tc={100: []},
    )
    _setup_state(client=mock_client)

    async with _http_client as client:
        resp = await client.delete("/api/v1/comments/123")

    assert resp.status_code == 200
    data = resp.json()
    assert data["skipped_test_cases"] == 1


# ---------------------------------------------------------------------------
# POST /api/v1/kb/entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_kb_entry_canonicalizes_error_example_before_save(monkeypatch, _http_client) -> None:
    """Сервер сохраняет normalized error_example и генерирует slug по нему."""
    captured: dict[str, Any] = {}

    class _Store:
        def create_kb_entry(self, entry, project_id):
            captured["entry"] = entry
            captured["project_id"] = project_id
            return 77

    monkeypatch.setattr("alla.server._get_feedback_store", lambda: _Store())

    payload = {
        "title": "Gateway timeout",
        "description": "desc",
        "error_example": (
            "Order 123e4567-e89b-12d3-a456-426614174000 failed at 2026-02-10 12:00:00\n"
            "--- Лог приложения ---\n"
            "--- [файл: app.log] ---\n"
            "2026-02-10 12:00:00 [ERROR] requestId=123e4567e89b12d3a456426614174000 "
            "from 10.20.30.40 build 123456"
        ),
        "category": "service",
        "resolution_steps": ["step 1"],
        "project_id": 42,
    }

    async with _http_client as client:
        resp = await client.post("/api/v1/kb/entries", json=payload)

    assert resp.status_code == 201
    data = resp.json()
    entry = captured["entry"]
    assert captured["project_id"] == 42
    assert entry.error_example == (
        "Order <ID> failed at <TS>\n"
        "--- [файл: app.log] ---\n"
        "<TS> [ERROR] requestId=<ID> from <IP> build <NUM>"
    )
    assert "--- Лог приложения ---" not in entry.error_example
    assert "123e4567-e89b-12d3-a456-426614174000" not in entry.error_example
    assert "2026-02-10 12:00:00" not in entry.error_example
    assert data["id"] == _make_slug("Gateway timeout", entry.error_example)
    assert entry.id == data["id"]


@pytest.mark.asyncio
async def test_submit_feedback_uses_exact_signature_payload(monkeypatch, _http_client) -> None:
    """POST /api/v1/kb/feedback передаёт stable issue signature в store."""
    captured: dict[str, Any] = {}

    class _Store:
        def record_vote(self, request):
            captured["request"] = request
            return FeedbackResponse(
                kb_entry_id=request.kb_entry_id,
                audit_text_preview=request.audit_text[:80],
                vote=request.vote,
                created=True,
                feedback_id=91,
            )

    monkeypatch.setattr("alla.server._get_feedback_store", lambda: _Store())

    payload = {
        "kb_entry_id": 77,
        "audit_text": "[message]\nGateway timeout while saving order",
        "issue_signature_hash": "a" * 64,
        "issue_signature_version": 5,
        "issue_signature_payload": {
            "signature_hash": "a" * 64,
            "version": 5,
            "basis": "message_exact",
        },
        "vote": "like",
        "cluster_id": "c1",
        "launch_id": 123,
    }

    async with _http_client as client:
        resp = await client.post("/api/v1/kb/feedback", json=payload)

    assert resp.status_code == 200
    data = resp.json()
    req = captured["request"]
    assert req.issue_signature_hash == "a" * 64
    assert req.issue_signature_version == 5
    assert req.audit_text == payload["audit_text"]
    assert data["feedback_id"] == 91
    assert data["audit_text_preview"] == payload["audit_text"][:80]


@pytest.mark.asyncio
async def test_resolve_feedback_uses_exact_signature_hash(monkeypatch, _http_client) -> None:
    """Resolve endpoint работает только по issue_signature_hash/version."""
    captured: dict[str, Any] = {}

    class _Store:
        def resolve_votes(self, items):
            captured["items"] = items
            return {"77:c1": (FeedbackVote.LIKE, 44)}

    monkeypatch.setattr("alla.server._get_feedback_store", lambda: _Store())

    payload = {
        "items": [
            {
                "kb_entry_id": 77,
                "issue_signature_hash": "b" * 64,
                "issue_signature_version": 5,
                "cluster_id": "c1",
            }
        ]
    }

    async with _http_client as client:
        resp = await client.post("/api/v1/kb/feedback/resolve", json=payload)

    assert resp.status_code == 200
    assert captured["items"] == [(77, "b" * 64, 5, "77:c1")]
    assert resp.json() == {
        "votes": {
            "77:c1": {
                "vote": "like",
                "feedback_id": 44,
            }
        }
    }


@pytest.mark.asyncio
async def test_create_merge_rules_returns_upsert_counts(monkeypatch, _http_client) -> None:
    """POST /api/v1/merge-rules сохраняет пары и возвращает create/update counts."""
    captured: dict[str, Any] = {}

    class _Store:
        def save_rules(self, project_id, pairs, launch_id=None):
            captured["project_id"] = project_id
            captured["pairs"] = pairs
            captured["launch_id"] = launch_id
            return (
                [
                    MergeRule(
                        rule_id=15,
                        project_id=project_id,
                        signature_hash_a="a" * 64,
                        signature_hash_b="b" * 64,
                        audit_text_a="[message]\nA",
                        audit_text_b="[message]\nB",
                        launch_id=launch_id,
                    )
                ],
                1,
                0,
            )

    monkeypatch.setattr("alla.server._get_merge_rules_store", lambda: _Store())

    payload = {
        "project_id": 42,
        "launch_id": 123,
        "pairs": [
            {
                "signature_hash_a": "a" * 64,
                "signature_hash_b": "b" * 64,
                "audit_text_a": "[message]\nA",
                "audit_text_b": "[message]\nB",
            }
        ],
    }

    async with _http_client as client:
        resp = await client.post("/api/v1/merge-rules", json=payload)

    assert resp.status_code == 200
    assert captured["project_id"] == 42
    assert captured["launch_id"] == 123
    assert len(captured["pairs"]) == 1
    assert resp.json()["created_count"] == 1
    assert resp.json()["updated_count"] == 0
    assert resp.json()["rules"][0]["rule_id"] == 15


@pytest.mark.asyncio
async def test_list_merge_rules_returns_rules_for_project(monkeypatch, _http_client) -> None:
    """GET /api/v1/merge-rules?project_id=N отдаёт список правил проекта."""
    captured: dict[str, Any] = {}

    class _Store:
        def load_rules(self, project_id):
            captured["project_id"] = project_id
            return [
                MergeRule(
                    rule_id=7,
                    project_id=project_id,
                    signature_hash_a="c" * 64,
                    signature_hash_b="d" * 64,
                )
            ]

    monkeypatch.setattr("alla.server._get_merge_rules_store", lambda: _Store())

    async with _http_client as client:
        resp = await client.get("/api/v1/merge-rules", params={"project_id": 77})

    assert resp.status_code == 200
    assert captured["project_id"] == 77
    assert resp.json() == {
        "rules": [
            {
                "rule_id": 7,
                "project_id": 77,
                "signature_hash_a": "c" * 64,
                "signature_hash_b": "d" * 64,
                "audit_text_a": "",
                "audit_text_b": "",
                "launch_id": None,
                "created_at": None,
            }
        ]
    }


@pytest.mark.asyncio
async def test_delete_merge_rule_returns_404_when_missing(monkeypatch, _http_client) -> None:
    """DELETE /api/v1/merge-rules/{rule_id} → 404, если правило не найдено."""
    class _Store:
        def delete_rule(self, rule_id):
            assert rule_id == 99
            return False

    monkeypatch.setattr("alla.server._get_merge_rules_store", lambda: _Store())

    async with _http_client as client:
        resp = await client.delete("/api/v1/merge-rules/99")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Merge rule 99 not found"


# ---------------------------------------------------------------------------
# build_analysis_response — token_usage
# ---------------------------------------------------------------------------


def test_build_analysis_response_includes_token_usage() -> None:
    """token_usage присутствует в JSON когда llm_result задан."""
    from alla.app_support import build_analysis_response
    from alla.models.llm import LLMAnalysisResult, LLMLaunchSummary, TokenUsage

    result = _make_analysis_result(
        llm_result=LLMAnalysisResult(
            total_clusters=1,
            analyzed_count=1,
            failed_count=0,
            skipped_count=0,
            token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        ),
        llm_launch_summary=LLMLaunchSummary(
            summary_text="summary",
            token_usage=TokenUsage(prompt_tokens=200, completion_tokens=80, total_tokens=280),
        ),
    )

    payload = build_analysis_response(result)

    assert payload["llm_result"]["token_usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    }
    assert payload["llm_launch_summary"]["token_usage"] == {
        "prompt_tokens": 200,
        "completion_tokens": 80,
        "total_tokens": 280,
    }
