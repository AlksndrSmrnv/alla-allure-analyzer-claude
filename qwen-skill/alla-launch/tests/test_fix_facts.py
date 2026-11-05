"""Проверенная правка (``fix_facts``): «да» на правку + перезапуск → разбор без агента потом."""

from __future__ import annotations

import json
import shutil
import subprocess
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


def _git(project: Path) -> None:
    """Настоящий git: факт пишется, только если файл до apply был как в HEAD."""
    shutil.rmtree(project / ".git", ignore_errors=True)
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)


def _checked_fix(project: Path, capsys: pytest.CaptureFixture[str],
                 outcomes: dict[str, list[str]] | None = None,
                 before_apply: str = "") -> tuple[Path, str, str]:
    """Правка применена, тесты проблемы перезапущены; итог ``rerun_done``."""
    skill_fake_maven.install(project / "autotests", outcomes or {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]})
    _git(project)
    if before_apply:
        source = project / SOURCE
        source.write_text(source.read_text(encoding="utf-8") + before_apply, encoding="utf-8")
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
    assert analysis.startswith(f"ЧТО СЛОМАЛОСЬ: Эту ошибку уже исправляла проверенная правка "
                               f"fix:{fact['id']} (OrderTest.java:6): после неё эти тесты прошли "
                               "с первого перезапуска.")
    assert f"Правка — {SOURCE}:6." in analysis and "уже есть — дождаться" in analysis

    out = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})

    brief = out.split("===ОТЧЁТ===", 1)[1].split("===КОНЕЦ===", 1)[0]
    assert "Эту ошибку уже исправляла проверенная правка" in brief
    assert f"fix:{fact['id']}" in brief  # id для reject N fix:<id> — в кратком разборе
    assert f"apply {int(order)}" not in out  # правка уже стоит — применять нечего
    kb_question = next((line for line in out.splitlines() if "Какие разборы верны" in line), "")
    assert f"Проблемы: {int(login)}." in kb_question  # о разобранной по факту не спрашиваем
    assert "Похожи на нестабильные" not in out


def test_fix_missing_from_the_code_is_offered_for_apply_without_the_agent(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alla_skill_lib import cli

    _checked_fix(nested_project, capsys)
    source = nested_project / SOURCE
    source.write_text(source.read_text(encoding="utf-8").replace("(201,", "(200,"),
                      encoding="utf-8")  # правку откатили (другая ветка)
    # Предложение по факту пишет скрипт — лимит предложений модели оно не занимает (ревью Grok).
    monkeypatch.setattr(cli, "MAX_PROPOSALS", 0)

    run_dir, run, order, login = _next_launch(nested_project, capsys, monkeypatch)
    out = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})

    analysis = (run_dir / "analyses" / f"{order}.md").read_text(encoding="utf-8")
    assert "в коде нет — применить её" in analysis
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



def test_other_error_of_another_member_is_left_to_the_model() -> None:
    """Сигнатура и лог кластера — представителя: другой ответ сервиса у второго теста не
    получает чужую правку (ревью Codex)."""
    from test_skill_kb_auto import _cluster, _http

    from alla_skill_lib.signature import cluster_signature

    error = "ru.bank.payments.TransferRejectedException: transfer rejected"
    same, same_tests = _cluster(error, error, logs=(_http("account is closed", "rq-1"),
                                                    _http("account is closed", "rq-2")))
    mixed, mixed_tests = _cluster(error, error, logs=(_http("account is closed", "rq-3"),
                                                      _http("insufficient funds", "rq-4")))
    signature = cluster_signature(same, same_tests)
    assert cluster_signature(mixed, mixed_tests) == signature
    assert fix_facts._members_same(same, same_tests, signature)
    assert not fix_facts._members_same(mixed, mixed_tests, signature)


def test_report_is_refreshed_after_applying_and_rechecking_the_fix(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Применили предложенную проверенную правку и перезапустили: разбор не меняется (сводка
    цела), отчёт пересобран с итогом, правка в нём применена (ревью Codex)."""
    _checked_fix(nested_project, capsys)
    source = nested_project / SOURCE
    source.write_text(source.read_text(encoding="utf-8").replace("(201,", "(200,"),
                      encoding="utf-8")
    run_dir, _, order, login = _next_launch(nested_project, capsys, monkeypatch)
    _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    diff = _run(["apply", order, "--run", str(run_dir)], capsys)[1]
    applied = _run(["apply", order, "--run", str(run_dir), "--yes", "--diff",
                    diff.split("--yes --diff ")[1].split()[0]], capsys)[1]
    assert applied.startswith("STATUS: applied")
    analysis = (run_dir / "analyses" / f"{order}.md").read_text(encoding="utf-8")
    command = applied.splitlines()[-1].split("выполни: ", 1)[1].split(" — дальше", 1)[0]
    done = _run(["rerun", *command.split(" rerun ", 1)[1].split()], capsys)[1]
    done = _wait_done(run_dir, capsys) if done.startswith("STATUS: running") else done

    assert done.startswith("STATUS: rerun_done")
    assert (run_dir / "analyses" / f"{order}.md").read_text(encoding="utf-8") == analysis
    assert (run_dir / "summary.md").is_file()
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Перезапуск локально" in report
    assert _run(["next", str(run_dir)], capsys)[1].startswith("STATUS: done")


def test_fix_with_other_edits_is_not_remembered(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    """Тест мог пройти благодаря чужой правке: файл не тот, что оставил apply (ревью Codex)."""
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]})
    _git(nested_project)
    run_dir, order, out = _applied(nested_project, capsys, SOURCE)
    source = nested_project / SOURCE
    source.write_text(source.read_text(encoding="utf-8") + "// ещё правка\n", encoding="utf-8")
    command = out.splitlines()[-1].split("выполни: ", 1)[1].split(" — дальше", 1)[0]
    done = _run(["rerun", *command.split(" rerun ", 1)[1].split()], capsys)[1]
    done = _wait_done(run_dir, capsys) if done.startswith("STATUS: running") else done

    assert done.startswith("STATUS: rerun_done"), done
    assert "Запомнена проверенная правка" not in done and _facts(nested_project) == []


def test_test_edited_during_the_rerun_is_not_a_checked_fix() -> None:
    passed = {"outcome": "passed", "attempt": 1, "changed": fix_facts.FIX_CHANGED}
    assert fix_facts._checked(passed)
    assert not fix_facts._checked({**passed, "edited": True})  # воркер: исходник изменился
    assert not fix_facts._checked({**passed, "surefire_flaky": True})


def test_manual_edit_before_apply_is_not_remembered(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    """В файле до apply уже была ручная правка: её факт не повторит (ревью Codex)."""
    _, _, done = _checked_fix(nested_project, capsys, before_apply="// ручная правка\n")

    assert "Запомнена проверенная правка" not in done and _facts(nested_project) == []


def test_fact_is_bound_to_the_fix_the_rerun_ran(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    """Итог прочитан после того, как правку откатили и применили другую: та не проверена."""
    from test_skill_flow import PROPOSAL

    run_dir, order, _ = _checked_fix(nested_project, capsys)
    for path in (nested_project / "alla-kb" / "fixes").glob("*.json"):
        path.unlink()
    (run_dir / "rerun" / "learned.json").unlink()  # итог ещё не показывали
    assert _run(["revert", order, "--run", str(run_dir)], capsys)[1].startswith("STATUS: reverted")
    plain = "src/test/java/ru/company/orders/OrderTest.java"
    (run_dir / "proposals" / f"{order}.md").write_text(
        PROPOSAL.replace(plain, SOURCE).replace("(201,", "(202,"), encoding="utf-8")
    diff = _run(["apply", order, "--run", str(run_dir)], capsys)[1]
    assert _run(["apply", order, "--run", str(run_dir), "--yes", "--diff",
                 diff.split("--yes --diff ")[1].split()[0]], capsys)[1].startswith("STATUS: applied")

    shown = _run(["rerun", "--wait", "--run", str(run_dir)], capsys)[1]

    assert shown.startswith("STATUS: rerun_done") and "Запомнена проверенная правка" not in shown
    assert _facts(nested_project) == []


def test_engineer_cause_wins_over_the_checked_fix(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Инженер назвал другую причину после разбора по факту: разбор и предложение снимаются,
    снимок до next уже их не видит (ревью Codex)."""
    from test_skill_flow import FEEDBACK

    from alla_skill_lib import workspace as ws
    from alla_skill_lib.problem_state import current_state

    _checked_fix(nested_project, capsys)
    run_dir, _, order, login = _next_launch(nested_project, capsys, monkeypatch)
    _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    (run_dir / "feedback" / f"{order}.md").write_text(FEEDBACK, encoding="utf-8")
    assert _run(["remember", order, "--run", str(run_dir)], capsys)[1].startswith("STATUS: saved")

    paths = ws.resolve_run(str(run_dir), nested_project / "alla-reports")
    assert order not in current_state(paths).analyses
    again = _run(["next", str(run_dir)], capsys)[1]
    assert again.startswith("STATUS: analyze") and f"clusters/{order}.md" in again
    assert not (run_dir / "proposals" / f"{order}.md").exists()


def test_removed_fact_is_not_in_the_snapshot_before_next(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alla_skill_lib import workspace as ws
    from alla_skill_lib.problem_state import current_state

    _checked_fix(nested_project, capsys)
    run_dir, _, order, login = _next_launch(nested_project, capsys, monkeypatch)
    _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    paths = ws.resolve_run(str(run_dir), nested_project / "alla-reports")
    assert order in current_state(paths).analyses
    for path in (nested_project / "alla-kb" / "fixes").glob("*.json"):
        path.unlink()

    assert order not in current_state(paths).analyses


def test_other_test_source_needs_an_answer_from_git(tmp_path: Path) -> None:
    """Без ответа git «чистым» исходник не считается (ревью Grok)."""
    from alla_skill_lib import rerun

    assert not rerun.git_clean(tmp_path, Path("OrderTest.java"))  # не репозиторий


@pytest.mark.parametrize("change", ["other_log", "expired"])
def test_fact_is_rechecked_on_every_next(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    """Факт с тем же id перезаписал другой прогон (другой лог) или он устарел — разбор снят,
    проблему разбирает модель; review не считает разбор скрипта разбором модели (ревью Codex)."""
    from alla_skill_lib import workspace as ws
    from alla_skill_lib.review import _cluster_facts

    _checked_fix(nested_project, capsys)
    run_dir, run, order, login = _next_launch(nested_project, capsys, monkeypatch)
    _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    paths = ws.resolve_run(str(run_dir), nested_project / "alla-reports")
    state = json.loads(paths.state_json.read_text(encoding="utf-8"))
    facts = _cluster_facts(paths, run, state, Path(run["project_root"]))
    assert next(item for item in facts if item["number"] == int(order))["kb_auto"]

    [path] = (nested_project / "alla-kb" / "fixes").glob("*.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if change == "other_log":
        data["log_digest"] = "other"
    else:
        data["date"] = (date.today() - timedelta(days=fix_facts.MAX_AGE_DAYS + 1)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")

    again = _run(["next", str(run_dir)], capsys)[1]
    assert again.startswith("STATUS: analyze") and f"clusters/{order}.md" in again


def test_committing_the_fix_before_the_result_keeps_it_checked(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    """«Файл был как в HEAD до правки» запоминается при apply: коммит правки до показа итога
    запоминание не отменяет (ревью Grok)."""
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]})
    _git(nested_project)
    run_dir, order, out = _applied(nested_project, capsys, SOURCE)
    record = json.loads((run_dir / "proposals" / f"{order}.applied.json").read_text(encoding="utf-8"))
    assert record[fix_facts.CLEAN_BEFORE] is True
    subprocess.run(["git", "-C", str(nested_project), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qam", "fix"], check=True, capture_output=True)
    command = out.splitlines()[-1].split("выполни: ", 1)[1].split(" — дальше", 1)[0]
    done = _run(["rerun", *command.split(" rerun ", 1)[1].split()], capsys)[1]
    done = _wait_done(run_dir, capsys) if done.startswith("STATUS: running") else done

    assert "Запомнена проверенная правка" in done and len(_facts(nested_project)) == 1


def test_facts_of_other_logs_are_kept_apart_and_the_latest_wins(tmp_path: Path) -> None:
    """Разные логи за одной сигнатурой — разные факты; в один день берётся позже записанный."""
    (tmp_path / "A.java").write_text("a();\n", encoding="utf-8")
    (tmp_path / "B.java").write_text("b();\n", encoding="utf-8")
    store = fix_facts.FixStore(tmp_path / "alla-kb")

    def fact(file: str, log: str, saved: str) -> fix_facts.FixFact:
        return fix_facts.FixFact(fix_facts.fact_id("v1:s", log, file, 1, ["x();"]), "v1:s", log, ["t"],
                                 file, 1, [f"{file[0].lower()}();"], ["x();"], 1, "2026-11-05",
                                 saved)

    store.save(fact("A.java", "log1", "2026-11-05T09:00:00"))
    store.save(fact("A.java", "log2", "2026-11-05T09:00:00"))
    assert len(store.load()) == 2
    store.save(fact("B.java", "log1", "2026-11-05T20:00:00"))
    entry = {"kb_dir": str(tmp_path / "alla-kb"), "signature": "v1:s", "log_digest": "log1",
             "kb": []}
    chosen = fix_facts.match(entry, ["t"], tmp_path, today=date(2026, 11, 6))
    assert chosen is not None and chosen["file"] == "B.java"


def test_proposal_rewritten_after_apply_is_not_remembered(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    """Предложение переписали после apply (СТАЛО то же, БЫЛО другое): одобрено было не оно
    (ревью Codex)."""
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]})
    _git(nested_project)
    run_dir, order, out = _applied(nested_project, capsys, SOURCE)
    proposal = run_dir / "proposals" / f"{order}.md"
    proposal.write_text(proposal.read_text(encoding="utf-8").replace(
        "БЫЛО:\n        assertEquals(200", "БЫЛО:\n        assertEquals(202"), encoding="utf-8")
    command = out.splitlines()[-1].split("выполни: ", 1)[1].split(" — дальше", 1)[0]
    done = _run(["rerun", *command.split(" rerun ", 1)[1].split()], capsys)[1]
    done = _wait_done(run_dir, capsys) if done.startswith("STATUS: running") else done

    assert "Запомнена проверенная правка" not in done and _facts(nested_project) == []



def test_facts_of_a_failed_write_are_rolled_back(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запись упала посреди итога — факты этой попытки сняты, следующий показ запишет заново."""
    from alla_skill_lib import learn

    original = fix_facts.record_checked

    def broken(*args: object, **kwargs: object) -> tuple[list[str], list[str]]:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise OSError("диск заполнен")

    monkeypatch.setattr(learn.fix_facts, "record_checked", broken)
    run_dir, _, done = _checked_fix(nested_project, capsys)
    assert "В базу знаний итог не записан: диск заполнен." in done
    assert _facts(nested_project) == []
    monkeypatch.undo()
    again = _run(["rerun", "--wait", "--run", str(run_dir)], capsys)[1]
    assert "Запомнена проверенная правка" in again and len(_facts(nested_project)) == 1


def test_failed_refresh_keeps_the_fact_of_an_earlier_run(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Откат упавшей записи возвращает прежний факт, а не удаляет его (ревью Codex); ошибка
    записи отметки learned.json итог не обрывает."""
    from alla_skill_lib import rerun

    run_dir, _, _ = _checked_fix(nested_project, capsys)
    [before] = _facts(nested_project)
    (run_dir / "rerun" / "learned.json").unlink()

    def broken(*_args: object, **_kwargs: object) -> Path:
        raise OSError("диск заполнен")

    monkeypatch.setattr(fix_facts.FixStore, "save", broken)
    out = _run(["rerun", "--wait", "--run", str(run_dir)], capsys)[1]
    assert "В базу знаний итог не записан: диск заполнен." in out
    assert _facts(nested_project) == [before]

    monkeypatch.undo()
    monkeypatch.setattr(rerun, "write_learned", broken)
    out = _run(["rerun", "--wait", "--run", str(run_dir)], capsys)[1]
    assert out.startswith("STATUS: rerun_done") and "Запомнена проверенная правка" in out


def test_crlf_working_copy_with_autocrlf_counts_as_clean(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    """CRLF в рабочей копии, LF в git (autocrlf): файл чистый, правка запоминается (ревью Grok)."""
    source = nested_project / SOURCE
    source.write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]})
    shutil.rmtree(nested_project / ".git", ignore_errors=True)
    git = ["git", "-C", str(nested_project)]
    subprocess.run([*git, "init", "-q"], check=True, capture_output=True)
    subprocess.run([*git, "config", "core.autocrlf", "true"], check=True, capture_output=True)
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
                   check=True, capture_output=True)
    assert subprocess.run([*git, "status", "--porcelain", "--", SOURCE], capture_output=True,
                          text=True, check=True).stdout == ""
    run_dir, order, out = _applied(nested_project, capsys, SOURCE)
    record = json.loads((run_dir / "proposals" / f"{order}.applied.json").read_text(encoding="utf-8"))
    assert record[fix_facts.CLEAN_BEFORE] is True


def test_member_without_a_key_is_left_to_the_model() -> None:
    """Тест без full_name и testCaseId не сверить со списком проверенных (ревью Codex)."""
    from test_skill_kb_auto import _cluster

    from alla_skill_lib.signature import cluster_signature

    error = "ru.bank.payments.TransferRejectedException: transfer rejected"
    cluster, tests = _cluster(error, error)
    signature = cluster_signature(cluster, tests)
    assert fix_facts._members_same(cluster, tests, signature)
    tests[2] = tests[2].model_copy(update={"full_name": None, "test_case_id": None})
    assert not fix_facts._members_same(cluster, tests, signature)


def test_script_analysis_that_no_longer_passes_the_check_is_dropped(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Разбор по факту перестал проходить проверку (код изменился) — снимается, разбирает модель,
    а не «исправь файл» (ревью Grok)."""
    _checked_fix(nested_project, capsys)
    run_dir, _, order, login = _next_launch(nested_project, capsys, monkeypatch)
    _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    monkeypatch.setattr(fix_facts, "check_entry_analysis", lambda text, *args: (
        None, ["строка КОД за концом файла"] if "проверенная правка" in text else []))

    again = _run(["next", str(run_dir)], capsys)[1]

    assert again.startswith("STATUS: analyze") and f"clusters/{order}.md" in again
    assert not (run_dir / "analyses" / f"{order}.md").exists()
