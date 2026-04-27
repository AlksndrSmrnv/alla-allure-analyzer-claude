"""Тесты дашборда использования (stats_store, html_view, backfill, эндпоинты)."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import pytest

from alla.dashboard.html_view import render_dashboard_html_shell
from alla.dashboard.stats_store import _PER_PROJECT_SQL, gap_fill_series
from alla.models.testops import LaunchResponse


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


@dataclass
class _DashboardSettings:
    """Минимальный Settings-стаб для дашборд-эндпоинтов."""
    kb_postgres_dsn: str = "postgresql://fake/fake"
    feedback_server_url: str = ""
    detail_concurrency: int = 5

    @property
    def kb_active(self) -> bool:
        return bool(self.kb_postgres_dsn)


class _StubClient:
    """Минимальный стаб TestOps-клиента для тестов."""

    def __init__(self, *, projects: dict[int, str] | None = None, raise_on_list: bool = False) -> None:
        self._projects = projects or {}
        self._raise = raise_on_list
        self.list_calls = 0

    async def list_projects(self) -> dict[int, str]:
        self.list_calls += 1
        if self._raise:
            raise RuntimeError("TestOps unavailable")
        return self._projects


class _StubStatsStore:
    def __init__(
        self,
        *,
        kpis: dict[str, int] | None = None,
        per_project: list[dict[str, Any]] | None = None,
        series: list[dict[str, Any]] | None = None,
    ) -> None:
        self._kpis = kpis or {
            "total_reports": 5,
            "total_kb_entries": 3,
            "total_likes": 2,
            "total_dislikes": 1,
            "total_merge_rules": 0,
            "active_projects": 1,
        }
        self._per_project = per_project or [
            {
                "project_id": 7,
                "reports": 5,
                "kb_entries": 3,
                "likes": 2,
                "dislikes": 1,
                "merge_rules": 0,
                "last_activity": "2026-04-27T10:00:00+00:00",
            },
            {
                "project_id": None,
                "reports": 1,
                "kb_entries": 0,
                "likes": 0,
                "dislikes": 0,
                "merge_rules": 0,
                "last_activity": None,
            },
        ]
        self._series = series or [
            {"day": "2026-04-26", "n": 2},
            {"day": "2026-04-27", "n": 3},
        ]

    def totals(self, *, days: int) -> dict[str, int]:
        return self._kpis

    def per_project_rollup(self, *, days: int) -> list[dict[str, Any]]:
        return self._per_project

    def reports_per_day(self, *, days: int) -> list[dict[str, Any]]:
        return self._series


class _ThreadRecordingStatsStore(_StubStatsStore):
    """Stats store, который запоминает thread id каждого sync DB-вызова."""

    def __init__(self) -> None:
        super().__init__()
        self.thread_ids: list[int] = []

    def totals(self, *, days: int) -> dict[str, int]:
        self.thread_ids.append(threading.get_ident())
        return super().totals(days=days)

    def per_project_rollup(self, *, days: int) -> list[dict[str, Any]]:
        self.thread_ids.append(threading.get_ident())
        return super().per_project_rollup(days=days)

    def reports_per_day(self, *, days: int) -> list[dict[str, Any]]:
        self.thread_ids.append(threading.get_ident())
        return super().reports_per_day(days=days)


@pytest.fixture
def _http_client():
    from alla.server import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture(autouse=True)
def _reset_dashboard_state():
    """Сбрасываем lazy dashboard state между тестами."""
    from alla.server import _reset_lazy_stores_and_caches

    _reset_lazy_stores_and_caches()
    yield
    _reset_lazy_stores_and_caches()


# ---------------------------------------------------------------------------
# stats_store.gap_fill_series
# ---------------------------------------------------------------------------


def test_reports_per_day_gap_fills() -> None:
    """Пропущенные дни заполняются нулями, длина равна days."""
    rows = [
        (date(2026, 4, 25), 5),
        (date(2026, 4, 27), 3),
    ]
    out = gap_fill_series(rows, days=4, today=date(2026, 4, 27))
    assert [p["day"] for p in out] == [
        "2026-04-24",
        "2026-04-25",
        "2026-04-26",
        "2026-04-27",
    ]
    assert [p["n"] for p in out] == [0, 5, 0, 3]


def test_reports_per_day_empty_input() -> None:
    """Пустой input даёт массив длиной days со всеми нулями."""
    out = gap_fill_series([], days=3, today=date(2026, 4, 27))
    assert len(out) == 3
    assert all(p["n"] == 0 for p in out)


def test_per_project_sql_joins_null_project_ids() -> None:
    """NULL project_id из CTE должен матчиться с NULL project_id в ids."""
    assert "USING (project_id)" not in _PER_PROJECT_SQL
    for alias in ["r", "k", "fb", "mr"]:
        assert (
            f"ids.project_id IS NOT DISTINCT FROM {alias}.project_id"
            in _PER_PROJECT_SQL
        )


# ---------------------------------------------------------------------------
# html_view.render_dashboard_html_shell
# ---------------------------------------------------------------------------


def test_dashboard_html_no_external_urls() -> None:
    """HTML-страница self-contained: ни одной http(s)://-ссылки кроме data:image."""
    html = render_dashboard_html_shell()
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html and "<script>" in html
    assert "--primary: #2563eb" in html
    # Все http(s) ссылки запрещены
    assert not re.search(r"https?://", html)


def test_dashboard_html_has_required_elements() -> None:
    """В шаблоне присутствуют все целевые DOM-узлы, к которым обращается JS."""
    html = render_dashboard_html_shell()
    for marker in [
        'id="kpis"',
        'id="ratioBar"',
        'id="bars"',
        'id="top5List"',
        'id="projectsTable"',
        'id="daysSelect"',
        'id="generatedAt"',
    ]:
        assert marker in html, f"missing {marker}"


def test_dashboard_html_top5_uses_russian_knowledge_base_copy() -> None:
    """User-facing Top-5 copy называет базу знаний без KB-аббревиатуры."""
    html = render_dashboard_html_shell()
    assert "KB-записей" not in html
    assert "записей в базе знаний" in html


# ---------------------------------------------------------------------------
# /api/v1/dashboard/stats
# ---------------------------------------------------------------------------


def test_lifespan_reset_clears_dashboard_store_and_project_names_cache() -> None:
    """Lifespan reset должен сбрасывать dashboard store и TTL-кэш имён."""
    from alla.server import _reset_lazy_stores_and_caches, _state

    sentinel = object()
    _state.report_store = sentinel
    _state.feedback_store = sentinel
    _state.merge_rules_store = sentinel
    _state.dashboard_store = sentinel
    _state.project_names_cache = {7: "Old project"}
    _state.project_names_expires_at = 123.0

    _reset_lazy_stores_and_caches()

    assert _state.report_store is None
    assert _state.feedback_store is None
    assert _state.merge_rules_store is None
    assert _state.dashboard_store is None
    assert _state.project_names_cache == {}
    assert _state.project_names_expires_at == 0.0


@pytest.mark.asyncio
async def test_dashboard_stats_503_without_dsn(_http_client, monkeypatch) -> None:
    """Без ALLURE_KB_POSTGRES_DSN эндпоинт отдаёт 503 с осмысленным detail."""
    from alla.server import _state

    settings = _DashboardSettings(kb_postgres_dsn="")
    monkeypatch.setattr(_state, "settings", settings)
    monkeypatch.setattr(_state, "client", _StubClient())
    monkeypatch.setattr(_state, "dashboard_store", None)

    async with _http_client as client:
        resp = await client.get("/api/v1/dashboard/stats")

    assert resp.status_code == 503
    body = resp.json()
    assert "ALLURE_KB_POSTGRES_DSN" in body["detail"]


@pytest.mark.asyncio
async def test_dashboard_stats_falls_back_when_testops_unavailable(
    _http_client, monkeypatch
) -> None:
    """Если list_projects падает, дашборд возвращает 200 с лейблами Project #N."""
    from alla.server import _state

    settings = _DashboardSettings()
    store = _StubStatsStore()
    client_stub = _StubClient(raise_on_list=True)

    monkeypatch.setattr(_state, "settings", settings)
    monkeypatch.setattr(_state, "client", client_stub)
    monkeypatch.setattr(_state, "dashboard_store", store)

    async with _http_client as http:
        resp = await http.get("/api/v1/dashboard/stats?days=30")

    assert resp.status_code == 200
    data = resp.json()
    rows_by_pid = {row["project_id"]: row for row in data["per_project"]}
    assert rows_by_pid[7]["project_name"] == "Project #7"
    assert rows_by_pid[None]["project_name"] == "Без привязки к проекту"
    assert data["kpis"]["total_reports"] == 5


@pytest.mark.asyncio
async def test_dashboard_stats_uses_testops_names(_http_client, monkeypatch) -> None:
    """Если list_projects возвращает имена, они подставляются в ответ."""
    from alla.server import _state

    settings = _DashboardSettings()
    store = _StubStatsStore()
    client_stub = _StubClient(projects={7: "Mobile App"})

    monkeypatch.setattr(_state, "settings", settings)
    monkeypatch.setattr(_state, "client", client_stub)
    monkeypatch.setattr(_state, "dashboard_store", store)

    async with _http_client as http:
        resp = await http.get("/api/v1/dashboard/stats?days=30")

    assert resp.status_code == 200
    data = resp.json()
    rows_by_pid = {row["project_id"]: row for row in data["per_project"]}
    assert rows_by_pid[7]["project_name"] == "Mobile App"


@pytest.mark.asyncio
async def test_dashboard_stats_reads_db_in_threadpool(_http_client, monkeypatch) -> None:
    """Sync stats store вызовы не должны выполняться на event loop thread."""
    from alla.server import _state

    settings = _DashboardSettings()
    store = _ThreadRecordingStatsStore()
    client_stub = _StubClient(projects={7: "Mobile App"})

    monkeypatch.setattr(_state, "settings", settings)
    monkeypatch.setattr(_state, "client", client_stub)
    monkeypatch.setattr(_state, "dashboard_store", store)

    event_loop_thread_id = threading.get_ident()
    async with _http_client as http:
        resp = await http.get("/api/v1/dashboard/stats?days=30")

    assert resp.status_code == 200
    assert len(store.thread_ids) == 3
    assert all(thread_id != event_loop_thread_id for thread_id in store.thread_ids)


@pytest.mark.asyncio
async def test_dashboard_html_endpoint_returns_shell(_http_client, monkeypatch) -> None:
    """GET /dashboard отдаёт HTML-оболочку независимо от наличия DSN."""
    from alla.server import _state

    monkeypatch.setattr(_state, "settings", _DashboardSettings(kb_postgres_dsn=""))
    monkeypatch.setattr(_state, "client", _StubClient())

    async with _http_client as http:
        resp = await http.get("/dashboard")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Дашборд использования alla" in resp.text


# ---------------------------------------------------------------------------
# persist_generated_report → report_store.save(project_id=...)
# ---------------------------------------------------------------------------


def test_persist_generated_report_passes_project_id(tmp_path) -> None:
    """persist_generated_report пробрасывает project_id в report_store.save."""
    from alla.app_support import persist_generated_report

    @dataclass
    class _Settings:
        reports_dir: str = ""

    class _RecordingStore:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def save(self, filename, launch_id, html, project_id=None) -> None:
            self.calls.append((filename, launch_id, html, project_id))

    store = _RecordingStore()
    persist_generated_report(
        html_content="<html></html>",
        launch_id=42,
        report_filename="42_x.html",
        settings=_Settings(),
        report_store=store,
        project_id=99,
    )
    assert store.calls == [("42_x.html", 42, "<html></html>", 99)]


# ---------------------------------------------------------------------------
# backfill.backfill_report_projects
# ---------------------------------------------------------------------------


class _FakeBackfillClient:
    """TestOps-клиент с нужным subset методов (get_launch)."""

    def __init__(self, mapping: dict[int, int | None]) -> None:
        self._mapping = mapping
        self.calls: list[int] = []

    async def get_launch(self, launch_id: int) -> LaunchResponse:
        self.calls.append(launch_id)
        if launch_id not in self._mapping:
            raise RuntimeError("network down")
        return LaunchResponse(id=launch_id, projectId=self._mapping[launch_id])


@pytest.mark.asyncio
async def test_backfill_resolves_project_ids(monkeypatch) -> None:
    """Бэкфилл вытягивает project_id из TestOps и UPDATE-ит alla.report."""
    from alla.dashboard import backfill as backfill_mod

    monkeypatch.setattr(
        backfill_mod, "_list_unattributed_launch_ids",
        lambda dsn, *, limit=None: [101, 102, 103],
    )

    update_calls: dict[int, int] = {}

    def fake_apply(dsn: str, mapping: dict[int, int]) -> int:
        update_calls.update(mapping)
        return len(mapping)

    monkeypatch.setattr(backfill_mod, "_apply_updates", fake_apply)

    client = _FakeBackfillClient({101: 7, 102: None, 103: 9})
    report = await backfill_mod.backfill_report_projects(
        client, dsn="fake://", concurrency=2, dry_run=False,
    )

    assert sorted(client.calls) == [101, 102, 103]
    assert update_calls == {101: 7, 103: 9}
    assert report.total == 3
    assert report.resolved == 2
    assert report.skipped == 1
    assert report.failed == 0
    assert report.rows_updated == 2


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_update(monkeypatch) -> None:
    """В режиме dry-run UPDATE не выполняется."""
    from alla.dashboard import backfill as backfill_mod

    monkeypatch.setattr(
        backfill_mod, "_list_unattributed_launch_ids",
        lambda dsn, *, limit=None: [101],
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("UPDATE should not run in dry-run")

    monkeypatch.setattr(backfill_mod, "_apply_updates", fail)

    client = _FakeBackfillClient({101: 7})
    report = await backfill_mod.backfill_report_projects(
        client, dsn="fake://", dry_run=True,
    )
    assert report.total == 1
    assert report.resolved == 1
    assert report.rows_updated == 0


@pytest.mark.asyncio
async def test_backfill_handles_testops_failures(monkeypatch) -> None:
    """Сетевые ошибки TestOps учитываются в failed, не падают весь бэкфилл."""
    from alla.dashboard import backfill as backfill_mod

    monkeypatch.setattr(
        backfill_mod, "_list_unattributed_launch_ids",
        lambda dsn, *, limit=None: [101, 102],
    )
    monkeypatch.setattr(backfill_mod, "_apply_updates", lambda dsn, m: len(m))

    # 101 успешно, 102 бросает в get_launch
    client = _FakeBackfillClient({101: 7})
    report = await backfill_mod.backfill_report_projects(
        client, dsn="fake://", dry_run=False,
    )
    assert report.resolved == 1
    assert report.failed == 1
