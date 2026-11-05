"""Проверенная правка (``fix_facts``): «да» на правку + перезапуск → разбор без агента потом."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import skill_fake_maven
from skill_fake_testops import FakeTestOps, default_launch
from skill_fixtures import (  # noqa: F401
    nested_project_fixture,
    testops_fixture,
)
from test_skill_flow import MARKDOWN_ANALYSIS, _finish, _prepare, _run
from test_skill_rerun import FAILED_500, ORDER, _applied, _problem_of, _wait_done

from alla_skill_lib import fix_facts
from alla_skill_lib.kb import ProjectKB

SOURCE = "autotests/src/test/java/ru/company/orders/OrderTest.java"


def _checked_fix(project: Path, capsys: pytest.CaptureFixture[str],
                 outcomes: dict[str, list[str]] | None = None) -> tuple[Path, str, str]:
    """Правка применена, тесты проблемы перезапущены; итог ``rerun_done``."""
    skill_fake_maven.install(project / "autotests", outcomes or {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]})
    run_dir, order, out = _applied(project, capsys, SOURCE)
    command = out.splitlines()[-1].split("выполни: ", 1)[1].split(" — дальше", 1)[0]
    done = _run(["rerun", *command.split(" rerun ", 1)[1].split()], capsys)[1]
    done = _wait_done(run_dir, capsys) if done.startswith("STATUS: running") else done
    assert done.startswith("STATUS: rerun_done"), done
    return run_dir, order, done


def _facts(project: Path) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((project / "alla-kb" / "fixes").glob("*.json"))]


def _next_launch(project: Path, capsys: pytest.CaptureFixture[str],
                 monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict, str, str]:
    FakeTestOps(default_launch(778)).install(monkeypatch)
    run_dir, run, _ = _prepare(project, capsys, launch_id=778)
    order = f"{int(_problem_of(run, 101)):02d}"
    login = f"{int(_problem_of(run, 103)):02d}"
    return run_dir, run, order, login


def test_checked_fix_is_remembered_and_the_next_run_needs_no_agent(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, order, done = _checked_fix(nested_project, capsys)

    told = done.split("===КОНЕЦ===", 1)[1]
    assert (f"Запомнена проверенная правка проблемы {int(order)} ({SOURCE}:6): в следующих "
            "прогонах эта же ошибка этих тестов разберётся без агента.") in told
    assert "напомни закоммитить папку базы знаний alla-kb" in told
    [fact] = _facts(nested_project)
    assert fact["file"] == SOURCE and fact["line"] == 6
    assert fact["tests"] == [f"{ORDER}.createOrder", f"{ORDER}.updateOrder"]
    assert fact["after"] == ["        assertEquals(201, api.create().status());"]
    assert "cause" not in fact and "why" not in fact  # слов разбора модели в факте нет
    # Факт — не запись базы знаний: точного узнавания из него нет.
    assert ProjectKB(nested_project / "alla-kb").load() == ([], [])
    assert "Папка `fixes/`" in (nested_project / "alla-kb" / "README.md").read_text(encoding="utf-8")

    # Следующий прогон, правка уже в коде (CI запускал код без неё): разбор пишет скрипт.
    run_dir, run, order, login = _next_launch(nested_project, capsys, monkeypatch)
    entry = next(item for item in run["clusters"] if item["file_id"] == order)
    assert entry["fix_auto"]["state"] == "applied" and entry["fix_auto"]["id"] == fact["id"]
    first = _run(["next", str(run_dir)], capsys)[1]
    assert first.startswith("STATUS: analyze") and f"clusters/{login}.md" in first
    assert "1 по базе знаний" in first
    analysis = (run_dir / "analyses" / f"{order}.md").read_text(encoding="utf-8")
    assert analysis.startswith(f"ЧТО СЛОМАЛОСЬ: Эту ошибку уже исправляла правка автотеста "
                               f"{SOURCE}:6 (проверена перезапуском ")
    assert "Правка уже есть в коде проекта на этой машине — CI запускал код без неё." in analysis
    assert f"факт fix:{fact['id']}" in analysis

    out = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})

    assert "Эту ошибку уже исправляла правка автотеста" in out
    assert f"apply {int(order)}" not in out  # правка уже стоит — применять нечего
    kb_question = next((line for line in out.splitlines() if "Какие разборы верны" in line), "")
    assert f"Проблемы: {int(login)}." in kb_question  # о разобранной по факту не спрашиваем
    assert "Похожи на нестабильные" not in out


def test_fix_missing_from_the_code_is_offered_for_apply_without_the_agent(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _checked_fix(nested_project, capsys)
    source = nested_project / SOURCE
    source.write_text(source.read_text(encoding="utf-8").replace("(201,", "(200,"),
                      encoding="utf-8")  # правку откатили (другая ветка)

    run_dir, run, order, login = _next_launch(nested_project, capsys, monkeypatch)
    out = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})

    analysis = (run_dir / "analyses" / f"{order}.md").read_text(encoding="utf-8")
    assert "В коде проекта на этой машине этой правки нет." in analysis
    assert f"- Правка автотеста для проблемы {int(order)}: покажи пользователю diff" in out
    proposal = (run_dir / "proposals" / f"{order}.md").read_text(encoding="utf-8")
    assert proposal.startswith(f"РЕШЕНИЕ: исправить\nФАЙЛ: {SOURCE}:6\n")
    diff = _run(["apply", order, "--run", str(run_dir)], capsys)[1]
    assert diff.startswith("STATUS: diff") and "+        assertEquals(201" in diff


def test_rejected_fix_goes_back_to_the_agent(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _checked_fix(nested_project, capsys)
    [fact] = _facts(nested_project)
    run_dir, _, order, login = _next_launch(nested_project, capsys, monkeypatch)
    _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})

    saved = _run(["reject", order, f"fix:{fact['id']}", "--run", str(run_dir)], capsys)[1]

    assert saved.startswith(f"STATUS: saved\nПроверенная правка fix:{fact['id']} больше не будет")
    assert _facts(nested_project) == []
    again = _run(["next", str(run_dir)], capsys)[1]
    assert again.startswith("STATUS: analyze") and f"clusters/{order}.md" in again
    assert not (run_dir / "analyses" / f"{order}.md").exists()
    assert not (run_dir / "proposals" / f"{order}.md").exists()
    missing = _run(["reject", order, "fix:000000000000", "--run", str(run_dir)], capsys)[1]
    assert missing.startswith("STATUS: error")


@pytest.mark.parametrize("outcomes", [
    {f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": [FAILED_500]},  # не все прошли
    {f"{ORDER}#createOrder": [FAILED_500, "passed"], f"{ORDER}#updateOrder": ["passed"]},  # не с 1-го
])
def test_partly_checked_fix_is_not_remembered(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    outcomes: dict[str, list[str]],
) -> None:
    _, _, done = _checked_fix(nested_project, capsys, outcomes)

    assert "Запомнена проверенная правка" not in done
    assert _facts(nested_project) == []


def test_fact_needs_the_same_log_the_same_tests_and_no_exact_record(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    _checked_fix(nested_project, capsys)
    [data] = _facts(nested_project)
    fact = fix_facts.FixFact.from_json(data)
    assert fact is not None
    entry = {"kb_dir": str(nested_project / "alla-kb"), "signature": fact.signature,
             "log_digest": fact.log_digest, "kb": []}
    tests = list(fact.tests)

    assert fix_facts.match(entry, tests, nested_project)["state"] == "applied"  # type: ignore[index]
    assert fix_facts.match({**entry, "log_digest": "other"}, tests, nested_project) is None
    assert fix_facts.match(entry, [*tests, f"{ORDER}.other"], nested_project) is None
    assert fix_facts.match({**entry, "kb": [{"id": "r", "origin": "exact"}]}, tests,
                           nested_project) is None
    late = date.fromisoformat(fact.date) + timedelta(days=fix_facts.MAX_AGE_DAYS + 1)
    assert fix_facts.match(entry, tests, nested_project, today=late) is None
    # Место правки пропало из файла — факт не подходит.
    source = nested_project / SOURCE
    source.write_text(source.read_text(encoding="utf-8").replace("api.create()", "api.make()"),
                      encoding="utf-8")
    assert fix_facts.match(entry, tests, nested_project) is None


def test_timeout_growth_is_not_remembered(tmp_path: Path) -> None:
    """Рост таймаута лечит симптом: после такой правки тест пройдёт и при другой причине."""
    from alla_skill_lib.proposals import timeouts_increased

    assert timeouts_increased(["wait(Duration.ofSeconds(5));"], ["wait(Duration.ofSeconds(30));"])
    assert not timeouts_increased(["assertEquals(200, code);"], ["assertEquals(201, code);"])

