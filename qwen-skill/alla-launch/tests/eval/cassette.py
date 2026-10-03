"""Кассета — сохранённый прогон TestOps, который воспроизводится без сети.

Каталог::

    launch.json                      ответ GET /api/launch/{id}
    results.json                     все результаты прогона, включая hidden
    executions/<result_id>.json      GET /api/testresult/{id}/execution
    details/<result_id>.json         GET /api/testresult/{id}
    attachments/<result_id>.json     метаданные вложений результата
    contents/<attachment_id>         байты вложения

Поля — как у ``LaunchFixture`` (``tests/skill_fake_testops.py``); воспроизведение — через
тот же ``FakeTestOps`` на ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from skill_fake_testops import FakeTestOps, LaunchFixture


def _dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_cassette(fixture: LaunchFixture, directory: Path) -> None:
    """Записать прогон в новый или пустой каталог.

    Непустой каталог не перезаписывается: ответы прошлой записи, которых нет в новой
    (execution, вложение), смешались бы с ней и подменили бы актуальные данные.
    """
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError(
            f"Каталог кассеты {directory} не пуст: укажите новый каталог или удалите старую "
            "запись целиком."
        )
    directory.mkdir(parents=True, exist_ok=True)
    _dump(directory / "launch.json", fixture.launch)
    _dump(directory / "results.json", fixture.results)
    for name, items in (
        ("executions", fixture.executions),
        ("details", fixture.details),
        ("attachments", fixture.attachments),
    ):
        for result_id, data in items.items():
            _dump(directory / name / f"{result_id}.json", data)
    for attachment_id, content in fixture.contents.items():
        path = directory / "contents" / str(attachment_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _by_id(directory: Path) -> dict[int, Any]:
    if not directory.is_dir():
        return {}
    return {int(path.stem): _load(path) for path in sorted(directory.glob("*.json"))}


def load_cassette(directory: Path) -> LaunchFixture:
    """Прочитать каталог кассеты в ``LaunchFixture``."""
    contents_dir = directory / "contents"
    contents = (
        {int(path.name): path.read_bytes() for path in sorted(contents_dir.iterdir())}
        if contents_dir.is_dir() else {}
    )
    return LaunchFixture(
        launch=_load(directory / "launch.json"),
        results=_load(directory / "results.json"),
        executions=_by_id(directory / "executions"),
        details=_by_id(directory / "details"),
        attachments=_by_id(directory / "attachments"),
        contents=contents,
    )


@contextmanager
def replay(fixture: LaunchFixture) -> Generator[FakeTestOps]:
    """Подменить ``httpx.AsyncClient`` фейковым TestOps на время блока (сеть не нужна)."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        fake = FakeTestOps(fixture)
        fake.install(monkeypatch)
        yield fake
