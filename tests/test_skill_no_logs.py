"""Проект без логов: разбор по сообщению, трейсу и ответу сервиса, без требования лога."""

from __future__ import annotations

from pathlib import Path

import pytest

from skill_fixtures import project_fixture, testops_fixture  # noqa: F401
from skill_fake_testops import FakeTestOps, no_logs_launch
from test_skill_flow import _prepare

from alla_skill_lib.sources import load_registry


@pytest.fixture(name="no_logs")
def no_logs_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeTestOps:
    fake = FakeTestOps(no_logs_launch())
    fake.install(monkeypatch)
    return fake


def test_run_without_logs_gives_symptom_tasks_without_log_demands(
    project: Path, no_logs: FakeTestOps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, run, _out = _prepare(project, capsys, launch_id=792)
    clusters = run["clusters"]
    assert len(clusters) == 4  # данные, приложение, окружение, неизвестно — разные проблемы
    tasks = {entry["file_id"]: (run_dir / "clusters" / f"{entry['file_id']}.md")
             .read_text(encoding="utf-8") for entry in clusters}
    for entry in clusters:
        task = " ".join(tasks[entry["file_id"]].split())
        assert not entry.get("log_not_found")
        assert "Лог не обязателен" in task and "Начни с «лога приложения" not in task
    # Ответ сервиса из вложения — кусок HTTP, его цитату скрипт сверяет.
    promo = next(entry for entry in clusters if "422" in str(entry.get("label")))
    registry = load_registry(run_dir / "evidence" / f"{promo['file_id']}.sources.json")
    assert registry is not None
    assert any(record.get("section") == "HTTP" and "SPRING25 has expired" in record["text"]
               for record in registry.values())
