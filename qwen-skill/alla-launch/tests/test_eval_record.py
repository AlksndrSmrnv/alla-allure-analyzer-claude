"""Запись прогона в кассету: тот же триаж при воспроизведении, токена на диске нет."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import skill_fixtures  # noqa: F401  # добавляет scripts/ в sys.path
from skill_fake_testops import APP_LOG, TOKEN, FakeTestOps, LaunchFixture, default_launch

from alla_core.config import Settings
from alla_skill_lib.pipeline import collect_launch
from eval.cassette import load_cassette, replay
from eval.record import record

ENVIRON = {
    "ALLURE_ENDPOINT": "https://testops.example",
    "ALLURE_TOKEN": TOKEN,
    "ALLURE_PAGE_SIZE": "3",
    "ALLURE_PROJECT_ID": "5",
}


def _record(fixture: LaunchFixture, out_dir: Path, **environ: str) -> FakeTestOps:
    fake = FakeTestOps(fixture)
    settings = Settings.load(environ={**ENVIRON, **environ})
    record(fixture.launch["id"], out_dir, settings,
           inner=lambda verify: httpx.MockTransport(fake.handle))
    return fake


def test_recorded_cassette_replays_the_same_triage(tmp_path: Path) -> None:
    fixture = default_launch()
    fake = _record(fixture, tmp_path / "cassette")
    settings = Settings.load(environ=ENVIRON)

    cassette = load_cassette(tmp_path / "cassette")
    with replay(cassette):
        replayed = asyncio.run(collect_launch(777, settings))
    with replay(fixture):
        original = asyncio.run(collect_launch(777, settings))

    assert {(method, path) for method, path in fake.requests} == {("POST", "/api/mcp")}
    # Три страницы склеены, hidden на месте; полные ответы — с содержимым вложений.
    assert [item["id"] for item in cassette.results] == [item["id"] for item in fixture.results]
    assert cassette.details[101]["attachments"]["9001"]["content"] == APP_LOG
    assert replayed.triage == original.triage
    assert replayed.clustering == original.clustering


def test_cassette_keeps_no_token(tmp_path: Path) -> None:
    _record(default_launch(), tmp_path / "cassette")

    files = [path for path in (tmp_path / "cassette").rglob("*") if path.is_file()]
    assert files
    for path in files:
        data = path.read_bytes()
        assert TOKEN.encode() not in data, path
        assert b"api-token" not in data, path


def test_record_under_an_endpoint_prefix(tmp_path: Path) -> None:
    fixture = default_launch()
    fake = FakeTestOps(fixture)

    def behind_proxy(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/testops/api/")
        path = request.url.path.removeprefix("/testops")
        return fake.handle(httpx.Request(request.method, request.url.copy_with(path=path),
                                         headers=request.headers, content=request.content))

    settings = Settings.load(environ={**ENVIRON, "ALLURE_ENDPOINT": "https://testops.example/testops"})
    record(777, tmp_path / "cassette", settings,
           inner=lambda verify: httpx.MockTransport(behind_proxy))
    cassette = load_cassette(tmp_path / "cassette")

    assert cassette.launch == {"id": 777, "name": fixture.launch["name"], "projectId": 5}
    assert [item["id"] for item in cassette.results] == [item["id"] for item in fixture.results]
    assert cassette.details[101]["scenario"] == {"steps": fixture.executions[101]}


def test_repeated_last_page_without_totals_is_not_recorded(tmp_path: Path) -> None:
    """Сервер без итогов не листает дальше и повторяет страницу: в кассете её нет, и
    воспроизведение не видит лишних повторов."""
    fixture = default_launch()
    fake = FakeTestOps(fixture)
    original = fake._find

    def find(arguments: dict[str, object]) -> object:
        if str(arguments.get("aql", "")).startswith("launch"):
            page = min(int(arguments.get("page", 0)), 2)  # после последней — она же
            return original({**arguments, "page": page})["content"]
        return original(arguments)

    fake._find = find  # type: ignore[method-assign]
    settings = Settings.load(environ=ENVIRON)
    record(777, tmp_path / "cassette", settings,
           inner=lambda verify: httpx.MockTransport(fake.handle))

    cassette = load_cassette(tmp_path / "cassette")
    assert [item["id"] for item in cassette.results] == [item["id"] for item in fixture.results]


def test_record_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "cassette").mkdir()
    (tmp_path / "cassette" / "launch.json").write_text("{}", encoding="utf-8")
    fake = FakeTestOps(default_launch())

    with pytest.raises(FileExistsError):
        record(777, tmp_path / "cassette", Settings.load(environ=ENVIRON),
               inner=lambda verify: httpx.MockTransport(fake.handle))
    assert fake.requests == []  # отказ до обращения к TestOps
