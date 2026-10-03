"""Запись прогона в кассету: тот же триаж при воспроизведении, токен и JWT не на диске."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from skill_fixtures import without_libmagic  # noqa: F401
from skill_fake_testops import TOKEN, FakeTestOps, LaunchFixture, default_launch

from alla_core.config import Settings
from alla_skill_lib.pipeline import collect_launch
from eval.cassette import load_cassette, replay
from eval.record import record

ENVIRON = {
    "ALLURE_ENDPOINT": "https://testops.example",
    "ALLURE_TOKEN": TOKEN,
    "ALLURE_PAGE_SIZE": "3",
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

    assert ("POST", "/api/uaa/oauth/token") in fake.requests
    assert cassette.results == fixture.results  # три страницы склеены, hidden на месте
    assert cassette.contents == fixture.contents
    assert replayed.triage == original.triage
    assert replayed.clustering == original.clustering


def test_cassette_keeps_no_token_or_jwt(tmp_path: Path) -> None:
    _record(default_launch(), tmp_path / "cassette")

    files = [path for path in (tmp_path / "cassette").rglob("*") if path.is_file()]
    assert files
    for path in files:
        data = path.read_bytes()
        assert TOKEN.encode() not in data, path
        assert b"jwt-1" not in data, path
        assert b"Bearer" not in data, path


def test_attachment_is_recorded_up_to_the_client_limit(tmp_path: Path) -> None:
    fixture = default_launch()
    fixture.contents[9001] = b"x" * 5000
    _record(fixture, tmp_path / "cassette", ALLURE_LOGS_MAX_ATTACHMENT_BYTES="1024")

    assert len(load_cassette(tmp_path / "cassette").contents[9001]) == 1025


def test_record_under_an_endpoint_prefix(tmp_path: Path) -> None:
    fixture = default_launch()
    fixture.contents[9001] = b"x" * 5000
    fake = FakeTestOps(fixture)

    def behind_proxy(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/testops/api/")
        path = request.url.path.removeprefix("/testops")
        return fake.handle(httpx.Request(request.method, request.url.copy_with(path=path),
                                         headers=request.headers, content=request.content))

    settings = Settings.load(environ={**ENVIRON, "ALLURE_ENDPOINT": "https://testops.example/testops",
                                      "ALLURE_LOGS_MAX_ATTACHMENT_BYTES": "1024"})
    record(777, tmp_path / "cassette", settings,
           inner=lambda verify: httpx.MockTransport(behind_proxy))
    cassette = load_cassette(tmp_path / "cassette")

    assert cassette.launch == fixture.launch
    assert cassette.results == fixture.results
    assert {key: steps for key, steps in cassette.executions.items() if steps} == fixture.executions
    assert len(cassette.contents[9001]) == 1025


def test_record_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "cassette").mkdir()
    (tmp_path / "cassette" / "launch.json").write_text("{}", encoding="utf-8")
    fake = FakeTestOps(default_launch())

    with pytest.raises(FileExistsError):
        record(777, tmp_path / "cassette", Settings.load(environ=ENVIRON),
               inner=lambda verify: httpx.MockTransport(fake.handle))
    assert fake.requests == []  # отказ до обращения к TestOps
