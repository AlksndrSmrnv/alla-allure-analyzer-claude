"""Полнота выгрузки прогона: лимит страниц, повторы id, идущий прогон, потерянные данные тестов."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from skill_fixtures import project_fixture, without_libmagic  # noqa: F401
from skill_fake_testops import FakeTestOps, default_launch
from alla_core.clients.auth import AllureAuthManager
from alla_core.clients.testops_client import AllureTestOpsClient
from alla_core.config import Settings
from alla_core.exceptions import PaginationLimitError
from alla_core.models.testops import FetchInfo
from alla_core.models.testops import TestResultResponse as Result
from alla_core.services.triage_service import _unique_results, fetch_warnings
from alla_skill_lib.errors import fetch_error_hint
from test_skill_flow import _prepare

LAUNCH = 777


def test_unique_results_counts_repeated_ids() -> None:
    fetch = FetchInfo()
    results = [Result(id=item) for item in (1, 2, 2, 3, 1)]

    unique = _unique_results(results, fetch)

    assert [result.id for result in unique] == [1, 2, 3]
    assert (fetch.duplicates, fetch.results_received) == (2, 3)


def test_complete_fetch_has_no_warnings() -> None:
    complete = FetchInfo(launch_closed=True, results_reported=8, results_received=8)

    assert fetch_warnings(complete, LAUNCH) == []
    # Поле closed TestOps не прислал — прогон не считаем идущим.
    assert fetch_warnings(FetchInfo(), LAUNCH) == []


def test_incomplete_fetch_warns_with_rerun_command() -> None:
    info = FetchInfo(launch_closed=False, results_reported=9, results_received=7,
                     execution_failed=2, detail_failed=1)

    warnings = fetch_warnings(info, LAUNCH)

    assert len(warnings) == 3
    assert "ещё не закрыт" in warnings[0]
    assert "насчитал 9 результатов, получено 7" in warnings[1]
    assert "дерево шагов — у 2, ошибка теста — у 1" in warnings[2]
    assert all(f"prepare {LAUNCH} --fresh" in warning for warning in warnings)


def test_page_limit_fails_after_the_first_page(monkeypatch) -> None:
    """Лимит страниц виден по первой странице — остальные не запрашиваются."""
    fake = FakeTestOps(default_launch(LAUNCH))
    fake.install(monkeypatch)
    settings = Settings(endpoint="https://testops.example", token="t", page_size=3, max_pages=2)

    async def fetch() -> None:
        async with AllureTestOpsClient(settings, AllureAuthManager(settings.endpoint, "t")) as client:
            await client.get_all_test_results_for_launch(LAUNCH)

    with pytest.raises(PaginationLimitError) as exc_info:
        asyncio.run(fetch())

    assert exc_info.value.pages_needed == 3
    assert "8 результатов (3 страниц по 3)" in str(exc_info.value)
    assert [path for _, path in fake.requests].count("/api/testresult") == 1
    hint = fetch_error_hint(exc_info.value, settings, Path(".env"))
    assert "ALLURE_MAX_PAGES хотя бы до 3 (сейчас 2)" in hint


class _UnstableTestOps(FakeTestOps):
    """Идущий прогон: страницы сдвигаются, дерево шагов одного падения не отдаётся."""

    def __init__(self) -> None:
        fixture = default_launch(LAUNCH)
        fixture.launch["closed"] = False
        super().__init__(fixture)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/testresult/101/execution":
            self.requests.append((request.method, request.url.path))
            return httpx.Response(503, text="busy")
        return super().handle(request)

    def _page(self, request: httpx.Request) -> httpx.Response:
        response = super()._page(request)
        page = int(request.url.params["page"])
        if page == 0:
            return response
        body = json.loads(response.content)
        # Пока шла выгрузка, в начало добавился результат: TestOps насчитывает на один
        # больше, а вторая страница начинается с последнего результата первой.
        body["totalElements"] += 1
        if page == 1:
            size = int(request.url.params["size"])
            body["content"] = self.fixture.results[size - 1:2 * size - 1]
        return httpx.Response(200, json=body)


def test_prepare_reports_incomplete_fetch(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ALLURE_HTTP_RETRIES", "0")
    fake = _UnstableTestOps()
    fake.install(monkeypatch)

    _, run, out = _prepare(project, capsys, LAUNCH)

    fetch = run["triage"]["fetch"]
    assert fetch == {
        "launch_closed": False,
        "results_reported": 9,
        "results_received": 7,
        "duplicates": 1,
        "execution_failed": 1,
        "detail_failed": 0,
    }
    lines = [line for line in out.splitlines() if line.startswith("Внимание:")]
    assert any("ещё не закрыт" in line for line in lines)
    assert any("насчитал 9 результатов, получено 7" in line for line in lines)
    assert any("дерево шагов — у 1" in line for line in lines)
    assert sum(1 for warning in run["warnings"] if f"prepare {LAUNCH} --fresh" in warning) == 3
