"""Журнал сеанса Qwen: запись сеанса в ``state.json``, окно разбора, субагенты, правила."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from skill_fake_testops import FakeTestOps
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from skill_journal import Clock, Journal, qwen_dirs, save_main, save_subagent
from test_skill_flow import _next, _prepare

from alla_skill_lib.session_log import (
    MAX_SESSIONS,
    load_session,
    note_session,
    sanitize_cwd,
    skill_status,
    skill_subcommand,
)
from alla_skill_lib.session_rules import RULES, RuleContext

REPORT = "## Разбор прогона #777\nПроблема 1 — сервер вернул 500.\nПолный разбор: report.md"


def _done_output(run: Path) -> str:
    return f"STATUS: done\nПрогон #777 · папка {run}\n===ОТЧЁТ===\n{REPORT}\n===КОНЕЦ==="


def _run_dir(project: Path) -> Path:
    run = project / "alla-reports" / "777-20261010-120000"
    (run / "clusters").mkdir(parents=True)
    (run / "run.json").write_text("{}", encoding="utf-8")
    return run


def _state(session: str, project_dir: Path) -> dict:
    return {"sessions": [session], "qwen_project_dir": str(project_dir)}


# --- запись сеанса ------------------------------------------------------------------------


def test_note_session_records_session_and_project_dir() -> None:
    state: dict = {}
    assert not note_session(state, {})
    assert state == {}

    env = {"QWEN_CODE_SESSION_ID": "s1", "QWEN_CODE_PROJECT_DIR": "/q/p"}
    assert note_session(state, env)
    assert state == {"sessions": ["s1"], "qwen_project_dir": "/q/p"}
    assert not note_session(state, env)  # тот же сеанс — без записи

    assert note_session(state, {"QWEN_CODE_SESSION_ID": "s2"})
    assert state["sessions"] == ["s1", "s2"]


def test_note_session_keeps_last_sessions() -> None:
    state: dict = {"sessions": [f"s{i}" for i in range(MAX_SESSIONS)]}
    note_session(state, {"QWEN_CODE_SESSION_ID": "new"})
    assert len(state["sessions"]) == MAX_SESSIONS and state["sessions"][-1] == "new"


def test_sanitize_cwd_matches_qwen() -> None:
    assert sanitize_cwd("/home/u/my.project_1") == "-home-u-my-project-1"


def test_prepare_and_next_remember_qwen_session(
    project: Path, testops: FakeTestOps, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWEN_CODE_SESSION_ID", "first")
    monkeypatch.setenv("QWEN_CODE_PROJECT_DIR", "/qwen/projects/p")
    run_dir, _, _ = _prepare(project, capsys)
    monkeypatch.setenv("QWEN_CODE_SESSION_ID", "second")
    _next(run_dir, capsys)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["sessions"] == ["first", "second"]
    assert state["qwen_project_dir"] == "/qwen/projects/p"


def test_without_qwen_session_state_has_no_sessions(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, _, _ = _prepare(project, capsys)
    state_file = run_dir / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    assert "sessions" not in state


# --- окно разбора -------------------------------------------------------------------------


def test_window_is_from_first_command_of_run_to_final_answer(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run = _run_dir(project)
    other = project / "alla-reports" / "555-20261009-100000"
    qdir = qwen_dirs(tmp_path, project)
    j = Journal("sess-1", project)
    j.user("разбери прогон 555")
    j.skill("prepare 555", f"STATUS: analyze\nПапка разбора: {other}")  # другой разбор
    j.user("разбери прогон 777")
    j.text("думаю", thought=True)
    j.skill("prepare 777", f"STATUS: analyze\nПапка разбора: {run}")
    j.call("read_file", {"file_path": str(run / "clusters" / "01.md")}, "# Кластер 1")
    j.call("write_file", {"file_path": str(run / "analyses" / "01.md"), "content": "x"}, "ok")
    j.skill(f"next {run}", _done_output(run))
    j.text(REPORT + "\n\n")
    j.text("Проблемы скилла:\n1. verify принял пустой файл\n   Где: verify\n")
    j.user("проверь разбор")
    j.skill(f"review --run {run}", f"STATUS: reviewed\nпапка {run}")
    save_main(j, qdir)

    log = load_session(run, project, _state("sess-1", qdir))

    assert log.note == "" and log.reached_done
    assert [skill_subcommand(c) for c in log.calls if c.name == "run_shell_command"] == ["prepare", "next"]
    assert [c.index for c in log.calls] == [0, 1, 2, 3]
    assert REPORT in log.final and "думаю" not in log.final
    assert log.skill_problems == ["verify принял пустой файл"]
    assert skill_status(log.calls[0]) == "analyze" and skill_status(log.calls[-1]) == "done"
    assert log.model == "qwen/qwen3.8-flash" and log.version == "0.25.0"
    # ход «разбери прогон 777» целиком: от реплики до последнего текста ответа (у вызова две
    # записи: вызов и результат); токены — размышление, четыре вызова и два текста; ход с
    # прогоном 555 и ход проверки в окно не входят
    assert log.turns == 1
    assert log.duration_seconds == 11
    assert log.usage.requests == 7 and log.usage.total == 4 * 110 + 3 * 115


def test_shell_output_without_display_is_unwrapped(tmp_path: Path) -> None:
    """Старый или неполный ``resultDisplay``: вывод берётся из ``Output:`` журнала."""
    project = tmp_path / "project"
    run = _run_dir(project)
    qdir = qwen_dirs(tmp_path, project)
    j = Journal("sess-2", project)
    j.skill(f"next {run}", _done_output(run))
    for record in j.records:
        if record["type"] == "tool_result":
            record["toolCallResult"]["resultDisplay"] = ""
    save_main(j, qdir)

    log = load_session(run, project, _state("sess-2", qdir))
    assert log.reached_done
    assert log.calls[0].result.startswith("STATUS: done") and "Exit Code" not in log.calls[0].result


def test_subagents_inside_window_are_included(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run = _run_dir(project)
    qdir = qwen_dirs(tmp_path, project)
    clock = Clock()
    early = Journal("early", project, clock)
    early.call("read_file", {"file_path": "/etc/hosts"}, "чужой субагент до разбора")
    main = Journal("sess-3", project, clock)
    main.skill("prepare 777", f"STATUS: analyze_batch\nПапка разбора: {run}")
    main_agent_at = len(main.records)
    sub = Journal("sub", project, clock)
    sub.call("read_file", {"file_path": str(run / "batches" / "1.md")}, "пакет")
    sub.skill(f"verify 01 --run {run}", "STATUS: ok")
    sub.api_error()
    main.call("agent", {"subagent_type": "alla-batch", "prompt": "пакет 1"},
              "Готово.\nПроблема скилла: verify не видит файл\n")
    main.skill(f"next {run}", _done_output(run))
    main.text(REPORT)
    save_main(main, qdir)
    save_subagent(early, qdir, "sess-3", "alla-batch-0")
    save_subagent(sub, qdir, "sess-3", "alla-batch-1")
    assert main_agent_at == 2

    log = load_session(run, project, _state("sess-3", qdir))

    assert [c.subagent for c in log.calls] == [False, True, True, False, False]
    assert log.subagents == 1 and log.api_errors == 1
    assert log.skill_problems == ["verify не видит файл"]
    assert all("/etc/hosts" not in json.dumps(c.input) for c in log.calls)


def test_missing_journal_is_explained(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run = _run_dir(project)
    assert "журнал неизвестен" in load_session(run, project, {}).note
    log = load_session(run, project, _state("nope", tmp_path / "none"),
                       {"QWEN_HOME": str(tmp_path / "qwen-home")})
    assert log.note.startswith("журнал сеанса Qwen не найден") and not log.calls


def test_journal_found_by_qwen_home_without_remembered_dir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run = _run_dir(project)
    qdir = qwen_dirs(tmp_path, project)
    j = Journal("sess-4", project)
    j.skill("prepare 777", f"STATUS: analyze\nПапка разбора: {run}")
    save_main(j, qdir)

    log = load_session(run, project, {"sessions": ["sess-4"]}, {"QWEN_HOME": str(tmp_path / "qwen-home")})
    assert len(log.calls) == 1 and not log.reached_done


def test_journal_without_run_commands(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run = _run_dir(project)
    qdir = qwen_dirs(tmp_path, project)
    j = Journal("sess-5", project)
    j.user("привет")
    j.text("привет")
    save_main(j, qdir)
    assert load_session(run, project, _state("sess-5", qdir)).note == "в журнале нет команд этого разбора"


def test_unfinished_run_keeps_its_last_actions(tmp_path: Path) -> None:
    """Без done ход разбора берётся целиком: упавший next без папки в выводе и чтение после
    него — тоже разбор (их нарушения и токены не теряются)."""
    project = tmp_path / "project"
    run = _run_dir(project)
    qdir = qwen_dirs(tmp_path, project)
    j = Journal("sess-7", project)
    j.user("разбери прогон 777")
    j.skill("prepare 777", f"STATUS: analyze\nПапка разбора: {run}")
    j.skill(f"next {run}", "Traceback (most recent call last):\nKeyError: 'clusters'")
    j.call("glob", {"pattern": "**/*.java"}, "Found 3 files")
    j.text("Скрипт упал, остановился.")
    save_main(j, qdir)

    log = load_session(run, project, _state("sess-7", qdir))

    assert not log.reached_done and log.final == ""
    assert [c.name for c in log.calls] == ["run_shell_command", "run_shell_command", "glob"]
    assert skill_status(log.calls[1]) is None
    assert log.usage.requests == 4


def test_other_work_between_sessions_is_not_the_run(tmp_path: Path) -> None:
    """Разбор прервали, занялись другим и продолжили в новом сеансе: посторонний ход не
    проверяется и не оплачивается как разбор, время — сумма ходов разбора."""
    project = tmp_path / "project"
    run = _run_dir(project)
    qdir = qwen_dirs(tmp_path, project)
    clock = Clock()
    first = Journal("s-a", project, clock)
    first.user("разбери прогон 777")
    first.skill("prepare 777", f"STATUS: analyze\nПапка разбора: {run}")  # ход: 2 с до результата
    first.user("посмотри README")  # другая работа в том же сеансе
    first.call("run_shell_command", {"command": "cat README.md"}, "…", tokens=5000)
    clock.now += timedelta(hours=2)
    second = Journal("s-b", project, clock)
    second.user("продолжи разбор 777")
    second.skill(f"next {run}", _done_output(run))  # ход: 3 с вместе с ответом
    second.text(REPORT)
    save_main(first, qdir)
    save_main(second, qdir)

    log = load_session(run, project, {"sessions": ["s-a", "s-b"], "qwen_project_dir": str(qdir)})

    assert log.turns == 2 and log.reached_done
    assert [skill_subcommand(c) for c in log.calls] == ["prepare", "next"]
    assert log.usage.total == 2 * 110 + 115  # два вызова и ответ; 5000 постороннего хода — нет
    assert log.duration_seconds == 2 + 3
    assert REPORT in log.final


def test_turn_with_only_a_crashed_next_belongs_to_the_run(tmp_path: Path) -> None:
    """«Продолжи» и единственный ``next --run DIR``, упавший без папки в выводе: ход — разбор
    по папке в аргументах команды."""
    project = tmp_path / "project"
    run = _run_dir(project)
    qdir = qwen_dirs(tmp_path, project)
    j = Journal("sess-8", project)
    j.user("разбери прогон 777")
    j.skill("prepare 777", f"STATUS: analyze\nПапка разбора: {run}")
    j.user("продолжи")
    j.skill(f"next --run {run}", "Traceback (most recent call last):\nKeyError: 'clusters'")
    j.call("read_file", {"file_path": str(project / "missing.txt")}, "not found", error=True)
    j.user("а что с погодой?")  # посторонний ход: без папки разбора
    j.skill("check", "STATUS: ready")
    save_main(j, qdir)

    log = load_session(run, project, _state("sess-8", qdir))

    assert log.turns == 2
    assert [c.name for c in log.calls] == ["run_shell_command", "run_shell_command", "read_file"]
    assert skill_status(log.calls[1]) is None and log.calls[2].is_error


def test_review_inside_the_run_turn_is_left_out(tmp_path: Path) -> None:
    """«Закончи разбор и проверь его»: next и review в одном ходе. Идущий review (ещё без
    результата) и всё после него — не разбор."""
    project = tmp_path / "project"
    run = _run_dir(project)
    qdir = qwen_dirs(tmp_path, project)
    j = Journal("sess-9", project)
    j.user("закончи разбор 777 и проверь его")
    j.skill(f"next {run}", _done_output(run))
    j.text(REPORT)
    j.skill(f"review --run {run}", "", pending=True)
    save_main(j, qdir)

    log = load_session(run, project, _state("sess-9", qdir))

    assert [skill_subcommand(c) for c in log.calls] == ["next"]
    assert log.reached_done and REPORT in log.final
    # затраты — вызов next и ответ с отчётом; запрос, вызвавший review, — уже проверка
    assert log.usage.requests == 2 and log.usage.total == 110 + 115


# --- правила по журналу -------------------------------------------------------------------


def test_rules_run_over_journal_calls(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run = _run_dir(project)
    (project / "src").mkdir()
    qdir = qwen_dirs(tmp_path, project)
    j = Journal("sess-6", project)
    j.skill("prepare 777", f"STATUS: analyze\nПапка разбора: {run}")
    j.call("glob", {"pattern": "**/*.java"}, "Found 3 files")
    j.call("run_shell_command", {"command": "grep -r Order src | head"}, "...")
    j.skill(f"next {run}", _done_output(run))
    j.text("Отчёт готов, вот главное: сервер вернул 500.")  # пересказ вместо отчёта
    save_main(j, qdir)

    log = load_session(run, project, _state("sess-6", qdir))
    ctx = RuleContext(project, tmp_path, log.calls, log.final, [run])
    results = {name: check(ctx)["status"] for name, check in RULES.items()}
    assert results["no_code_search"] == "fail"
    assert results["shell_only_skill_commands"] == "fail"
    assert results["report_verbatim"] == "fail"
    assert results["allowed_writes"] == "pass"
