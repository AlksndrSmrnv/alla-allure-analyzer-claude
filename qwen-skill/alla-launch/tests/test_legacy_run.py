"""Старый разбор (``tests/fixtures/legacy_run_v1``) открывается и доводится до отчёта."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, without_libmagic  # noqa: F401
from test_skill_flow import MARKDOWN_ANALYSIS

from alla_skill_lib import cli, workspace
from eval.legacy import restore_legacy_run


def _next(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert cli.main(["next", str(run_dir)]) == 0
    return capsys.readouterr().out


def test_legacy_run_resumes_and_keeps_accepted_analysis(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = restore_legacy_run(project, workspace.SKILL_DIR, workspace.ENTRYPOINT)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["schema"] == 2 and "task_format" not in run["clusters"][0]

    out = _next(run_dir, capsys)
    # Принятый разбор 01 не уходит в fix; неверный 03 — та же попытка из state.json.
    assert out.startswith("STATUS: fix")
    assert "Кластер 3 из 3" in out and "попытка 1 из 3" in out
    assert "Файл не изменился с прошлого вызова next" in out
    assert str(run_dir / "analyses" / "03.md") in out

    (run_dir / "analyses" / "03.md").write_text(MARKDOWN_ANALYSIS, encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: summary")

    (run_dir / "summary.md").write_text(
        "Упало 4 теста, выявлено 3 проблемы. Главная — NPE в OrderService.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done")
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "OrderService.create падает на пустом customer" in report
    assert "auth-service:8080 не принимает соединения" in report
    assert "[silent](https://testops.example/launch/777/testresult/108)" in report
