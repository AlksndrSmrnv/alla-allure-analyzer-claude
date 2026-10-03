"""Кассета прогона: сохранение, чтение и воспроизведение без сети."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, without_libmagic  # noqa: F401
from skill_fake_testops import default_launch

from alla_skill_lib import cli
from eval.cassette import load_cassette, replay, save_cassette


def test_cassette_round_trip(tmp_path: Path) -> None:
    fixture = default_launch()
    save_cassette(fixture, tmp_path / "cassette")

    loaded = load_cassette(tmp_path / "cassette")

    assert asdict(loaded) == asdict(fixture)
    assert (tmp_path / "cassette" / "contents" / "9001").read_bytes() == fixture.contents[9001]


def test_cassette_without_optional_folders(tmp_path: Path) -> None:
    fixture = default_launch()
    fixture.executions, fixture.details, fixture.attachments, fixture.contents = {}, {}, {}, {}
    save_cassette(fixture, tmp_path)

    assert asdict(load_cassette(tmp_path)) == asdict(fixture)


def test_replayed_cassette_prepares_like_the_fixture(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save_cassette(default_launch(), tmp_path / "cassette")

    with replay(load_cassette(tmp_path / "cassette")) as fake:
        assert cli.main(["prepare", "777", "--project-root", str(project)]) == 0
    out = capsys.readouterr().out
    run_dir = Path(next(line for line in out.splitlines() if line.startswith("Папка разбора:"))
                   .split(":", 1)[1].strip())
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    assert ("GET", "/api/testresult/attachment/9001/content") in fake.requests
    assert run["counts"] == {
        "total": 7, "passed": 1, "failed": 4, "broken": 1, "skipped": 1,
        "unknown": 0, "muted_failures": 1, "active_failures": 4,
    }


def test_cassette_is_never_written_over_an_old_one(tmp_path: Path) -> None:
    save_cassette(default_launch(), tmp_path / "cassette")

    with pytest.raises(FileExistsError, match="не пуст"):
        save_cassette(default_launch(), tmp_path / "cassette")
    (tmp_path / "empty").mkdir()
    save_cassette(default_launch(), tmp_path / "empty")  # пустой каталог — можно
