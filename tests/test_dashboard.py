"""Тесты дашборда использования (stats_store, html_view, эндпоинты)."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import pytest

from alla.dashboard.html_view import render_dashboard_html_shell
from alla.dashboard.stats_store import (
    _KPI_SQL,
    _PER_PROJECT_SQL,
    _REPORTS_FOR_PROJECT_SQL,
    DateWindow,
    gap_fill_series,
)


# ---------------------------------------------------------------------------
# Вспомогательные функции / fixtures
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
            "report_views": 12,
            "total_kb_entries": 3,
            "total_merge_rules": 0,
            "active_projects": 1,
            "unique_launches": 4,
            "llm_total_tokens": 900,
            "llm_prompt_tokens": 700,
            "llm_completion_tokens": 200,
            "llm_avg_tokens_per_run": 450,
            "llm_avg_prompt_tokens_per_run": 350,
            "llm_avg_completion_tokens_per_run": 100,
            "llm_reports_with_usage": 2,
            "avg_analysis_duration_ms": 75000,
            "peak_day": "2026-04-27",
            "peak_day_count": 3,
        }
        self._per_project = per_project or [
            {
                "project_id": 7,
                "reports": 5,
                "report_views": 11,
                "kb_entries": 3,
                "merge_rules": 0,
                "llm_total_tokens": 900,
                "llm_prompt_tokens": 700,
                "llm_completion_tokens": 200,
                "llm_avg_tokens_per_run": 450,
                "llm_avg_prompt_tokens_per_run": 350,
                "llm_avg_completion_tokens_per_run": 100,
                "llm_reports_with_usage": 2,
                "avg_analysis_duration_ms": 75000,
                "last_activity": "2026-04-27T10:00:00+00:00",
            },
            {
                "project_id": None,
                "reports": 1,
                "report_views": 0,
                "kb_entries": 0,
                "merge_rules": 0,
                "llm_total_tokens": 0,
                "llm_prompt_tokens": 0,
                "llm_completion_tokens": 0,
                "llm_avg_tokens_per_run": 0,
                "llm_avg_prompt_tokens_per_run": 0,
                "llm_avg_completion_tokens_per_run": 0,
                "llm_reports_with_usage": 0,
                "avg_analysis_duration_ms": None,
                "last_activity": None,
            },
        ]
        self._series = series or [
            {"day": "2026-04-26", "n": 2},
            {"day": "2026-04-27", "n": 3},
        ]

    def totals(self, *, window) -> dict[str, Any]:
        return self._kpis

    def per_project_rollup(self, *, window) -> list[dict[str, Any]]:
        return self._per_project

    def reports_per_day(self, *, window) -> list[dict[str, Any]]:
        return self._series

    def reports_for_project(self, *, project_id, window, limit=200) -> list[dict[str, Any]]:
        return [
            {
                "filename": "42_x.html",
                "launch_id": 42,
                "created_at": "2026-04-27T10:00:00+00:00",
                "llm_prompt_tokens": 120,
                "llm_completion_tokens": 30,
                "llm_total_tokens": 150,
                "analysis_duration_ms": 60000,
                "view_count": 8,
            }
        ] if project_id == 7 else []


class _ThreadRecordingStatsStore(_StubStatsStore):
    """Хранилище статистики, которое запоминает thread id каждого sync DB-вызова."""

    def __init__(self) -> None:
        super().__init__()
        self.thread_ids: list[int] = []

    def totals(self, *, window) -> dict[str, Any]:
        self.thread_ids.append(threading.get_ident())
        return super().totals(window=window)

    def per_project_rollup(self, *, window) -> list[dict[str, Any]]:
        self.thread_ids.append(threading.get_ident())
        return super().per_project_rollup(window=window)

    def reports_per_day(self, *, window) -> list[dict[str, Any]]:
        self.thread_ids.append(threading.get_ident())
        return super().reports_per_day(window=window)


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
    """Пропущенные дни заполняются нулями для заданного списка дат."""
    rows = [
        (date(2026, 4, 25), 5),
        (date(2026, 4, 27), 3),
    ]
    series_dates = [
        date(2026, 4, 24),
        date(2026, 4, 25),
        date(2026, 4, 26),
        date(2026, 4, 27),
    ]
    out = gap_fill_series(rows, series_dates=series_dates)
    assert [p["day"] for p in out] == [d.isoformat() for d in series_dates]
    assert [p["n"] for p in out] == [0, 5, 0, 3]


def test_reports_per_day_empty_input() -> None:
    """Пустой input даёт массив длиной series_dates со всеми нулями."""
    series_dates = [date(2026, 4, 25), date(2026, 4, 26), date(2026, 4, 27)]
    out = gap_fill_series([], series_dates=series_dates)
    assert len(out) == 3
    assert all(p["n"] == 0 for p in out)


def test_date_window_from_day_is_single_day() -> None:
    """DateWindow.from_day охватывает ровно один календарный день."""
    window = DateWindow.from_day(date(2026, 4, 27))
    assert window.kind == "day"
    assert (window.end_ts - window.start_ts).total_seconds() == 86400
    assert window.series_dates() == [date(2026, 4, 27)]
    assert window.descriptor() == {"kind": "day", "value": "2026-04-27"}


def test_date_window_from_days_covers_n_dates() -> None:
    """DateWindow.from_days возвращает ровно N календарных дат для серии."""
    from datetime import datetime, timezone

    now = datetime(2026, 4, 27, 15, 0, tzinfo=timezone.utc)
    window = DateWindow.from_days(3, now=now)
    assert window.kind == "days"
    assert window.descriptor() == {"kind": "days", "value": 3}
    assert window.series_dates() == [
        date(2026, 4, 25),
        date(2026, 4, 26),
        date(2026, 4, 27),
    ]
    assert window.start_ts == datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
    assert window.end_ts == datetime(2026, 4, 28, 0, 0, tzinfo=timezone.utc)


def test_per_project_sql_joins_null_project_ids() -> None:
    """NULL project_id из CTE должен матчиться с NULL project_id в ids."""
    assert "USING (project_id)" not in _PER_PROJECT_SQL
    for alias in ["r", "k", "mr"]:
        assert (
            f"ids.project_id IS NOT DISTINCT FROM {alias}.project_id"
            in _PER_PROJECT_SQL
        )


def test_dashboard_sql_aggregates_llm_tokens_without_old_reports() -> None:
    """Среднее по токенам считается только по отчётам с сохранённым usage."""
    assert "llm_total_tokens IS NOT NULL" in _KPI_SQL
    assert "AVG(llm_total_tokens)" in _KPI_SQL
    assert "COUNT(llm_total_tokens)" in _KPI_SQL
    assert "FILTER (WHERE llm_total_tokens IS NOT NULL)" in _PER_PROJECT_SQL
    assert "COUNT(llm_total_tokens) AS llm_reports_with_usage" in _PER_PROJECT_SQL


def test_dashboard_sql_splits_prompt_and_completion_tokens() -> None:
    """KPI и per-project запросы возвращают prompt/completion отдельно от total."""
    assert "AS llm_prompt_tokens" in _KPI_SQL
    assert "AS llm_completion_tokens" in _KPI_SQL
    assert "AS llm_avg_prompt_tokens_per_run" in _KPI_SQL
    assert "AS llm_avg_completion_tokens_per_run" in _KPI_SQL
    assert "AS llm_prompt_tokens" in _PER_PROJECT_SQL
    assert "AS llm_completion_tokens" in _PER_PROJECT_SQL
    assert "AS llm_avg_prompt_tokens_per_run" in _PER_PROJECT_SQL
    assert "AS llm_avg_completion_tokens_per_run" in _PER_PROJECT_SQL


def test_dashboard_sql_includes_new_metrics() -> None:
    """KPI-запрос содержит метрики длительности, уникальных запусков и пика."""
    assert "AVG(analysis_duration_ms)" in _KPI_SQL
    assert "COUNT(DISTINCT launch_id)" in _KPI_SQL
    assert "WITH peak AS" in _KPI_SQL
    assert "AS report_views" in _KPI_SQL
    assert "FROM alla.report_view" in _KPI_SQL
    assert "v AS (" in _PER_PROJECT_SQL
    assert "AS report_views" in _PER_PROJECT_SQL
    assert "LEFT JOIN LATERAL" in _REPORTS_FOR_PROJECT_SQL
    assert "AS view_count" in _REPORTS_FOR_PROJECT_SQL
    assert "kb_feedback" not in _KPI_SQL
    assert "kb_feedback" not in _PER_PROJECT_SQL


def test_dashboard_sql_buckets_in_utc() -> None:
    """Бакетинг по дням должен быть в UTC, чтобы совпасть с DateWindow."""
    from alla.dashboard.stats_store import _REPORTS_PER_DAY_SQL

    assert "AT TIME ZONE 'UTC'" in _KPI_SQL
    assert "AT TIME ZONE 'UTC'" in _REPORTS_PER_DAY_SQL


# ---------------------------------------------------------------------------
# html_view.render_dashboard_html_shell
# ---------------------------------------------------------------------------


def test_dashboard_html_no_external_urls() -> None:
    """HTML-страница самодостаточна: ни одной http(s)://-ссылки кроме data:image."""
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
        'id="bars"',
        'id="projectsTable"',
        'id="daysSelect"',
        'id="daySelect"',
        'id="resetDay"',
        'id="windowLabel"',
        'id="generatedAt"',
    ]:
        assert marker in html, f"missing {marker}"
    for label in [
        "Токены за период",
        "Входные за период",
        "Выходные за период",
        "Токены / прогон",
        "Входные / прогон",
        "Выходные / прогон",
        "Среднее время анализа",
        "Уникальных запусков",
        "Среднее отчётов / день",
        "Просмотры отчётов",
        "Просмотры",
    ]:
        assert label in html
    assert 'data-col="report_views"' in html


def test_dashboard_html_drops_removed_widgets() -> None:
    """Виджеты лайков/дизлайков и Топ-5 проектов удалены из шаблона."""
    html = render_dashboard_html_shell()
    assert "ratioBar" not in html
    assert "ratioLegend" not in html
    assert "top5List" not in html
    assert "Лайки vs дизлайки" not in html
    assert "Топ-5 проектов" not in html


def test_dashboard_html_uses_russian_knowledge_base_copy() -> None:
    """В шаблоне используется русское «база знаний», не KB-аббревиатура."""
    html = render_dashboard_html_shell()
    assert "KB-записей" not in html
    assert "База знаний" in html


# ---------------------------------------------------------------------------
# /api/v1/dashboard/stats
# ---------------------------------------------------------------------------


def test_lifespan_reset_clears_dashboard_store_and_project_names_cache() -> None:
    """Сброс lifespan должен сбрасывать dashboard store и TTL-кэш имён."""
    from alla.server import _reset_lazy_stores_and_caches, _state

    sentinel = object()
    _state.report_store = sentinel
    _state.report_view_store = sentinel
    _state.feedback_store = sentinel
    _state.merge_rules_store = sentinel
    _state.dashboard_store = sentinel
    _state.project_names_cache = {7: "Old project"}
    _state.project_names_expires_at = 123.0

    _reset_lazy_stores_and_caches()

    assert _state.report_store is None
    assert _state.report_view_store is None
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
    assert data["kpis"]["report_views"] == 12
    assert data["kpis"]["llm_total_tokens"] == 900
    assert data["kpis"]["llm_prompt_tokens"] == 700
    assert data["kpis"]["llm_completion_tokens"] == 200
    assert data["kpis"]["llm_avg_tokens_per_run"] == 450
    assert data["kpis"]["llm_avg_prompt_tokens_per_run"] == 350
    assert data["kpis"]["llm_avg_completion_tokens_per_run"] == 100
    assert data["kpis"]["llm_reports_with_usage"] == 2
    assert rows_by_pid[7]["llm_total_tokens"] == 900
    assert rows_by_pid[7]["llm_prompt_tokens"] == 700
    assert rows_by_pid[7]["llm_completion_tokens"] == 200
    assert rows_by_pid[7]["llm_avg_tokens_per_run"] == 450
    assert rows_by_pid[7]["llm_avg_prompt_tokens_per_run"] == 350
    assert rows_by_pid[7]["llm_avg_completion_tokens_per_run"] == 100
    assert rows_by_pid[7]["llm_reports_with_usage"] == 2
    assert rows_by_pid[7]["report_views"] == 11


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
    """Синхронные вызовы stats store не должны выполняться на thread event loop."""
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
async def test_dashboard_project_reports_include_view_count(_http_client, monkeypatch) -> None:
    """Детальный endpoint отчётов проекта возвращает счётчик просмотров."""
    from alla.server import _state

    settings = _DashboardSettings()
    store = _StubStatsStore()

    monkeypatch.setattr(_state, "settings", settings)
    monkeypatch.setattr(_state, "client", _StubClient(projects={7: "Mobile App"}))
    monkeypatch.setattr(_state, "dashboard_store", store)

    async with _http_client as http:
        resp = await http.get("/api/v1/dashboard/projects/7/reports?days=30")

    assert resp.status_code == 200
    data = resp.json()
    assert data["reports"][0]["filename"] == "42_x.html"
    assert data["reports"][0]["view_count"] == 8


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
    """persist_generated_report пробрасывает project_id и token_usage в report_store.save."""
    from alla.app_support import persist_generated_report
    from alla.models.llm import LLMAnalysisResult, LLMLaunchSummary, TokenUsage
    from alla.models.testops import TriageReport
    from alla.orchestrator import AnalysisResult

    @dataclass
    class _Settings:
        reports_dir: str = ""

    class _RecordingStore:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def save(
            self,
            filename,
            launch_id,
            html,
            project_id=None,
            *,
            token_usage=None,
            analysis_duration_ms=None,
        ) -> None:
            self.calls.append(
                (filename, launch_id, html, project_id, token_usage, analysis_duration_ms)
            )

    store = _RecordingStore()
    analysis_result = AnalysisResult(
        triage_report=TriageReport(launch_id=42, total_results=0),
        llm_result=LLMAnalysisResult(
            total_clusters=1,
            analyzed_count=1,
            failed_count=0,
            skipped_count=0,
            token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        ),
        llm_launch_summary=LLMLaunchSummary(
            summary_text="summary",
            token_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        ),
        analysis_duration_seconds=12.5,
    )
    persist_generated_report(
        html_content="<html></html>",
        launch_id=42,
        report_filename="42_x.html",
        settings=_Settings(),
        report_store=store,
        project_id=99,
        analysis_result=analysis_result,
    )
    assert len(store.calls) == 1
    filename, launch_id, html, project_id, token_usage, analysis_duration_ms = store.calls[0]
    assert (filename, launch_id, html, project_id) == ("42_x.html", 42, "<html></html>", 99)
    assert token_usage == TokenUsage(prompt_tokens=120, completion_tokens=60, total_tokens=180)
    assert analysis_duration_ms == 12500


def test_persist_generated_report_keeps_skipped_llm_usage_null() -> None:
    """Если LLM stage полностью пропущен, report_store получает token_usage=None."""
    from alla.app_support import persist_generated_report
    from alla.models.testops import TriageReport
    from alla.orchestrator import AnalysisResult

    @dataclass
    class _Settings:
        reports_dir: str = ""

    class _RecordingStore:
        def __init__(self) -> None:
            self.token_usage: Any = "not-called"
            self.analysis_duration_ms: Any = "not-called"

        def save(
            self,
            filename,
            launch_id,
            html,
            project_id=None,
            *,
            token_usage=None,
            analysis_duration_ms=None,
        ) -> None:
            self.token_usage = token_usage
            self.analysis_duration_ms = analysis_duration_ms

    store = _RecordingStore()
    persist_generated_report(
        html_content="<html></html>",
        launch_id=42,
        report_filename="42_x.html",
        settings=_Settings(),
        report_store=store,
        project_id=99,
        analysis_result=AnalysisResult(
            triage_report=TriageReport(launch_id=42, total_results=0),
        ),
    )

    assert store.token_usage is None
