"""Тесты FastAPI-сервера: эндпоинты, маппинг ошибок, сериализация ответов."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

import alla
from alla.knowledge.feedback_models import FeedbackResponse, FeedbackVote
from alla.knowledge.slug import make_kb_slug
from alla.knowledge.merge_rules_models import MergeRule
from alla.exceptions import (
    AllureApiError,
    AuthenticationError,
    ConfigurationError,
    KnowledgeBaseError,
    PaginationLimitError,
)
from alla.models.llm import LLMAnalysisResult, LLMLaunchSummary, TokenUsage
from alla.models.testops import (
    CommentResponse,
    TestResultResponse as ResultResponse,
    TriageReport,
)
from alla.orchestrator import AnalysisResult
from alla.server import _McpNoSlashRedirectMiddleware, app


# ---------------------------------------------------------------------------
# Вспомогательные функции
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
        self.patch_launch_link_calls: list[tuple[int, str, str]] = []

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

    async def patch_launch_links(self, launch_id: int, name: str, url: str) -> None:
        self.patch_launch_link_calls.append((launch_id, name, url))


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
    _state.report_store = None
    _state.skill_report_store = None
    _state.report_view_store = None


@dataclass
class _DummySettings:
    """Минимальный Settings-заглушка для сервера."""
    detail_concurrency: int = 5
    push_comments: bool = False
    push_report_link: bool = True
    reports_dir: str = ""
    report_url: str = ""
    server_external_url: str = ""
    report_link_name: str = "Alla report"
    endpoint: str = "https://allure.example"
    kb_active: bool = False
    feedback_server_url: str = ""

    def model_copy(self, *, update: dict[str, Any] | None = None) -> "_DummySettings":
        return replace(self, **(update or {}))


class _RecordingViewStore:
    """Стаб учёта просмотров отчётов."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def record_view(
        self,
        filename: str,
        *,
        project_id: int | None = None,
        launch_id: int | None = None,
        viewer_id: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "filename": filename,
                "project_id": project_id,
                "launch_id": launch_id,
                "viewer_id": viewer_id,
            }
        )
        if self.fail:
            raise RuntimeError("simulated DB outage")


def _mock_connect_context() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Вернуть psycopg.connect context, connection и cursor mocks."""
    cursor = MagicMock()
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    conn = MagicMock()
    conn.cursor.return_value = cursor_context
    connect_context = MagicMock()
    connect_context.__enter__.return_value = conn
    return connect_context, conn, cursor


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
    """Смонтированный MCP transport доступен на /mcp, а не /mcp/mcp."""
    mount = next(route for route in app.routes if getattr(route, "path", None) == "/mcp")
    inner_paths = {getattr(route, "path", None) for route in mount.app.routes}

    assert "/" in inner_paths
    assert "/mcp" not in inner_paths


@pytest.mark.asyncio
async def test_mcp_exact_path_is_rewritten_without_redirect() -> None:
    """POST /mcp доходит до mounted app как /mcp/ без видимого клиенту 307."""
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
# report_view store
# ---------------------------------------------------------------------------


def test_postgres_report_view_store_ensures_table_and_records_view(monkeypatch) -> None:
    """PostgresReportViewStore создаёт DDL и пишет просмотр через INSERT...SELECT."""
    from alla.report import report_store as module

    ddl_context, ddl_conn, ddl_cursor = _mock_connect_context()
    insert_context, insert_conn, insert_cursor = _mock_connect_context()
    connect = MagicMock(side_effect=[ddl_context, insert_context])
    monkeypatch.setattr(module.psycopg, "connect", connect)
    insert_cursor.fetchone.return_value = ("alla.report",)

    store = module.PostgresReportViewStore(dsn="postgresql://example/db")
    store.record_view("42_x.html", project_id=7, launch_id=42, viewer_id="viewer-abc")

    ddl_sql = ddl_cursor.execute.call_args.args[0]
    assert "CREATE TABLE IF NOT EXISTS alla.report_view" in ddl_sql
    assert "ALTER TABLE alla.report_view ADD COLUMN IF NOT EXISTS viewer_id" in ddl_sql
    assert "CREATE INDEX IF NOT EXISTS idx_report_view_viewed_at" in ddl_sql
    assert "CREATE INDEX IF NOT EXISTS idx_report_view_project_id" in ddl_sql
    assert "CREATE INDEX IF NOT EXISTS idx_report_view_filename" in ddl_sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_report_view_unique_per_viewer" in ddl_sql
    assert "WHERE viewer_id IS NOT NULL" in ddl_sql
    ddl_conn.commit.assert_called_once()

    exists_sql, exists_params = insert_cursor.execute.call_args_list[0].args
    assert "to_regclass" in exists_sql
    assert exists_params == ("alla.report",)

    insert_sql, params = insert_cursor.execute.call_args_list[1].args
    assert "INSERT INTO alla.report_view" in insert_sql
    assert "SELECT %(filename)s" in insert_sql
    assert "LEFT JOIN alla.report r ON r.filename = k.f" in insert_sql
    assert "ON CONFLICT (filename, viewer_id) WHERE viewer_id IS NOT NULL DO NOTHING" in insert_sql
    assert params == {
        "filename": "42_x.html",
        "project_id": 7,
        "launch_id": 42,
        "viewer_id": "viewer-abc",
    }
    insert_conn.commit.assert_called_once()


def test_postgres_report_view_store_records_without_report_table(monkeypatch) -> None:
    """FS-only просмотры пишутся даже если alla.report ещё не создана."""
    from alla.report import report_store as module

    ddl_context, _, _ = _mock_connect_context()
    insert_context, insert_conn, insert_cursor = _mock_connect_context()
    connect = MagicMock(side_effect=[ddl_context, insert_context])
    monkeypatch.setattr(module.psycopg, "connect", connect)
    insert_cursor.fetchone.return_value = (None,)

    store = module.PostgresReportViewStore(dsn="postgresql://example/db")
    store.record_view("fs_only.html")

    insert_sql, params = insert_cursor.execute.call_args_list[1].args
    assert "INSERT INTO alla.report_view" in insert_sql
    assert "VALUES" in insert_sql
    assert "alla.report r" not in insert_sql
    assert "ON CONFLICT (filename, viewer_id) WHERE viewer_id IS NOT NULL DO NOTHING" in insert_sql
    assert params == {
        "filename": "fs_only.html",
        "project_id": None,
        "launch_id": None,
        "viewer_id": None,
    }
    insert_conn.commit.assert_called_once()


def test_postgres_report_view_store_caches_report_table_after_success(monkeypatch) -> None:
    """После первого обнаружения alla.report to_regclass больше не вызывается."""
    from alla.report import report_store as module

    ddl_context, _, _ = _mock_connect_context()
    first_context, _, first_cursor = _mock_connect_context()
    second_context, _, second_cursor = _mock_connect_context()
    connect = MagicMock(side_effect=[ddl_context, first_context, second_context])
    monkeypatch.setattr(module.psycopg, "connect", connect)
    first_cursor.fetchone.return_value = ("alla.report",)

    store = module.PostgresReportViewStore(dsn="postgresql://example/db")
    store.record_view("first.html")
    store.record_view("second.html")

    first_exists_sql = first_cursor.execute.call_args_list[0].args[0]
    assert "to_regclass" in first_exists_sql
    assert len(first_cursor.execute.call_args_list) == 2

    assert len(second_cursor.execute.call_args_list) == 1
    second_sql, second_params = second_cursor.execute.call_args.args
    assert "LEFT JOIN alla.report r ON r.filename = k.f" in second_sql
    assert "to_regclass" not in second_sql
    assert second_params == {
        "filename": "second.html",
        "project_id": None,
        "launch_id": None,
        "viewer_id": None,
    }


def test_postgres_report_view_store_record_view_is_best_effort(
    monkeypatch,
    caplog,
) -> None:
    """Ошибка записи просмотра логируется warning-ом и не пробрасывается наружу."""
    from alla.report import report_store as module

    ddl_context, _, _ = _mock_connect_context()
    connect = MagicMock(side_effect=[ddl_context, RuntimeError("postgres unavailable")])
    monkeypatch.setattr(module.psycopg, "connect", connect)

    store = module.PostgresReportViewStore(dsn="postgresql://example/db")

    with caplog.at_level("WARNING", logger=module.__name__):
        assert store.record_view("42_x.html") is None

    assert "report_view recording failed for 42_x.html" in caplog.text


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


@pytest.mark.asyncio
async def test_analyze_html_push_report_link_false_does_not_attach_report_link(
    monkeypatch,
    _http_client,
) -> None:
    """?push_report_link=false запрещает запись ссылки на отчёт в TestOps."""
    mock_client = _MockClient()
    _setup_state(
        client=mock_client,
        settings=_DummySettings(
            push_report_link=True,
            report_url="https://jenkins.example/alla-report.html",
        ),
    )
    captured_settings: dict[str, Any] = {}

    async def mock_analyze(launch_id, client, settings, *, updater=None):
        captured_settings["push_report_link"] = settings.push_report_link
        return _make_analysis_result()

    monkeypatch.setattr("alla.orchestrator.analyze_launch", mock_analyze)
    monkeypatch.setattr(
        "alla.server.build_html_report_content",
        lambda result, *, settings: "<html><body>report</body></html>",
    )
    monkeypatch.setattr("alla.server.persist_generated_report", lambda **kwargs: None)

    async with _http_client as client:
        resp = await client.post("/api/v1/analyze/123/html?push_report_link=false")

    assert resp.status_code == 200
    assert captured_settings["push_report_link"] is False
    assert resp.headers["X-Report-URL"] == "https://jenkins.example/alla-report.html"
    assert mock_client.patch_launch_link_calls == []


@pytest.mark.asyncio
async def test_analyze_html_attaches_report_link_when_only_comments_disabled(
    monkeypatch,
    _http_client,
) -> None:
    """push_comments=false + push_report_link=true → ссылка прикрепляется."""
    mock_client = _MockClient()
    _setup_state(
        client=mock_client,
        settings=_DummySettings(
            push_comments=False,
            push_report_link=True,
            report_url="https://jenkins.example/alla-report.html",
            report_link_name="[Alla] HTML",
        ),
    )

    async def mock_analyze(launch_id, client, settings, *, updater=None):
        return _make_analysis_result()

    monkeypatch.setattr("alla.orchestrator.analyze_launch", mock_analyze)
    monkeypatch.setattr(
        "alla.server.build_html_report_content",
        lambda result, *, settings: "<html><body>report</body></html>",
    )
    monkeypatch.setattr("alla.server.persist_generated_report", lambda **kwargs: None)

    async with _http_client as client:
        resp = await client.post(
            "/api/v1/analyze/123/html?push_comments=false&push_report_link=true",
            headers={"Origin": "https://jenkins.example"},
        )

    assert resp.status_code == 200
    assert resp.headers["X-Report-URL"] == "https://jenkins.example/alla-report.html"
    assert "X-Report-URL" in resp.headers["Access-Control-Expose-Headers"]
    assert mock_client.patch_launch_link_calls == [
        (123, "[Alla] HTML", "https://jenkins.example/alla-report.html"),
    ]


@pytest.mark.asyncio
async def test_analyze_push_comments_query_overrides_settings(
    monkeypatch,
    _http_client,
) -> None:
    """?push_comments=true пробрасывается в settings.push_comments."""
    _setup_state(settings=_DummySettings(push_comments=False, push_report_link=False))
    captured_settings: dict[str, Any] = {}

    async def mock_analyze(launch_id, client, settings, *, updater=None):
        captured_settings["push_comments"] = settings.push_comments
        return _make_analysis_result()

    monkeypatch.setattr("alla.orchestrator.analyze_launch", mock_analyze)

    async with _http_client as client:
        resp = await client.post("/api/v1/analyze/123?push_comments=true")

    assert resp.status_code == 200
    assert captured_settings["push_comments"] is True


# ---------------------------------------------------------------------------
# POST /api/v1/analyze/{launch_id} — маппинг ошибок
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
# GET /reports/{filename}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_report_records_view_for_postgres_report(_http_client) -> None:
    """Успешная PG-ветка GET /reports/{filename} пишет просмотр отчёта."""

    class _ReportStore:
        def load(self, filename: str) -> str | None:
            return "<html><body>from pg</body></html>" if filename == "42_x.html" else None

    view_store = _RecordingViewStore()
    _setup_state(settings=_DummySettings())

    from alla.server import _state

    _state.report_store = _ReportStore()
    _state.report_view_store = view_store

    async with _http_client as client:
        resp = await client.get("/reports/42_x.html")

    assert resp.status_code == 200
    assert resp.text == "<html><body>from pg</body></html>"
    assert len(view_store.calls) == 1
    call = view_store.calls[0]
    assert call["filename"] == "42_x.html"
    assert call["project_id"] is None
    assert call["launch_id"] is None
    assert isinstance(call["viewer_id"], str) and call["viewer_id"]
    # Первый визит — сервер выставляет cookie.
    assert "alla_viewer_id" in resp.cookies


@pytest.mark.asyncio
async def test_get_report_records_view_for_filesystem_report(
    _http_client,
    tmp_path,
) -> None:
    """Успешная FS-ветка GET /reports/{filename} тоже пишет просмотр отчёта."""
    report_path = tmp_path / "42_x.html"
    report_path.write_text("<html><body>from fs</body></html>", encoding="utf-8")
    view_store = _RecordingViewStore()
    _setup_state(settings=_DummySettings(reports_dir=str(tmp_path)))

    from alla.server import _state

    _state.report_view_store = view_store

    async with _http_client as client:
        resp = await client.get("/reports/42_x.html")

    assert resp.status_code == 200
    assert resp.text == "<html><body>from fs</body></html>"
    assert len(view_store.calls) == 1
    assert view_store.calls[0]["filename"] == "42_x.html"
    assert isinstance(view_store.calls[0]["viewer_id"], str)
    assert "alla_viewer_id" in resp.cookies


@pytest.mark.asyncio
async def test_get_report_allows_missing_report_view_store(_http_client, tmp_path) -> None:
    """Если store учёта просмотров не создан, отчёт продолжает отдаваться."""
    (tmp_path / "42_x.html").write_text("<html><body>ok</body></html>", encoding="utf-8")
    _setup_state(settings=_DummySettings(reports_dir=str(tmp_path)))

    async with _http_client as client:
        resp = await client.get("/reports/42_x.html")

    assert resp.status_code == 200
    assert resp.text == "<html><body>ok</body></html>"


@pytest.mark.asyncio
async def test_get_report_ignores_report_view_store_failure(_http_client, tmp_path) -> None:
    """Ошибка записи просмотра не должна ломать успешную отдачу HTML."""
    (tmp_path / "42_x.html").write_text("<html><body>still ok</body></html>", encoding="utf-8")
    view_store = _RecordingViewStore(fail=True)
    _setup_state(settings=_DummySettings(reports_dir=str(tmp_path)))

    from alla.server import _state

    _state.report_view_store = view_store

    async with _http_client as client:
        resp = await client.get("/reports/42_x.html")

    assert resp.status_code == 200
    assert resp.text == "<html><body>still ok</body></html>"
    assert len(view_store.calls) == 1


@pytest.mark.asyncio
async def test_get_report_reuses_viewer_cookie_on_reload(_http_client, tmp_path) -> None:
    """Перезагрузка с существующей cookie — тот же viewer_id, без повторного Set-Cookie."""
    (tmp_path / "42_x.html").write_text("<html><body>reload</body></html>", encoding="utf-8")
    view_store = _RecordingViewStore()
    _setup_state(settings=_DummySettings(reports_dir=str(tmp_path)))

    from alla.server import _state

    _state.report_view_store = view_store

    async with _http_client as client:
        first = await client.get("/reports/42_x.html")
        viewer = first.cookies.get("alla_viewer_id")
        assert viewer
        # Второй запрос с уже выставленной cookie (httpx сохраняет её на
        # клиенте автоматически после первого ответа).
        second = await client.get("/reports/42_x.html")

    assert first.status_code == 200
    assert second.status_code == 200
    # record_view вызывался дважды, оба раза с тем же viewer_id — дедуп уже
    # на стороне БД через ON CONFLICT, но id одинаковый, что и проверяем.
    assert len(view_store.calls) == 2
    assert view_store.calls[0]["viewer_id"] == viewer
    assert view_store.calls[1]["viewer_id"] == viewer
    # Cookie не выставляется повторно, если она уже была.
    assert "alla_viewer_id" not in second.cookies


@pytest.mark.asyncio
async def test_get_report_distinct_viewers_get_distinct_ids(
    _http_client,
    tmp_path,
) -> None:
    """Разные клиенты без cookie получают разные viewer_id."""
    (tmp_path / "42_x.html").write_text("<html><body>distinct</body></html>", encoding="utf-8")
    view_store = _RecordingViewStore()
    _setup_state(settings=_DummySettings(reports_dir=str(tmp_path)))

    from alla.server import _state

    _state.report_view_store = view_store

    async with _http_client as client:
        resp_a = await client.get("/reports/42_x.html")
        # Сбрасываем cookie между запросами, чтобы эмулировать другого пользователя.
        client.cookies.clear()
        resp_b = await client.get("/reports/42_x.html")

    assert resp_a.cookies.get("alla_viewer_id")
    assert resp_b.cookies.get("alla_viewer_id")
    assert resp_a.cookies["alla_viewer_id"] != resp_b.cookies["alla_viewer_id"]
    assert len(view_store.calls) == 2
    assert view_store.calls[0]["viewer_id"] != view_store.calls[1]["viewer_id"]


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
    assert data["id"] == make_kb_slug("Gateway timeout", entry.error_example)
    assert entry.id == data["id"]


@pytest.mark.asyncio
async def test_create_kb_entry_is_idempotent_on_retry_with_matching_payload(
    monkeypatch, _http_client
) -> None:
    """Повтор create с тем же payload (ретрай после потерянного ответа) — 200, created=False."""
    from alla.knowledge.models import KBEntry, RootCauseCategory

    payload = {
        "title": "Gateway timeout",
        "description": "desc",
        "error_example": "timeout while saving order",
        "category": "service",
        "resolution_steps": ["step 1"],
        "project_id": 42,
    }

    class _Store:
        def create_kb_entry(self, entry, project_id):
            return None  # симулируем ON CONFLICT DO NOTHING

        def find_kb_entry_by_slug(self, slug, project_id):
            return KBEntry(
                entry_id=99,
                id=slug,
                title=payload["title"],
                description=payload["description"],
                error_example=payload["error_example"],
                step_path=None,
                category=RootCauseCategory.SERVICE,
                resolution_steps=list(payload["resolution_steps"]),
                project_id=project_id,
            )

    monkeypatch.setattr("alla.server._get_feedback_store", lambda: _Store())

    async with _http_client as client:
        resp = await client.post("/api/v1/kb/entries", json=payload)

    assert resp.status_code == 200
    data = resp.json()
    assert data["entry_id"] == 99
    assert data["created"] is False


@pytest.mark.asyncio
async def test_create_kb_entry_returns_409_when_slug_collides_with_different_payload(
    monkeypatch, _http_client
) -> None:
    """Slug collide с другим содержимым — реальный конфликт, 409."""
    from alla.knowledge.models import KBEntry, RootCauseCategory

    class _Store:
        def create_kb_entry(self, entry, project_id):
            return None

        def find_kb_entry_by_slug(self, slug, project_id):
            return KBEntry(
                entry_id=99,
                id=slug,
                title="Совсем другая запись",
                description="другое",
                error_example="другая ошибка",
                step_path=None,
                category=RootCauseCategory.TEST,
                resolution_steps=["другой шаг"],
                project_id=project_id,
            )

    monkeypatch.setattr("alla.server._get_feedback_store", lambda: _Store())

    payload = {
        "title": "Gateway timeout",
        "description": "desc",
        "error_example": "timeout while saving order",
        "category": "service",
        "resolution_steps": ["step 1"],
        "project_id": 42,
    }

    async with _http_client as client:
        resp = await client.post("/api/v1/kb/entries", json=payload)

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_kb_entries_returns_entries_with_computed_counts(
    monkeypatch, _http_client
) -> None:
    """GET /api/v1/kb/entries возвращает KBEntry плюс lightweight counters."""
    from alla.knowledge.models import KBEntry, RootCauseCategory

    captured: dict[str, Any] = {}

    class _Store:
        def list_kb_entries(self, project_id):
            captured["project_id"] = project_id
            return [
                KBEntry(
                    entry_id=10,
                    id="gateway_timeout_abc12345",
                    title="Gateway timeout",
                    description="desc",
                    error_example="line 1\nline 2",
                    step_path="login -> submit",
                    category=RootCauseCategory.SERVICE,
                    resolution_steps=["restart", "retry"],
                    project_id=42,
                )
            ]

    monkeypatch.setattr("alla.server._get_feedback_store", lambda: _Store())

    async with _http_client as client:
        resp = await client.get("/api/v1/kb/entries", params={"project_id": 42})

    assert resp.status_code == 200
    assert captured["project_id"] == 42
    data = resp.json()
    assert data["count"] == 1
    assert data["entries"][0]["id"] == "gateway_timeout_abc12345"
    assert data["entries"][0]["error_example_chars"] == len("line 1\nline 2")
    assert data["entries"][0]["resolution_steps_count"] == 2


@pytest.mark.asyncio
async def test_delete_kb_entry_returns_409_when_feedback_exists(
    monkeypatch, _http_client
) -> None:
    """DELETE без force защищает записи, на которые уже есть feedback."""

    class _Store:
        def count_feedback_for_entry(self, entry_id):
            assert entry_id == 10
            return 3

        def delete_kb_entry(self, entry_id):  # pragma: no cover - не должен вызываться
            raise AssertionError("delete_kb_entry must not run without force")

    monkeypatch.setattr("alla.server._get_feedback_store", lambda: _Store())

    async with _http_client as client:
        resp = await client.delete("/api/v1/kb/entries/10")

    assert resp.status_code == 409
    assert resp.headers["x-feedback-count"] == "3"
    assert resp.json() == {
        "detail": "Cannot delete: kb_entry has 3 feedback votes. Pass force=true to cascade.",
        "feedback_count": 3,
    }


@pytest.mark.asyncio
async def test_delete_kb_entry_force_deletes_with_feedback(monkeypatch, _http_client) -> None:
    """DELETE с force=true вызывает store.delete_kb_entry даже при feedback."""
    captured: dict[str, Any] = {}

    class _Store:
        def count_feedback_for_entry(self, entry_id):
            captured["count_entry_id"] = entry_id
            return 3

        def delete_kb_entry(self, entry_id):
            captured["delete_entry_id"] = entry_id
            return True

    monkeypatch.setattr("alla.server._get_feedback_store", lambda: _Store())

    async with _http_client as client:
        resp = await client.delete("/api/v1/kb/entries/10", params={"force": "true"})

    assert resp.status_code == 200
    assert captured == {"count_entry_id": 10, "delete_entry_id": 10}
    assert resp.json() == {"entry_id": 10, "deleted": True}


@pytest.mark.asyncio
async def test_delete_kb_entry_returns_404_when_missing(monkeypatch, _http_client) -> None:
    """DELETE /api/v1/kb/entries/{id} → 404, если запись не найдена."""

    class _Store:
        def count_feedback_for_entry(self, entry_id):
            return 0

        def delete_kb_entry(self, entry_id):
            assert entry_id == 99
            return False

    monkeypatch.setattr("alla.server._get_feedback_store", lambda: _Store())

    async with _http_client as client:
        resp = await client.delete("/api/v1/kb/entries/99")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Entry 99 not found"


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
    """Resolve-эндпоинт работает только по issue_signature_hash/version."""
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
                "rule_kind": "base",
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


@pytest.mark.parametrize(
    ("llm_result", "llm_launch_summary", "expected"),
    [
        (
            LLMAnalysisResult(
                total_clusters=1,
                analyzed_count=1,
                failed_count=0,
                skipped_count=0,
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            ),
            LLMLaunchSummary(
                summary_text="summary",
                token_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
            ),
            TokenUsage(prompt_tokens=120, completion_tokens=60, total_tokens=180),
        ),
        (
            LLMAnalysisResult(
                total_clusters=1,
                analyzed_count=1,
                failed_count=0,
                skipped_count=0,
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            ),
            None,
            TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        ),
        (
            None,
            LLMLaunchSummary(
                summary_text="summary",
                token_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
            ),
            TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        ),
        (None, None, None),
    ],
)
def test_calculate_llm_token_usage(
    llm_result: LLMAnalysisResult | None,
    llm_launch_summary: LLMLaunchSummary | None,
    expected: TokenUsage | None,
) -> None:
    """Статистика токенов для дашборда суммирует LLM stage и не считает skipped LLM."""
    from alla.app_support import calculate_llm_token_usage

    result = _make_analysis_result(
        llm_result=llm_result,
        llm_launch_summary=llm_launch_summary,
    )

    assert calculate_llm_token_usage(result) == expected


# ---------------------------------------------------------------------------
# Skill pipeline endpoints (/api/v1/skill/...)
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from alla.models.clustering import (  # noqa: E402
    ClusterSignature,
    ClusteringReport,
    FailureCluster,
)
from alla.models.onboarding import OnboardingState  # noqa: E402
from alla.models.common import TestStatus as _SkillStatus  # noqa: E402
from alla.models.testops import FailedTestSummary  # noqa: E402
from alla.services.skill_state_service import (  # noqa: E402
    SKILL_RUN_SCHEMA_VERSION,
    SkillRun,
    SkillStateError,
)


def _skill_settings(**overrides) -> SimpleNamespace:
    base = dict(
        kb_active=True,
        kb_postgres_dsn="postgresql://u:p@localhost/db",
        reports_postgres=False,
        server_external_url="http://x",
        report_url="",
        report_link_name="Alla report",
        endpoint="https://allure.example",
        feedback_server_url="http://x",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _skill_triage() -> TriageReport:
    return TriageReport(
        launch_id=123,
        launch_name="Run",
        project_id=1,
        total_results=5,
        passed_count=4,
        failed_count=1,
        failed_tests=[
            FailedTestSummary(
                test_result_id=1,
                test_case_id=101,
                name="t1",
                status=_SkillStatus.FAILED,
                status_message="boom",
                log_snippet="ERROR boom",
            )
        ],
    )


def _skill_clustering() -> ClusteringReport:
    return ClusteringReport(
        launch_id=123,
        total_failures=1,
        cluster_count=1,
        unclustered_count=0,
        clusters=[
            FailureCluster(
                cluster_id="c-1",
                label="boom",
                signature=ClusterSignature(),
                member_test_ids=[1],
                member_count=1,
                representative_test_id=1,
                example_message="boom",
            )
        ],
    )


def _make_skill_run(**overrides) -> SkillRun:
    defaults = dict(
        run_id=42,
        schema_version=SKILL_RUN_SCHEMA_VERSION,
        status="clustered",
        launch_id=123,
        project_id=1,
        launch_name="Run",
        triage_report=_skill_triage(),
        clustering_report=_skill_clustering(),
        onboarding=OnboardingState(),
    )
    defaults.update(overrides)
    return SkillRun(**defaults)


@pytest.mark.asyncio
async def test_skill_get_run_501_when_kb_inactive(_http_client) -> None:
    _setup_state(settings=_DummySettings(kb_active=False))
    async with _http_client as client:
        resp = await client.get("/api/v1/skill/runs/42")
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_skill_get_run_returns_serialized(_http_client, monkeypatch) -> None:
    _setup_state(settings=_skill_settings())
    import alla.services.skill_state_service as state_mod

    monkeypatch.setattr(state_mod, "load_run", lambda *, dsn, run_id: _make_skill_run())

    async with _http_client as client:
        resp = await client.get("/api/v1/skill/runs/42")

    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == 42
    assert data["launch_id"] == 123
    TriageReport.model_validate(data["triage_report"])


@pytest.mark.asyncio
async def test_skill_get_run_404_when_missing(_http_client, monkeypatch) -> None:
    _setup_state(settings=_skill_settings())
    import alla.services.skill_state_service as state_mod

    def _raise(*, dsn, run_id):
        raise SkillStateError("not found")

    monkeypatch.setattr(state_mod, "load_run", _raise)

    async with _http_client as client:
        resp = await client.get("/api/v1/skill/runs/99")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_skill_create_run_persists_and_summarizes(_http_client, monkeypatch) -> None:
    _setup_state(settings=_skill_settings())

    import alla.orchestrator as orch
    import alla.services.kb_lookup_service as kb_mod
    import alla.services.skill_state_service as state_mod
    from alla.services.kb_lookup_service import KBStageResult

    monkeypatch.setattr(orch, "apply_merge_rules_phase", lambda r, c, s: c)
    monkeypatch.setattr(kb_mod, "lookup_kb_for_clusters", lambda r, c, s: KBStageResult())
    monkeypatch.setattr(orch, "build_onboarding_state", lambda *a, **k: OnboardingState())
    captured = {}

    def _create_run(*, dsn, triage_report, clustering_report, kb_stage, onboarding):
        captured["launch_id"] = triage_report.launch_id
        return 77

    monkeypatch.setattr(state_mod, "create_run", _create_run)

    body = {
        "triage_report": _skill_triage().model_dump(mode="json"),
        "clustering_report": _skill_clustering().model_dump(mode="json"),
    }
    async with _http_client as client:
        resp = await client.post("/api/v1/skill/runs", json=body)

    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == 77
    assert data["cluster_count"] == 1
    assert captured["launch_id"] == 123


@pytest.mark.asyncio
async def test_skill_create_run_422_without_triage(_http_client) -> None:
    _setup_state(settings=_skill_settings())
    async with _http_client as client:
        resp = await client.post("/api/v1/skill/runs", json={"clustering_report": None})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_skill_cluster_context_happy_and_404(_http_client, monkeypatch) -> None:
    _setup_state(settings=_skill_settings())
    import alla.services.skill_state_service as state_mod

    monkeypatch.setattr(state_mod, "load_run", lambda *, dsn, run_id: _make_skill_run())

    async with _http_client as client:
        ok = await client.get("/api/v1/skill/runs/42/clusters/c-1/context")
        missing = await client.get("/api/v1/skill/runs/42/clusters/nope/context")

    assert ok.status_code == 200
    assert ok.json()["cluster_id"] == "c-1"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_skill_submit_analysis_saves(_http_client, monkeypatch) -> None:
    _setup_state(settings=_skill_settings())
    import alla.services.agent_analysis_adapter as adapter_mod
    import alla.services.skill_state_service as state_mod

    monkeypatch.setattr(state_mod, "load_run", lambda *, dsn, run_id: _make_skill_run())
    monkeypatch.setattr(
        adapter_mod, "validate_agent_payload", lambda payload, *, expected_cluster_ids=None: ([], [])
    )
    saved = {}
    monkeypatch.setattr(
        state_mod,
        "save_agent_analysis",
        lambda *, dsn, run_id, agent_analysis, agent_summary_text: saved.update(
            {"run_id": run_id, "summary": agent_summary_text}
        ),
    )

    payload = {"schema_version": 1, "launch_summary": {"summary_text": "done"}, "clusters": {}}
    async with _http_client as client:
        resp = await client.post("/api/v1/skill/runs/42/analysis", json=payload)

    assert resp.status_code == 200
    assert resp.json()["clusters_expected"] == 1
    assert saved["summary"] == "done"


@pytest.mark.asyncio
async def test_skill_submit_analysis_422_on_invalid(_http_client, monkeypatch) -> None:
    _setup_state(settings=_skill_settings())
    import alla.services.agent_analysis_adapter as adapter_mod
    import alla.services.skill_state_service as state_mod
    from alla.services.agent_analysis_adapter import AgentAnalysisError

    monkeypatch.setattr(state_mod, "load_run", lambda *, dsn, run_id: _make_skill_run())

    def _bad(payload, *, expected_cluster_ids=None):
        raise AgentAnalysisError("nope")

    monkeypatch.setattr(adapter_mod, "validate_agent_payload", _bad)

    async with _http_client as client:
        resp = await client.post("/api/v1/skill/runs/42/analysis", json={"x": 1})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_skill_generate_report_returns_html(_http_client, monkeypatch) -> None:
    _setup_state(settings=_skill_settings())
    import alla.server as server_mod
    import alla.services.skill_api_service as api_svc
    import alla.services.skill_state_service as state_mod
    import alla.report.report_store as report_store_mod

    monkeypatch.setattr(state_mod, "load_run", lambda *, dsn, run_id: _make_skill_run())
    monkeypatch.setattr(
        api_svc, "build_analysis_result", lambda run, settings: _make_analysis_result()
    )
    monkeypatch.setattr(server_mod, "build_html_report_content", lambda result, *, settings: "<html>")

    class _FakeStore:
        def __init__(self, *, dsn):
            pass

        def save(self, *a, **k):
            pass

    monkeypatch.setattr(report_store_mod, "PostgresReportStore", _FakeStore)
    monkeypatch.setattr(state_mod, "save_report", lambda **k: None)

    async with _http_client as client:
        resp = await client.post("/api/v1/skill/runs/42/report")

    assert resp.status_code == 200
    data = resp.json()
    assert data["html"] == "<html>"
    assert data["saved_to_db"] is True
    assert data["report_filename"].endswith(".html")
    assert data["report_url"].startswith("http://x/reports/")


@pytest.mark.asyncio
async def test_skill_save_push_result(_http_client, monkeypatch) -> None:
    _setup_state(settings=_skill_settings())
    import alla.services.skill_state_service as state_mod

    captured = {}
    monkeypatch.setattr(
        state_mod,
        "save_push_result",
        lambda *, dsn, run_id, push_result: captured.update(
            {"run_id": run_id, "result": push_result}
        ),
    )

    async with _http_client as client:
        resp = await client.post(
            "/api/v1/skill/runs/42/push-result", json={"comments_posted": 3}
        )

    assert resp.status_code == 200
    assert captured["result"]["comments_posted"] == 3


@pytest.mark.asyncio
async def test_skill_generate_report_db_failure_no_dangling_url(_http_client, monkeypatch) -> None:
    """Если запись в БД упала — saved_to_db=False, без /reports-ссылки, HTML отдан."""
    _setup_state(settings=_skill_settings())
    import alla.server as server_mod
    import alla.services.skill_api_service as api_svc
    import alla.services.skill_state_service as state_mod
    import alla.report.report_store as report_store_mod

    monkeypatch.setattr(state_mod, "load_run", lambda *, dsn, run_id: _make_skill_run())
    monkeypatch.setattr(
        api_svc, "build_analysis_result", lambda run, settings: _make_analysis_result()
    )
    monkeypatch.setattr(server_mod, "build_html_report_content", lambda result, *, settings: "<html>")

    class _FailingStore:
        def __init__(self, *, dsn):
            pass

        def save(self, *a, **k):
            raise RuntimeError("DB down")

    monkeypatch.setattr(report_store_mod, "PostgresReportStore", _FailingStore)
    monkeypatch.setattr(state_mod, "save_report", lambda **k: None)

    async with _http_client as client:
        resp = await client.post("/api/v1/skill/runs/42/report")

    assert resp.status_code == 200
    data = resp.json()
    assert data["html"] == "<html>"
    assert data["saved_to_db"] is False
    assert "/reports/" not in (data["report_url"] or "")


@pytest.mark.asyncio
async def test_skill_generate_report_store_init_failure(_http_client, monkeypatch) -> None:
    """Ошибка инициализации стора (DDL/права) не валит endpoint."""
    _setup_state(settings=_skill_settings())
    import alla.server as server_mod
    import alla.services.skill_api_service as api_svc
    import alla.services.skill_state_service as state_mod
    import alla.report.report_store as report_store_mod

    monkeypatch.setattr(state_mod, "load_run", lambda *, dsn, run_id: _make_skill_run())
    monkeypatch.setattr(
        api_svc, "build_analysis_result", lambda run, settings: _make_analysis_result()
    )
    monkeypatch.setattr(server_mod, "build_html_report_content", lambda result, *, settings: "<html>")

    class _BrokenInitStore:
        def __init__(self, *, dsn):
            raise RuntimeError("permission denied")

    monkeypatch.setattr(report_store_mod, "PostgresReportStore", _BrokenInitStore)
    monkeypatch.setattr(state_mod, "save_report", lambda **k: None)

    async with _http_client as client:
        resp = await client.post("/api/v1/skill/runs/42/report")

    assert resp.status_code == 200
    assert resp.json()["saved_to_db"] is False


@pytest.mark.asyncio
async def test_reports_serves_skill_saved_report_without_reports_postgres(
    _http_client, monkeypatch
) -> None:
    """/reports читает отчёт из alla.report, сохранённый skill-flow при reports_postgres=false."""
    _setup_state(settings=_skill_settings())
    import alla.report.report_store as report_store_mod

    class _ReadStore:
        def __init__(self, *, dsn):
            pass

        def load(self, filename):
            return "<html>skill</html>"

    monkeypatch.setattr(report_store_mod, "PostgresReportStore", _ReadStore)

    async with _http_client as client:
        resp = await client.get("/reports/alla_launch_1_run_1_x.html")

    assert resp.status_code == 200
    assert "skill" in resp.text


@pytest.mark.asyncio
async def test_skill_summary_context_422_when_clusters_not_object(_http_client) -> None:
    _setup_state(settings=_skill_settings())
    async with _http_client as client:
        resp = await client.post(
            "/api/v1/skill/runs/42/summary-context", json={"clusters": ["a", "b"]}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_reports_db_load_error_falls_back_to_filesystem(
    _http_client, monkeypatch, tmp_path
) -> None:
    """Временная ошибка чтения из БД не обрывает /reports — отдаём из reports_dir."""
    (tmp_path / "alla_launch_1_run_1_x.html").write_text("<html>fs</html>", encoding="utf-8")
    _setup_state(settings=_skill_settings(reports_dir=str(tmp_path)))
    import alla.report.report_store as report_store_mod

    class _FlakyStore:
        def __init__(self, *, dsn):
            pass

        def load(self, filename):
            raise RuntimeError("DB blip")

    monkeypatch.setattr(report_store_mod, "PostgresReportStore", _FlakyStore)

    async with _http_client as client:
        resp = await client.get("/reports/alla_launch_1_run_1_x.html")

    assert resp.status_code == 200
    assert "fs" in resp.text
