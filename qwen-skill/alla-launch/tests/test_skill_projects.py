"""Проект TestOps прогона: ``ALLURE_PROJECT_ID``, ``prepare --module``/``--project`` и ``check``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, testops_fixture  # noqa: F401
from skill_fake_testops import FIND_TOOL_SCHEMA, FakeTestOps, default_launch

from alla_core.config import Settings
from alla_skill_lib import cli
from alla_skill_lib.projects import ProjectChoice, ProjectNotChosen, choose_project

MODULES = "orders=5,payments/api=7"


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli.main(argv)
    return code, capsys.readouterr().out


def _run_json(out: str) -> dict:
    run_dir = Path(next(line for line in out.splitlines() if line.startswith("Папка разбора:"))
                   .split(":", 1)[1].strip())
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def _settings(raw: str) -> Settings:
    return Settings.load(environ={"ALLURE_ENDPOINT": "https://t.example", "ALLURE_TOKEN": "t",
                                  "ALLURE_PROJECT_ID": raw})


@pytest.mark.parametrize(("raw", "module", "project", "expected"), [
    ("", None, None, ProjectChoice(None)),
    ("", "orders", None, ProjectChoice(None)),  # модулей в настройках нет — имя не нужно
    ("5", None, None, ProjectChoice(5)),
    ("5", "orders", None, ProjectChoice(5)),  # один номер подходит любому модулю
    ("orders=5", None, None, ProjectChoice(5, "orders")),
    (MODULES, "payments/api", None, ProjectChoice(7, "payments/api")),
    (MODULES, "/payments\\api/", None, ProjectChoice(7, "payments/api")),
    (MODULES, "api", None, ProjectChoice(7, "payments/api")),  # последний сегмент пути
    (MODULES, None, 7, ProjectChoice(7, "payments/api")),
    (MODULES, None, 99, ProjectChoice(99)),
    (MODULES, "orders", 7, ProjectChoice(7, "orders")),
    ("orders=5,smoke=5", None, 5, ProjectChoice(5)),  # номер у двух модулей — модуль не назван
])
def test_project_choice(raw: str, module: str | None, project: int | None,
                        expected: ProjectChoice) -> None:
    assert choose_project(_settings(raw), module=module, project=project) == expected


@pytest.mark.parametrize(("module", "unknown"), [(None, None), ("billing", "billing")])
def test_several_projects_need_a_module(module: str | None, unknown: str | None) -> None:
    with pytest.raises(ProjectNotChosen) as exc_info:
        choose_project(_settings(MODULES), module=module)
    assert exc_info.value.unknown == unknown and len(exc_info.value.projects) == 2


def test_one_project_is_sent_and_stored(project: Path, testops: FakeTestOps, capsys) -> None:
    code, out = _run(["prepare", "777", "--project-root", str(project)], capsys)

    assert code == 0, out
    assert testops.calls and all(call.get("projectId") == 5 for call in testops.calls)
    run = _run_json(out)
    assert (run["project_id"], run["project_module"]) == (5, None)


def test_several_projects_ask_for_the_module(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    monkeypatch.setenv("ALLURE_PROJECT_ID", MODULES)

    code, out = _run(["prepare", "777", "--project-root", str(project)], capsys)

    assert code == 1 and out.startswith("STATUS: error"), out
    assert "Спроси пользователя, из какого модуля прогон" in out
    assert "prepare 777 --module orders" in out and "prepare 777 --module payments/api" in out
    assert testops.requests == [] and not (project / "alla-reports").exists()

    code, out = _run(["prepare", "777", "--module", "billing", "--project-root", str(project)],
                     capsys)
    assert code == 1 and "Модуля «billing» нет в ALLURE_PROJECT_ID" in out
    assert "prepare 777 --module orders" in out

    code, out = _run(["prepare", "777", "--module", "orders", "--project-root", str(project)],
                     capsys)
    assert code == 0, out
    assert {call.get("projectId") for call in testops.calls} == {5}
    run = _run_json(out)
    assert (run["project_id"], run["project_module"]) == (5, "orders")

    # Неоконченный разбор продолжается без вопроса о модуле.
    code, out = _run(["prepare", "777", "--project-root", str(project)], capsys)
    assert code == 0 and "Продолжаю неоконченный разбор" in out


def test_launch_of_another_project_is_reported_as_empty(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    monkeypatch.setenv("ALLURE_PROJECT_ID", MODULES)

    code, out = _run(["prepare", "777", "--module", "payments/api", "--project-root",
                      str(project)], capsys)

    assert code == 1 and out.startswith("STATUS: error"), out
    assert "проект TestOps 7, модуль payments/api" in out and "Проверь номер запуска и модуль" in out


def test_without_a_project_the_tool_needs_one(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    monkeypatch.delenv("ALLURE_PROJECT_ID")

    code, out = _run(["prepare", "777", "--project-root", str(project)], capsys)
    assert code == 1 and out.startswith("STATUS: error") and "ALLURE_PROJECT_ID" in out
    assert "orders=5" in out  # подсказка о многомодульном формате

    code, out = _run(["check", "--project-root", str(project)], capsys)
    assert code == 1 and "ALLURE_PROJECT_ID: не задан" in out and "orders=5" in out


def test_prepare_project_option(project: Path, testops: FakeTestOps,
                                monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("ALLURE_PROJECT_ID")

    code, out = _run(["prepare", "777", "--project", "5", "--project-root", str(project)], capsys)
    assert code == 0, out
    assert _run_json(out)["project_id"] == 5

    with pytest.raises(SystemExit):
        cli.main(["prepare", "777", "--project", "x", "--project-root", str(project)])
    captured = capsys.readouterr()
    assert (captured.out + captured.err).startswith("STATUS: error")
    assert "номер проекта" in captured.out + captured.err


def test_check_lists_projects_and_warns(project: Path, testops: FakeTestOps,
                                        monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    (project / "orders").mkdir()
    monkeypatch.setenv("ALLURE_PROJECT_ID", MODULES)

    code, out = _run(["check", "--project-root", str(project)], capsys)
    assert code == 0 and out.startswith("STATUS: ready"), out
    assert "проекты TestOps по модулям — orders → 5, payments/api → 7" in out
    assert "Внимание: модуль «payments/api» из ALLURE_PROJECT_ID — нет папки" in out
    assert "модуль «orders»" not in out

    testops.tools = [{**FIND_TOOL_SCHEMA, "inputSchema": {"type": "object",
                                                          "properties": {"aql": {}}}}]
    code, out = _run(["check", "--project-root", str(project)], capsys)
    assert code == 0 and "нет параметра проекта — ALLURE_PROJECT_ID не используется" in out


def test_wrong_project_setting_stops_prepare(project: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys) -> None:
    FakeTestOps(default_launch()).install(monkeypatch)
    monkeypatch.setenv("ALLURE_PROJECT_ID", "orders")

    code, out = _run(["prepare", "777", "--project-root", str(project)], capsys)
    assert code == 2 and out.startswith("STATUS: error") and "ALLURE_PROJECT_ID" in out
