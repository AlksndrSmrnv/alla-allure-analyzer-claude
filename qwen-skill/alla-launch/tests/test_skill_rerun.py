"""Перезапуск тестов локально (``rerun``): план, Maven фоновым заданием, ход и итог.

Maven — фейковый ``mvnw`` проекта (``skill_fake_maven``): он пишет отчёты surefire так же,
как настоящий (``TEST-<класс>-<суффикс>.xml``, ``classname`` с суффиксом в скобках).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
import skill_fake_maven
from skill_fake_testops import FakeTestOps, default_launch, flaky_launch
from skill_fixtures import (  # noqa: F401
    multimodule_project_fixture,
    nested_project_fixture,
    project_fixture,
    testops_fixture,
    without_libmagic,
)
from test_skill_flow import MARKDOWN_ANALYSIS, _finish, _prepare, _run

from alla_core.clients.testops_client import AllureTestOpsClient
from alla_core.config import Settings
from alla_skill_lib import flaky, rerun, resolve
from alla_skill_lib.rerun import (
    JobFiles,
    PlannedTest,
    method_result,
    read_reports,
    same_error,
    surefire_filter,
    worker_env,
)
from alla_skill_lib import rerun_worker
from alla_skill_lib.rerun_worker import execute

ORDER = "ru.company.orders.OrderTest"
FAILED_500 = "failed:expected: <200> but was: <500>"
UNKNOWN_500 = (
    "ЧТО СЛОМАЛОСЬ: Тест получил ответ 500 вместо 200.\n\n"
    "ПРИЧИНА: неизвестно — по коду ответа причину не видно.\n\n"
    "НАБЛЮДЕНИЯ:\n- [S1] «expected: <200> but was: <500>»\n"
    "НЕ ХВАТАЕТ: лога сервиса заказов за время теста.\n\n"
    "КАК ИСПРАВИТЬ:\n1. Посмотреть лог сервиса заказов за время теста.\n"
)


@pytest.fixture(name="flaky_testops")
def flaky_testops_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeTestOps:
    """Прогон 777, где у createOrder повтор упал иначе, а у updateOrder — прошёл."""
    fake = FakeTestOps(flaky_launch(777))
    fake.install(monkeypatch)
    return fake


def _problem_of(run: dict[str, Any], test_id: int) -> str:
    cluster = next(item for item in run["clustering"]["clusters"]
                   if test_id in item["member_test_ids"])
    entry = next(item for item in run["clusters"] if item["cluster_id"] == cluster["cluster_id"])
    return str(int(entry["file_id"]))


def _rerun(run_dir: Path, capsys: pytest.CaptureFixture[str], *args: str) -> str:
    _code, out = _run(["rerun", *args, "--run", str(run_dir)], capsys)
    return out


def _plan_hash(out: str) -> str:
    return out.split("--plan ", 1)[1].split()[0]


def _wait_done(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> str:
    for _ in range(40):
        out = _rerun(run_dir, capsys, "--wait")
        if not out.startswith("STATUS: running"):
            return out
    raise AssertionError(out)


def _start(run_dir: Path, capsys: pytest.CaptureFixture[str], number: str) -> str:
    plan = _rerun(run_dir, capsys, number)
    assert plan.startswith("STATUS: rerun"), plan
    return _rerun(run_dir, capsys, number, "--yes", "--plan", _plan_hash(plan))


def test_plan_names_the_tests_the_maven_command_and_the_confirmed_launch(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {})
    run_dir, run, _ = _prepare(nested_project, capsys)
    order = _problem_of(run, 101)

    out = _rerun(run_dir, capsys, order)

    assert out.startswith("STATUS: rerun\n"), out
    assert f"Проблема {order}: {ORDER}.createOrder (модуль autotests)" in out
    assert f"Проблема {order}: {ORDER}.updateOrder" in out
    assert f"-Dtest={ORDER}#createOrder+updateOrder" in out
    assert "до 3 раз" in out and "Только после явного «да»" in out
    assert f"rerun {order} --run {run_dir} --yes --plan {_plan_hash(out)}" in out
    assert not (run_dir / "rerun").exists()  # без --yes ничего не запущено


def test_rerun_until_the_first_pass_tells_flaky_from_reproduced(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    autotests = nested_project / "autotests"
    skill_fake_maven.install(autotests, {
        f"{ORDER}#createOrder": [FAILED_500, "passed"],
        f"{ORDER}#updateOrder": [FAILED_500],
    })
    run_dir, run, _ = _prepare(nested_project, capsys)
    order = _problem_of(run, 101)

    out = _start(run_dir, capsys, order)
    if out.startswith("STATUS: running"):
        out = _wait_done(run_dir, capsys)

    assert out.startswith("STATUS: rerun_done\n"), out
    body = out.split("===ПЕРЕЗАПУСК===", 1)[1].split("===КОНЕЦ===", 1)[0]
    assert "Нестабильные — упали в CI, а при перезапуске прошли (1):" in body
    assert f"Проблема {order}: {ORDER}.createOrder — прошёл на запуске 2 из 3" in body
    assert f"Проблема {order}: {ORDER}.updateOrder — упал 3 из 3" in body
    assert skill_fake_maven.runs(autotests) == {
        f"{ORDER}#createOrder": 2, f"{ORDER}#updateOrder": 3}
    files = JobFiles(run_dir / "rerun")
    assert sorted(files.logs.iterdir()) == [files.maven_log(k, "autotests") for k in (1, 2, 3)]
    result = json.loads((run_dir / "rerun" / "result.json").read_text(encoding="utf-8"))
    assert [item["outcome"] for item in result["tests"]] == ["passed", "reproduced"]


def test_other_local_error_is_not_retried_and_build_failure_is_reported(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    autotests = nested_project / "autotests"
    skill_fake_maven.install(autotests, {
        f"{ORDER}#createOrder": ["failed:Connection refused: localhost:8080"],
        f"{ORDER}#updateOrder": ["skipped"],
    })
    run_dir, run, _ = _prepare(nested_project, capsys)
    order = _problem_of(run, 101)

    out = _wait_done(run_dir, capsys) if (out := _start(run_dir, capsys, order)).startswith(
        "STATUS: running") else out

    assert "Падают локально с другой ошибкой — о нестабильности не говорят (1):" in out
    assert "createOrder — «Connection refused: localhost:8080»" in out
    assert "updateOrder — пропущен (skipped)" in out
    assert skill_fake_maven.runs(autotests) == {f"{ORDER}#createOrder": 1, f"{ORDER}#updateOrder": 1}

    skill_fake_maven.install(autotests, {}, exit_code=1)
    out = _start(run_dir, capsys, order)
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out
    assert "Не запустились (2):" in out
    assert "createOrder — сборка не прошла: COMPILATION ERROR :" in out


def test_module_missing_from_the_reactor_runs_as_its_own_project(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    (nested_project / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    skill_fake_maven.install(nested_project, {f"{ORDER}#createOrder": ["passed"],
                                              f"{ORDER}#updateOrder": ["passed"]},
                             not_in_reactor=True)
    run_dir, run, _ = _prepare(nested_project, capsys)
    order = _problem_of(run, 101)
    plan = _rerun(run_dir, capsys, order)
    assert "-pl autotests -am test" in plan

    out = _rerun(run_dir, capsys, order, "--yes", "--plan", _plan_hash(plan))
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out

    assert "Нестабильные — упали в CI, а при перезапуске прошли (2):" in out
    log = JobFiles(run_dir / "rerun").maven_log(1, "autotests").read_text(encoding="utf-8")
    assert "Could not find the selected project in the reactor" in log
    assert f"$ cd {nested_project / 'autotests'} && " in log  # повтор из папки модуля


def test_wrong_plan_hash_starts_nothing(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {})
    run_dir, run, _ = _prepare(nested_project, capsys)

    out = _rerun(run_dir, capsys, _problem_of(run, 101), "--yes", "--plan", "deadbeef")

    assert out.startswith("STATUS: rerun\nПлан изменился")
    assert not (run_dir / "rerun").exists()


def test_running_job_is_waited_for_and_then_left_to_the_background(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]}, sleep=3)
    run_dir, run, _ = _prepare(nested_project, capsys)
    monkeypatch.setattr(rerun, "WAIT_BUDGET", 0.0)

    out = _start(run_dir, capsys, _problem_of(run, 101))
    assert out.startswith("STATUS: running\n"), out
    assert f"rerun --run {run_dir} --wait" in out and "Пользователю пока ничего не пиши" in out
    for _ in range(rerun.MAX_WAITS - 2):
        assert "Пользователю пока" in _rerun(run_dir, capsys, "--wait")
    out = _rerun(run_dir, capsys, "--wait")
    assert "продолжается в фоне" in out and "Сам больше не жди" in out
    again = _rerun(run_dir, capsys, "2")
    assert again.startswith("STATUS: running\nНовый перезапуск не начат")
    assert "ход и итог можно спросить позже: /alla:rerun" in again

    monkeypatch.setattr(rerun, "WAIT_BUDGET", 30.0)
    assert _rerun(run_dir, capsys, "--wait").startswith("STATUS: rerun_done")


def test_another_rerun_in_the_project_blocks_the_start(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {})
    run_dir, run, _ = _prepare(nested_project, capsys)
    order = _problem_of(run, 101)
    plan = _rerun(run_dir, capsys, order)

    with rerun.hold_lock(run_dir.parent / rerun.LOCK_NAME) as held:
        assert held
        out = _rerun(run_dir, capsys, order, "--yes", "--plan", _plan_hash(plan))

    assert out.startswith("STATUS: ok\nВ проекте уже идёт перезапуск")
    assert not (run_dir / "rerun").exists()


def test_gradle_and_projects_without_maven_are_explained(
    multimodule_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, run, _ = _prepare(multimodule_project, capsys)

    out = _rerun(run_dir, capsys, _problem_of(run, 101))

    assert out.startswith("STATUS: ok\nПерезапустить нечего.")
    assert "createOrder — Gradle пока не поддерживается — только Maven" in out


def test_no_maven_anywhere_is_explained(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, run, _ = _prepare(nested_project, capsys)
    monkeypatch.setattr(rerun.shutil, "which", lambda name: None)

    out = _rerun(run_dir, capsys, _problem_of(run, 101))

    assert out.startswith("STATUS: ok\nПерезапустить нечего: Maven не найден")


def test_test_without_a_build_file_cannot_be_rerun(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, run, _ = _prepare(project, capsys)

    out = _rerun(run_dir, capsys, _problem_of(run, 103))

    assert "ru.company.auth.LoginTest.login — нет файла сборки (pom.xml)" in out


def test_applied_fix_makes_the_rerun_a_check_of_the_fix(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]})
    run_dir, run, _ = _prepare(nested_project, capsys)
    order = _problem_of(run, 101)
    (run_dir / "proposals").mkdir(exist_ok=True)
    (run_dir / "proposals" / f"{int(order):02d}.applied.json").write_text("{}", encoding="utf-8")

    plan = _rerun(run_dir, capsys, order)
    assert "проверка правки (правка агента применена): о нестабильности не скажет" in plan
    out = _rerun(run_dir, capsys, order, "--yes", "--plan", _plan_hash(plan))
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out

    assert "Прошли на изменённом коде теста — проверка правки, не нестабильность (2):" in out
    assert "Нестабильные" not in out


def test_zero_max_tests_turns_the_rerun_off(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, run, _ = _prepare(nested_project, capsys)
    monkeypatch.setenv("ALLURE_RERUN_MAX_TESTS", "0")

    out = _rerun(run_dir, capsys, _problem_of(run, 101))

    assert out.startswith("STATUS: ok\nПерезапуск тестов выключен")


def test_wait_without_a_rerun_says_so(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _, _ = _prepare(nested_project, capsys)

    assert _rerun(run_dir, capsys, "--wait").startswith("STATUS: ok\nПерезапуска тестов")


# ---------------------------------------------------------------------------
# части без CLI
# ---------------------------------------------------------------------------


def _planned(method: str, message: str = "expected: <200> but was: <500>") -> PlannedTest:
    return PlannedTest(test_result_id=hash(method) % 1000, file_id="01", name=method,
                       full_name=f"{ORDER}.{method}", class_name=ORDER, method=method,
                       source="src/test/java/ru/company/orders/OrderTest.java", module="",
                       message=message)


def test_surefire_reports_are_read_per_method_with_parameters_and_reruns(tmp_path: Path) -> None:
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-ru.company.orders.OrderTest-alla-j-1.xml").write_text(
        '<testsuite name="ru.company.orders.OrderTest(alla-j-1)">'
        '<testcase name="param(int)[1]" classname="ru.company.orders.OrderTest(alla-j-1)"/>'
        '<testcase name="param(int)[2]" classname="ru.company.orders.OrderTest(alla-j-1)">'
        '<failure message="boom 2">trace</failure></testcase>'
        '<testcase name="flaky" classname="ru.company.orders.OrderTest(alla-j-1)">'
        '<flakyFailure message="first"/></testcase>'
        '<testcase name="always" classname="ru.company.orders.OrderTest(alla-j-1)">'
        '<rerunFailure message="again"/></testcase>'
        '<testcase name="off" classname="ru.company.orders.OrderTest(alla-j-1)"><skipped/></testcase>'
        "</testsuite>", encoding="utf-8")
    (reports / "TEST-ru.company.orders.OrderTest.xml").write_text(  # чужой, старый запуск
        '<testsuite><testcase name="param" classname="ru.company.orders.OrderTest"/></testsuite>',
        encoding="utf-8")

    cases = read_reports(tmp_path, "alla-j-1", before={})

    assert method_result(cases, _planned("param")) == rerun.CaseResult("failed", "boom 2")
    assert method_result(cases, _planned("flaky")) == rerun.CaseResult("passed", flaky=True)
    assert method_result(cases, _planned("always")) == rerun.CaseResult("failed", "again")
    assert method_result(cases, _planned("off")) == rerun.CaseResult("skipped")
    assert method_result(cases, _planned("missing")) is None


def test_reports_without_our_suffix_count_only_if_this_run_wrote_them(tmp_path: Path) -> None:
    """pom со своим суффиксом: свежий отчёт прошлого запуска не выдаёт себя за итог этого."""
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    old = reports / "TEST-ru.company.orders.OrderTest.xml"
    old.write_text('<testsuite><testcase name="flaky" classname="ru.company.orders.OrderTest"/>'
                   "</testsuite>", encoding="utf-8")
    before = rerun.report_snapshot(tmp_path)

    assert not read_reports(tmp_path, "alla-j-1", before)  # сборка упала — отчёт прежний
    old.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
    import os
    os.utime(old, ns=(before[old.name] + 10**9, before[old.name] + 10**9))
    assert read_reports(tmp_path, "alla-j-1", before)  # этот запуск переписал его
    assert read_reports(tmp_path, "alla-j-1", {})  # и новый файл — тоже


def test_same_error_ignores_ids_and_time_but_not_codes() -> None:
    assert same_error("Order 123456789 not found at 2026-10-24T10:00:00",
                      "Order 987654321 not found at 2026-10-23T09:00:00")
    assert not same_error("error_code=10001 rejected", "error_code=10002 rejected")
    assert same_error("anything", "")  # в TestOps ошибки нет — сравнивать не с чем


def test_filter_groups_methods_by_class() -> None:
    tests = [_planned("a"), _planned("b"),
             PlannedTest(1, "02", "c", "ru.x.C.c", "ru.x.C", "c", "C.java", "", "")]

    assert surefire_filter(tests) == f"{ORDER}#a+b,ru.x.C#c"


def test_worker_env_drops_skill_settings_and_qwen_session() -> None:
    env = worker_env({"ALLURE_TOKEN": "t", "QWEN_CODE_SESSION_ID": "s", "PYTHONPATH": "p",
                      "PATH": "/bin", "MAVEN_OPTS": "-Xmx1g"})

    assert env == {"PATH": "/bin", "MAVEN_OPTS": "-Xmx1g"}


def test_time_limit_stops_maven_and_reports_what_did_not_run(tmp_path: Path) -> None:
    skill_fake_maven.install(tmp_path, {f"{ORDER}#a": ["passed"]}, sleep=30)
    files, plan = _job(tmp_path, timeout=1)

    started = time.monotonic()
    result = execute(files, plan)

    assert time.monotonic() - started < 20  # Maven остановлен, а не дождались его 30 с
    assert result["timed_out"] is True
    assert [item["reason"] for item in result["tests"]] == [rerun_worker.TIMEOUT_REASON]


def test_maven_args_are_split_like_a_shell() -> None:
    assert rerun.maven_args('-Pstand -Dstand.url="https://stand x" ') == [
        "-Pstand", "-Dstand.url=https://stand x"]
    assert rerun.maven_args("  ") == []


def test_rerun_settings_need_no_testops_but_keep_their_bounds() -> None:
    from alla_core.config import Settings
    from alla_core.exceptions import ConfigurationError

    settings = Settings.load(environ={"ALLURE_RERUN_ATTEMPTS": "2"}, require_testops=False)
    assert (settings.rerun_attempts, settings.rerun_max_tests, settings.rerun_timeout) == (2, 20, 1800)
    with pytest.raises(ConfigurationError, match="ALLURE_RERUN_ATTEMPTS: допустимо от 1 до 5"):
        Settings.load(environ={"ALLURE_RERUN_ATTEMPTS": "9"}, require_testops=False)
    with pytest.raises(ConfigurationError, match="ALLURE_ENDPOINT"):
        Settings.load(environ={})


# ---------------------------------------------------------------------------
# кандидаты скилла (flaky.py) и предложение после отчёта
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    "Expected condition failed: waiting for visibility of element (tried for 10 second(s))",
    "java.net.SocketTimeoutException: Read timed out",
    "stale element reference: element is not attached to the page document",
    "element click intercepted: Element <button> is not clickable",
    "expected: <200> but was: <503>",
    "Status code: 502 Bad Gateway",
    "HTTP/1.1 504 Gateway Time-out",
    "Connection reset by peer",
])
def test_transient_messages_are_recognised(message: str) -> None:
    assert flaky.transient(message)


@pytest.mark.parametrize("message", [
    "expected: <200> but was: <500>",
    "java.net.ConnectException: Connection refused: auth-service:8080",
    "Order 503 not found",  # число без контекста статуса
    "NullPointerException: customer is null",
])
def test_stable_messages_are_not_transient(message: str) -> None:
    assert not flaky.transient(message)


def _failed(message: str = "", *attempts: dict[str, Any]) -> dict[str, Any]:
    return {"status_message": message, "attempts": list(attempts)}


def test_signals_need_a_retry_or_a_transient_error_outside_test_and_data() -> None:
    passed = {"status": "passed"}
    other = {"status": "failed", "message": "x", "same_as_final": False}
    same = {"status": "failed", "message": "Read timed out", "same_as_final": True}

    assert flaky.signals_of(_failed("", passed), "тест") == ["retry_passed"]
    assert flaky.signals_of(_failed("Read timed out", other), None) == [
        "retry_different", "transient"]
    assert flaky.signals_of(_failed("Read timed out"), "окружение") == ["transient"]
    assert flaky.signals_of(_failed("Read timed out"), "данные") == []
    assert flaky.signals_of(_failed("Read timed out", same), None) == []  # все повторы — так же
    assert flaky.signals_of(_failed("expected: <200> but was: <500>"), "окружение") == []


def test_offer_after_the_report_reruns_the_candidates_with_one_yes(
    nested_project: Path, flaky_testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": [FAILED_500, "passed"], f"{ORDER}#updateOrder": ["passed"]})
    run_dir, run, _ = _prepare(nested_project, capsys)
    order, login = _problem_of(run, 101), _problem_of(run, 103)

    done = _finish(run_dir, capsys, {f"{int(order):02d}": UNKNOWN_500,
                                     f"{int(login):02d}": MARKDOWN_ANALYSIS})

    offer = next(line for line in done.splitlines() if "Похожи на нестабильные" in line)
    assert f"2 теста в проблемах {order}" in offer and "Только после явного «да»" in offer
    assert "уже ответил на этот вопрос или сам попросил перезапустить тесты, не спрашивай" in offer
    assert f"({ORDER}.createOrder; {ORDER}.updateOrder)" in offer and "на этой машине" in offer
    plan = _rerun(run_dir, capsys)
    assert plan.startswith("STATUS: rerun\n"), plan
    assert "createOrder (модуль autotests) — в TestOps повтор упал с другой ошибкой" in plan
    assert "updateOrder (модуль autotests) — в TestOps повтор прошёл" in plan
    command = offer.split("выполни: ", 1)[1].split(" — дальше", 1)[0]
    assert command.endswith(f"rerun --run {run_dir} --yes --plan {_plan_hash(plan)}")

    out = _rerun(run_dir, capsys, "--yes", "--plan", _plan_hash(plan))
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out
    assert "Нестабильные — упали в CI, а при перезапуске прошли (2):" in out
    assert "Похожи на нестабильные" not in _run(["next", str(run_dir)], capsys)[1]  # уже был
    assert _rerun(run_dir, capsys).startswith("STATUS: rerun_done")  # без номеров — итог


@pytest.mark.parametrize("fixture", ["project", "nested_project"])
def test_no_offer_without_maven_or_with_an_applied_fix(
    fixture: str, flaky_testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
) -> None:
    root: Path = request.getfixturevalue(fixture)
    run_dir, run, _ = _prepare(root, capsys)
    order = f"{int(_problem_of(run, 101)):02d}"
    if fixture == "nested_project":  # Maven есть, кандидаты есть, но правка уже применена
        (run_dir / "proposals").mkdir(exist_ok=True)
        (run_dir / "proposals" / f"{order}.applied.json").write_text("{}", encoding="utf-8")

    done = _finish(run_dir, capsys, {order: UNKNOWN_500,
                                     f"{int(_problem_of(run, 103)):02d}": MARKDOWN_ANALYSIS})

    assert "Похожи на нестабильные" not in done
    assert _rerun(run_dir, capsys).startswith("STATUS: ok\nТестов, похожих на нестабильные")


def test_rerun_result_goes_to_the_report_without_a_new_summary_or_hooks(
    nested_project: Path, flaky_testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    from alla_skill_lib import extensions as ext
    from test_skill_extensions import ext_cli, make_demo

    make_demo(nested_project)
    ext_cli(nested_project, capsys, "enable", "demo")
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": [FAILED_500, "passed"], f"{ORDER}#updateOrder": [FAILED_500]})
    run_dir, run, _ = _prepare(nested_project, capsys)
    order, login = _problem_of(run, 101), _problem_of(run, 103)
    done = _finish(run_dir, capsys, {f"{int(order):02d}": UNKNOWN_500,
                                     f"{int(login):02d}": MARKDOWN_ANALYSIS})
    assert "событие after_report" in done
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))

    plan = _rerun(run_dir, capsys)
    out = _rerun(run_dir, capsys, "--yes", "--plan", _plan_hash(plan))
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out

    assert f"Полный разбор с итогом: {run_dir / 'report.md'}\n===КОНЕЦ===" in out
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Перезапуск локально: нестабильных 1 из 2 перезапущенных тестов — раздел" in report
    section = report.split("### Перезапуск локально (2)", 1)[1].split("\n### ", 1)[0]
    assert "Нестабильные — упали в CI, а при перезапуске прошли (1):" in section
    assert f"- Проблема {order}: {ORDER}.updateOrder — упал 3 из 3" in section
    assert ("- Перезапуск локально: 2 теста: нестабильные — 1, воспроизводятся — 1 "
            "(раздел «Перезапуск локально»)") in report
    brief = _run(["next", str(run_dir)], capsys)[1]
    assert "Перезапуск локально: нестабильных 1 из 2 перезапущенных тестов (список в полном " \
           "разборе)" in brief
    assert "[прошёл при перезапуске: 1 из 2]" in brief
    assert ext.BLOCK_HEADING not in brief  # after_report на итог перезапуска не повторяется
    after = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert after["summary_data_hash"] == state["summary_data_hash"]
    assert (run_dir / "summary.md").is_file()


def test_failed_job_offers_the_retry_command(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, run, _ = _prepare(nested_project, capsys)
    order = _problem_of(run, 101)
    files = JobFiles(run_dir / "rerun")
    files.root.mkdir()
    files.plan.write_text(json.dumps({"job": "j", "tests": [{"file_id": f"{int(order):02d}"}]}),
                          encoding="utf-8")
    files.failed.write_text("сбой перезапуска: OSError: диск полон\n", encoding="utf-8")

    out = _rerun(run_dir, capsys)

    assert out.startswith("STATUS: ok\nПерезапуск тестов не удался: сбой перезапуска: OSError")
    assert "Повторить (покажет план заново): " in out
    assert out.rstrip().endswith(f"rerun {order} --run {run_dir}")


def test_problem_with_a_proposed_fix_is_not_offered(
    nested_project: Path, flaky_testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    from test_skill_flow import PROPOSAL, TEST_ANALYSIS

    source = "src/test/java/ru/company/orders/OrderTest.java"
    run_dir, run, _ = _prepare(nested_project, capsys)
    order, login = f"{int(_problem_of(run, 101)):02d}", f"{int(_problem_of(run, 103)):02d}"
    (run_dir / "analyses" / f"{order}.md").write_text(
        TEST_ANALYSIS.replace(source, f"autotests/{source}"), encoding="utf-8")
    assert _run(["next", str(run_dir)], capsys)[1].startswith("STATUS: propose")
    (run_dir / "proposals" / f"{order}.md").write_text(
        PROPOSAL.replace(source, f"autotests/{source}"), encoding="utf-8")

    done = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})

    assert f"Правка автотеста для проблемы {int(order)}" in done
    assert "Похожи на нестабильные" not in done  # повтор в TestOps прошёл, но правка предложена


# ---------------------------------------------------------------------------
# отметки «нестабильный» в базе знаний
# ---------------------------------------------------------------------------


def _remember_command(out: str) -> list[str]:
    command = out.split("Только после явного «да» на этот вопрос выполни: ", 1)[1].split("\n")[0]
    return command.split(" rerun ", 1)[1].split()


def test_flaky_tests_are_remembered_offered_next_time_and_unmarked_when_they_reproduce(
    nested_project: Path, flaky_testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    autotests = nested_project / "autotests"
    skill_fake_maven.install(autotests, {
        f"{ORDER}#createOrder": [FAILED_500, "passed"], f"{ORDER}#updateOrder": [FAILED_500]})
    run_dir, run, _ = _prepare(nested_project, capsys)
    order, login = _problem_of(run, 101), _problem_of(run, 103)
    _finish(run_dir, capsys, {f"{int(order):02d}": UNKNOWN_500,
                              f"{int(login):02d}": MARKDOWN_ANALYSIS})
    out = _rerun(run_dir, capsys, "--yes", "--plan", _plan_hash(_rerun(run_dir, capsys)))
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out
    # Один вопрос: отметка в базе знаний и статус passed в TestOps (updateOrder воспроизвёлся).
    assert (f"задай пользователю один вопрос — сохранить ли итог: отметить в базе знаний "
            f"нестабильными 1 тест ({ORDER}.createOrder); отметить успешными в TestOps (статус "
            f"passed в прогоне 777) 1 тест ({ORDER}.createOrder).") in out
    assert ("Согласие только с частью («в базу знаний да, TestOps не трогай», «только первый "
            "тест») — не «да»: ничего не выполняй") in out
    assert "ALLURE_RERUN_RESOLVE=false в .env скилла (.env и переменные окружения сам не меняй)" \
        in out
    args = _remember_command(out)
    assert args[:3] == ["--run", str(run_dir), "--remember"]

    saved = _run(["rerun", *args], capsys)[1]

    assert saved.startswith("STATUS: saved\nСохранено в базу знаний.")
    assert f"Полный разбор обновлён: {run_dir / 'report.md'}" in saved
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert f"- Нестабильные по базе знаний: {ORDER}.createOrder (с " in report
    assert f"{ORDER}.createOrder — прошёл на запуске 2 из 3; в TestOps — passed" in report
    assert f"Отмечены нестабильными: {ORDER}.createOrder." in saved
    assert (f"Отмечены успешными в TestOps (статус passed в прогоне 777): {ORDER}.createOrder."
            in saved)
    assert list(flaky_testops.resolved) == [101]
    assert flaky_testops.resolved[101] == {
        "status": "passed",
        "message": "alla: тест прошёл при перезапуске на машине инженера (запуск 2 из 3). "
                   "Ошибка в CI: expected: <200> but was: <500>"}
    assert "закоммитить папку базы знаний alla-kb (отметки — в alla-kb/flaky/)" in saved
    marks = list((nested_project / "alla-kb" / "flaky").glob("*.json"))
    assert [path.name.split("_")[:2] for path in marks] == [["OrderTest", "createOrder"]]
    mark = json.loads(marks[0].read_text(encoding="utf-8"))
    assert mark["test"] == f"{ORDER}.createOrder" and mark["launch_id"] == 777
    assert (mark["passed_on"], mark["attempts"], mark["signals"]) == (2, 3, ["retry_different"])
    assert "Папка `flaky/`" in (nested_project / "alla-kb" / "README.md").read_text(encoding="utf-8")
    # Повтор команды: в TestOps уже отмечено — запрос не уходит снова.
    again = _run(["rerun", *args], capsys)[1]
    assert again.startswith("STATUS: ok\nСохранять нечего")
    assert "--remember" not in _rerun(run_dir, capsys, "--wait")  # вопрос не повторяется
    assert [method for method, path in flaky_testops.requests if path.endswith("/resolve")] \
        == ["POST"]

    # Следующий прогон: повторов нет, но тест отмечен — его снова предлагают, отчёт его помечает.
    FakeTestOps(default_launch(778)).install(monkeypatch)
    skill_fake_maven.install(autotests, {f"{ORDER}#createOrder": [FAILED_500]})
    run_dir, run, prepared = _prepare(nested_project, capsys, launch_id=778)
    assert "База знаний: пропущен" not in prepared  # подпапка отметок — не запись базы знаний
    order = _problem_of(run, 101)
    done = _finish(run_dir, capsys, {f"{int(order):02d}": UNKNOWN_500,
                                     f"{int(_problem_of(run, 103)):02d}": MARKDOWN_ANALYSIS})
    assert "Похожи на нестабильные (флаки): 1 тест" in done
    assert "[нестабильные по базе знаний: 1]" in done
    today = mark["since"]
    assert f"- Нестабильные по базе знаний: {ORDER}.createOrder (с {today})" in \
        (run_dir / "report.md").read_text(encoding="utf-8")
    plan = _rerun(run_dir, capsys)
    assert "createOrder (модуль autotests) — отмечен нестабильным в базе знаний" in plan

    out = _rerun(run_dir, capsys, "--yes", "--plan", _plan_hash(plan))
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out
    assert f"снять в базе знаний отметку «нестабильный» ({ORDER}.createOrder): тест " \
           "воспроизводится каждый раз." in out
    assert "TestOps" not in out.split(rerun.RESULT_END, 1)[1]  # прошедших нет
    saved = _run(["rerun", *_remember_command(out)], capsys)[1]
    assert f"Сняты отметки (тест воспроизводится): {ORDER}.createOrder." in saved
    assert not list((nested_project / "alla-kb" / "flaky").glob("*.json"))
    assert "Нестабильные по базе знаний" not in (run_dir / "report.md").read_text(encoding="utf-8")


def test_passed_test_without_a_ci_signal_is_marked_only_in_testops(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]})
    run_dir, run, _ = _prepare(nested_project, capsys)
    monkeypatch.setenv("ALLURE_RERUN_RESOLVE", "false")

    out = _start(run_dir, capsys, _problem_of(run, 101))  # 500 без повторов: стенд мог лежать
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out

    assert "Нестабильные — упали в CI, а при перезапуске прошли (2):" in out
    assert "--remember" not in out  # в базу знаний не идёт, отметка в TestOps выключена
    job = json.loads((run_dir / "rerun" / "result.json").read_text(encoding="utf-8"))["job"]
    assert _rerun(run_dir, capsys, "--remember", "--result", job).startswith(
        "STATUS: ok\nСохранять нечего")

    monkeypatch.delenv("ALLURE_RERUN_RESOLVE")
    out = _rerun(run_dir, capsys, "--wait")
    question = out.split(rerun.RESULT_END, 1)[1]
    assert "в базе знаний" not in question
    assert (f"сохранить ли итог: отметить успешными в TestOps (статус passed в прогоне 777) "
            f"2 теста ({ORDER}.createOrder; {ORDER}.updateOrder).") in question
    assert _rerun(run_dir, capsys, "--remember", "--result", "other").startswith("STATUS: error")

    saved = _run(["rerun", *_remember_command(out)], capsys)[1]

    assert saved.startswith("STATUS: saved\nОтмечены успешными в TestOps (статус passed в "
                            f"прогоне 777): {ORDER}.createOrder; {ORDER}.updateOrder.")
    assert "базы знаний" not in saved and saved.rstrip().endswith(
        "Скажи это пользователю. `next` не выполняй.")
    assert sorted(testops.resolved) == [101, 102]
    assert _rerun(run_dir, capsys, "--remember", "--result", job).startswith(
        "STATUS: ok\nСохранять нечего")
    assert "--remember" not in _rerun(run_dir, capsys, "--wait")  # всё отмечено — не спрашивает


def _testops_only(project: Path, capsys: pytest.CaptureFixture[str]) -> tuple[Path, str]:
    """Оба теста проблемы прошли без признака в CI: сохранять — только статус в TestOps."""
    skill_fake_maven.install(project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]})
    run_dir, run, _ = _prepare(project, capsys)
    out = _start(run_dir, capsys, _problem_of(run, 101))
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out
    return run_dir, out


def test_redirect_is_not_taken_for_a_changed_status(
    nested_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeTestOps(default_launch(777), resolve_status=302)
    fake.install(monkeypatch)
    run_dir, out = _testops_only(nested_project, capsys)

    saved = _run(["rerun", *_remember_command(out)], capsys)[1]

    assert saved.startswith("STATUS: ok\nНе отмечены в TestOps: ")
    assert "TestOps ответил перенаправлением (HTTP 302)" in saved
    assert len([path for _, path in fake.requests if path.endswith("/resolve")]) == 1
    assert not (run_dir / "rerun" / "resolved.json").exists()


def test_marks_beyond_the_answer_budget_are_sent_by_the_repeat(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, out = _testops_only(nested_project, capsys)
    args = _remember_command(out)
    monkeypatch.setattr(rerun, "WAIT_BUDGET", 0.0)  # время ответа вышло после первого запроса

    first = _run(["rerun", *args], capsys)[1]

    assert first.startswith("STATUS: saved\nОтмечены успешными в TestOps (статус passed в "
                            f"прогоне 777): {ORDER}.createOrder.\n")
    assert list(testops.resolved) == [101]
    marks = json.loads((run_dir / "rerun" / "resolved.json").read_text(encoding="utf-8"))
    assert list(marks["tests"]) == ["101"]  # записано сразу, а не в конце команды
    repeat = first.rstrip().splitlines()[-1]
    assert repeat.startswith(f"Не успели уйти в TestOps за один ответ: {ORDER}.updateOrder. ")
    assert "сразу, в том же ходе, выполни ту же команду ещё раз" in repeat
    assert repeat.endswith(" ".join(["--run", str(run_dir), "--remember", "--result", args[-1]]))

    monkeypatch.setattr(rerun, "WAIT_BUDGET", 75.0)
    second = _run(["rerun", *args], capsys)[1]
    assert second.startswith(f"STATUS: saved\nОтмечены успешными в TestOps (статус passed в "
                             f"прогоне 777): {ORDER}.updateOrder.")
    assert sorted(testops.resolved) == [101, 102] and "Не успели" not in second


def test_send_reports_part_of_the_variants_and_stops_on_a_silent_testops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeTestOps(default_launch(777))
    fake.resolve_status_of = {102: 404}
    fake.install(monkeypatch)
    settings = Settings(endpoint="https://testops.example", token="t")
    mark = resolve.Mark([101, 102], f"{ORDER}.createOrder", "01", "", {101: "n1", 102: "n2"})
    done: list[int] = []

    outcome = asyncio.run(resolve.send(settings, [mark], Path(".env"), "d",
                                       deadline=time.monotonic() + 75, on_done=done.append))

    assert done == [101] and fake.resolved[101]["message"] == "n1"
    assert outcome.partly([mark]) == [(mark, 1)] and outcome.done_marks([mark]) == []
    assert "не знает запрос resolve" in outcome.failed[0][1]

    # TestOps молчит: запрос с обменом токена ограничен сроком ответа, остальные не идут.
    async def silent(*_args: object) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(AllureTestOpsClient, "resolve_test_result", silent)
    other = resolve.Mark([103], f"{ORDER}.login", "02", "", {103: "n3"})
    outcome = asyncio.run(resolve.send(settings, [mark, other], Path(".env"), "d",
                                       deadline=time.monotonic() - resolve.HARD_MARGIN_S,
                                       on_done=done.append))
    assert [(item.full_name, reason) for item, reason in outcome.failed] == [
        (mark.full_name, resolve.NO_ANSWER), (other.full_name, resolve.NO_ANSWER)]
    assert outcome.done == {} and outcome.left == []


def test_refusal_turns_the_automatic_repeat_off(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ съел срок ответа: повтор тратил бы его на тот же отказ и не дошёл бы до остальных."""
    run_dir, out = _testops_only(nested_project, capsys)
    testops.resolve_status_of = {101: 404}
    monkeypatch.setattr(rerun, "WAIT_BUDGET", 0.0)

    answer = _run(["rerun", *_remember_command(out)], capsys)[1]

    assert answer.startswith(f"STATUS: ok\nНе успели уйти в TestOps за один ответ: "
                             f"{ORDER}.updateOrder.\nНе отмечены в TestOps: {ORDER}.createOrder")
    assert "Сам команду не повторяй" in answer and "в том же ходе" not in answer
    assert list(testops.resolved) == []


def test_report_marks_a_method_passed_only_when_every_variant_is(tmp_path: Path) -> None:
    plan = {"job": "j", "tests": [{"test_result_id": 101, "other_ids": [102]}]}
    item = {"test_result_id": 101, "outcome": "passed", "attempt": 1, "file_id": "01",
            "full_name": f"{ORDER}.createOrder"}
    state = rerun.JobState("done", plan, result={"job": "j", "attempts": 3, "tests": [item]})

    assert "TestOps" not in "\n".join(rerun.RerunView.of(state).lines())  # type: ignore[union-attr]
    state.resolved = {101: "d"}
    assert "в TestOps — passed у 1 из 2 вариантов" in "\n".join(
        rerun.RerunView.of(state).lines())  # type: ignore[union-attr]
    state.resolved = {101: "d", 102: "d"}
    assert f"{ORDER}.createOrder — прошёл на запуске 1 из 3; в TestOps — passed" in "\n".join(
        rerun.RerunView.of(state).lines())  # type: ignore[union-attr]


def test_testops_refusal_keeps_the_knowledge_base_and_the_command_can_be_repeated(
    nested_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeTestOps(flaky_launch(777), resolve_status=403)
    fake.install(monkeypatch)
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": [FAILED_500, "passed"], f"{ORDER}#updateOrder": ["passed"]})
    run_dir, run, _ = _prepare(nested_project, capsys)
    _finish(run_dir, capsys, {f"{int(_problem_of(run, 101)):02d}": UNKNOWN_500,
                              f"{int(_problem_of(run, 103)):02d}": MARKDOWN_ANALYSIS})
    out = _rerun(run_dir, capsys, "--yes", "--plan", _plan_hash(_rerun(run_dir, capsys)))
    out = _wait_done(run_dir, capsys) if out.startswith("STATUS: running") else out
    args = _remember_command(out)

    saved = _run(["rerun", *args], capsys)[1]

    # Отказ TestOps (403) на первом результате — второй запрос не уходит, база знаний записана.
    assert saved.startswith("STATUS: saved\nСохранено в базу знаний.")
    assert (f"Не отмечены в TestOps: {ORDER}.createOrder — у токена нет права менять результаты "
            "тестов в проекте TestOps") in saved
    assert f"{ORDER}.updateOrder — у токена нет права" in saved
    assert "Сам команду не повторяй" in saved and "закоммитить папку базы знаний" in saved
    assert "Не меняй .env и переменные окружения сам" in saved
    assert [path for _, path in fake.requests if path.endswith("/resolve")] == [
        "/api/testresult/101/resolve"]
    assert not (run_dir / "rerun" / "resolved.json").exists()
    assert "в TestOps — passed" not in (run_dir / "report.md").read_text(encoding="utf-8")

    # Адрес TestOps в .env сменился: id результатов чужие — запроса нет.
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://other.example")
    moved = _run(["rerun", *args], capsys)[1]
    assert "прогон разбирался на другом TestOps, чем ALLURE_ENDPOINT сейчас" in moved
    assert len([path for _, path in fake.requests if path.endswith("/resolve")]) == 1

    monkeypatch.setenv("ALLURE_ENDPOINT", "https://testops.example")
    fake.resolve_status = 202
    fixed = _run(["rerun", *args], capsys)[1]
    assert (f"Отмечены успешными в TestOps (статус passed в прогоне 777): {ORDER}.createOrder; "
            f"{ORDER}.updateOrder.") in fixed
    assert sorted(fake.resolved) == [101, 102]


def test_partly_skipped_method_is_not_a_pass(tmp_path: Path) -> None:
    """Параметризованный метод: один вызов пропущен — упавший в CI вариант мог не выполниться."""
    cases = {(ORDER, "param"): [rerun.CaseResult("passed"), rerun.CaseResult("skipped")]}

    assert method_result(cases, _planned("param")) == rerun.CaseResult("skipped")


def _job(tmp_path: Path, timeout: int, test: PlannedTest | None = None,
         ) -> tuple[JobFiles, dict[str, Any]]:
    files = JobFiles(tmp_path / "rerun")
    files.root.mkdir()
    group = rerun.MavenGroup(module="", cwd=str(tmp_path), tool=[str(tmp_path / "mvnw")],
                             select=[], fallback_cwd="")
    return files, {"job": "j", "attempts": 3, "timeout": timeout, "maven_args": [],
                   "project_root": str(tmp_path), "tests": [vars(test or _planned("a"))],
                   "groups": [vars(group)]}


def test_timeout_after_a_reproduced_run_keeps_the_reproduction(tmp_path: Path) -> None:
    skill_fake_maven.install(tmp_path, {f"{ORDER}#a": [FAILED_500]}, sleep=30, sleep_from_call=2)
    files, plan = _job(tmp_path, timeout=6)  # с запасом на первый запуск под нагрузкой

    result = execute(files, plan)

    item = result["tests"][0]
    assert (item["outcome"], item["runs"], item["cut"]) == (
        "reproduced", 1, rerun_worker.TIMEOUT_REASON)
    assert "упал 1 из 1 (дальше не проверен: вышел потолок времени" in "\n".join(
        rerun.RerunView(3, result["tests"], {}).lines())


def test_timeout_kills_the_whole_maven_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Maven выходит по SIGTERM, а его потомок (форк surefire) SIGTERM игнорирует."""
    import os

    monkeypatch.setattr(rerun_worker, "KILL_GRACE_S", 1)
    skill_fake_maven.install(tmp_path, {f"{ORDER}#a": ["passed"]}, sleep=30, orphan=True)
    files, plan = _job(tmp_path, timeout=4)  # Maven должен успеть запустить потомка

    execute(files, plan)

    pid = int((tmp_path / skill_fake_maven.ORPHAN_PID_NAME).read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(pid, 9)
    raise AssertionError("потомок Maven пережил остановку по времени")


def test_second_start_while_running_does_not_replace_the_job(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": ["passed"]}, sleep=3)
    run_dir, run, _ = _prepare(nested_project, capsys)
    order = _problem_of(run, 101)
    plan = _rerun(run_dir, capsys, order)
    paths = rerun.ws.RunPaths(run_dir)
    built = rerun.build_plan(paths, json.loads((run_dir / "run.json").read_text()),
                             [(entry, test) for entry in json.loads((run_dir / "run.json")
                                                                     .read_text())["clusters"]
                              if str(int(entry["file_id"])) == order
                              for test in rerun.failed_tests_of(run, entry)],
                             attempts=3, max_tests=20)
    assert built.digest == _plan_hash(plan)

    first = rerun.start_job(paths, built, 60, [])
    second = rerun.start_job(paths, built, 60, [])

    assert first is not None and second is None
    assert json.loads((run_dir / "rerun" / "plan.json").read_text())["job"] == first
    assert _wait_done(run_dir, capsys).startswith("STATUS: rerun_done")


def test_test_edited_during_the_rerun_is_a_check_of_the_fix(tmp_path: Path) -> None:
    source = tmp_path / "src" / "OrderTest.java"
    source.parent.mkdir()
    source.write_text("class OrderTest {}\n", encoding="utf-8")
    planned = PlannedTest(1, "01", "a", f"{ORDER}.a", ORDER, "a", "src/OrderTest.java", "", "",
                          source_sha=rerun.source_digest(tmp_path, "src/OrderTest.java"))
    skill_fake_maven.install(tmp_path, {f"{ORDER}#a": [FAILED_500, "passed"]})
    files, plan = _job(tmp_path, timeout=60, test=planned)
    assert execute(files, plan)["tests"][0]["changed"] == ""  # код тот же

    shutil.rmtree(files.root)
    (tmp_path / skill_fake_maven.RUNS_NAME).unlink()
    files, plan = _job(tmp_path, timeout=60, test=planned)
    source.write_text("class OrderTest { /* правка */ }\n", encoding="utf-8")
    item = execute(files, plan)["tests"][0]

    assert (item["outcome"], item["changed"]) == ("passed", rerun_worker.EDITED_REASON)
    assert rerun.RerunView.group(item) == "fixed"


def test_project_lock_holds_across_report_folders(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {})
    reports = tmp_path / "elsewhere"
    code, out = _run(["prepare", "777", "--project-root", str(nested_project),
                      "--reports-dir", str(reports)], capsys)
    assert code == 0, out
    run_dir = next(path for path in reports.iterdir() if (path / "run.json").is_file())
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    order = _problem_of(run, 101)
    plan = _rerun(run_dir, capsys, order)

    with rerun.hold_lock(rerun.project_lock(nested_project)) as held:  # перезапуск другого разбора
        assert held
        out = _rerun(run_dir, capsys, order, "--yes", "--plan", _plan_hash(plan))

    assert out.startswith("STATUS: ok\nВ проекте уже идёт перезапуск")
    assert not (run_dir / "rerun" / "plan.json").exists()


def test_multiline_errors_are_compared_whole() -> None:
    """RestAssured: первая строка у всех одна, а код ответа — ниже."""
    ci = "1 expectation failed.\nExpected status code <200> but was <500>.\n"

    assert same_error("1 expectation failed.\n  Expected status code <200> but was <500>.", ci)
    assert not same_error("1 expectation failed.\nExpected status code <200> but was <401>.", ci)


def test_failed_invocation_matching_ci_wins_whatever_the_xml_order() -> None:
    other = rerun.CaseResult("failed", "expected: <200> but was: <401>")
    same = rerun.CaseResult("failed", "expected: <200> but was: <500>")

    for order in ([other, same], [same, other]):
        assert method_result({(ORDER, "param"): order}, _planned("param")) == same
    assert method_result({(ORDER, "param"): [other]}, _planned("param")) == other


def test_failure_without_a_message_attribute_drops_the_exception_class(tmp_path: Path) -> None:
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-x-alla-j-1.xml").write_text(
        f'<testsuite><testcase name="a" classname="{ORDER}(alla-j-1)"><error type="E">'
        "java.net.SocketTimeoutException: Read timed out\n\tat x.y(Z.java:1)</error>"
        "</testcase></testsuite>", encoding="utf-8")

    cases = read_reports(tmp_path, "alla-j-1", {})

    assert method_result(cases, _planned("a")) == rerun.CaseResult("failed", "Read timed out")


@pytest.mark.parametrize("full_name", [
    f"{ORDER}#createOrder+updateOrder", f"{ORDER}#*", f"{ORDER}#create?rder",
    f"{ORDER}#createOrder,ru.x.Other", f"{ORDER}#should create order",
])
def test_selector_characters_from_testops_never_reach_maven(
    nested_project: Path, full_name: str,
) -> None:
    """Имя теста — данные TestOps: шаблон surefire запустил бы больше показанного в плане."""
    index = rerun.ProjectIndex(nested_project)

    found = rerun.locate(nested_project, index, full_name)

    assert found == "в имени теста есть символы, которые Maven понял бы как шаблон"


def test_plan_limit_is_checked_before_git_status(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {})
    run_dir, run, _ = _prepare(nested_project, capsys)
    entry = next(item for item in run["clusters"] if str(int(item["file_id"])) == _problem_of(run, 101))
    calls: list[str] = []
    monkeypatch.setattr(rerun, "_git_dirty", lambda root, source: calls.append(str(source)) or False)

    plan = rerun.build_plan(rerun.ws.RunPaths(run_dir), run,
                            [(entry, test) for test in rerun.failed_tests_of(run, entry)],
                            attempts=3, max_tests=1)

    assert [test.method for test in plan.tests] == ["createOrder"]
    assert [item.reason for item in plan.skipped] == [
        "сверх лимита ALLURE_RERUN_MAX_TESTS (1 за раз)"]
    assert len(calls) == 1  # сверх лимита git status не вызывается


def test_variants_of_a_method_keep_every_ci_error_and_each_problem_its_own_test(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    run_dir, run, _ = _prepare(nested_project, capsys)
    tests = {test["test_result_id"]: test for test in run["triage"]["failed_tests"]}
    tests[102].update(full_name=f"{ORDER}.createOrder[2]", status_message="boom 2")
    tests[103].update(full_name=f"{ORDER}.createOrder[3]")  # тот же метод в другой проблеме
    order, login = _problem_of(run, 101), _problem_of(run, 103)
    picks = [(entry, test) for entry in run["clusters"]
             if str(int(entry["file_id"])) in (order, login)
             for test in rerun.failed_tests_of(run, entry)]

    plan = rerun.build_plan(rerun.ws.RunPaths(run_dir), run, picks, attempts=3, max_tests=20)

    assert [(int(t.file_id), t.method) for t in plan.tests] == [
        (int(order), "createOrder"), (int(login), "createOrder")]
    assert plan.tests[0].other_messages == ["boom 2"]
    assert plan.tests[0].other_ids == [102]
    # Метод прошёл — passed нужен результатам всех его вариантов, а отмеченные не повторяются.
    result = {"attempts": 3, "tests": [{"test_result_id": 101, "outcome": "passed", "attempt": 1,
                                        "full_name": f"{ORDER}.createOrder", "file_id": order}]}
    planned = {"tests": [asdict(test) for test in plan.tests]}
    assert [mark.ids for mark in resolve.plan_marks(result, planned, {})] == [[101, 102]]
    notes = resolve.plan_marks(result, planned, {}, {101: "boom 1", 102: "boom 2"})[0].notes
    assert notes[101].endswith("Ошибка в CI: boom 1") and notes[102].endswith("Ошибка в CI: boom 2")
    assert [mark.ids for mark in resolve.plan_marks(result, planned, {101: "d"})] == [[102]]
    assert resolve.plan_marks(result, planned, {101: "d", 102: "d"}) == []
    signals = {int(t["test_result_id"]): codes for t, codes in ((tests[101], []),
                                                                 (tests[102], ["retry_passed"]))}
    merged = rerun.build_plan(rerun.ws.RunPaths(run_dir), run, picks, attempts=3, max_tests=20,
                              signals=signals)
    assert merged.tests[0].signals == ["retry_passed"]  # признак второго варианта не теряется
    assert surefire_filter(plan.tests) == f"{ORDER}#createOrder"

    # Локально воспроизводится ошибка второго варианта — это падение из CI, а не «иначе».
    skill_fake_maven.install(tmp_path, {f"{ORDER}#createOrder": ["failed:boom 2"]})
    files, job = _job(tmp_path, timeout=60, test=replace(plan.tests[0], module=""))
    item = execute(files, job)["tests"][0]
    assert (item["outcome"], item["runs"]) == ("reproduced", 3)


def test_module_logs_never_share_a_file(tmp_path: Path) -> None:
    files = JobFiles(tmp_path)

    assert files.maven_log(1, "a/b") != files.maven_log(1, "a_b")
    assert files.maven_log(1, "").name.startswith("attempt-1-root-")


# ---------------------------------------------------------------------------
# проверка правки после apply
# ---------------------------------------------------------------------------


def _applied(root: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[Path, str, str]:
    from test_skill_flow import PROPOSAL, TEST_ANALYSIS

    plain = "src/test/java/ru/company/orders/OrderTest.java"
    run_dir, run, _ = _prepare(root, capsys)
    order, login = f"{int(_problem_of(run, 101)):02d}", f"{int(_problem_of(run, 103)):02d}"
    (run_dir / "analyses" / f"{order}.md").write_text(TEST_ANALYSIS.replace(plain, source),
                                                      encoding="utf-8")
    assert _run(["next", str(run_dir)], capsys)[1].startswith("STATUS: propose")
    (run_dir / "proposals" / f"{order}.md").write_text(PROPOSAL.replace(plain, source),
                                                       encoding="utf-8")
    _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    diff = _run(["apply", order, "--run", str(run_dir)], capsys)[1]
    out = _run(["apply", order, "--run", str(run_dir), "--yes", "--diff",
                diff.split("--yes --diff ")[1].split()[0]], capsys)[1]
    assert out.startswith("STATUS: applied"), out
    return run_dir, order, out


def test_applied_fix_offers_to_rerun_the_problem_tests_with_one_yes(
    nested_project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    skill_fake_maven.install(nested_project / "autotests", {
        f"{ORDER}#createOrder": ["passed"], f"{ORDER}#updateOrder": [FAILED_500]})
    run_dir, order, out = _applied(nested_project, capsys,
                                   "autotests/src/test/java/ru/company/orders/OrderTest.java")

    offer = out.splitlines()[-1]
    assert offer.startswith(f"Проверить правку: 2 теста проблемы {int(order)} ({ORDER}.createOrder; "
                            f"{ORDER}.updateOrder) можно перезапустить на этой машине")
    assert "«Да» на правку сюда не относится" in offer and "Тесты сам не запускай." in offer
    command = offer.split("выполни: ", 1)[1].split(" — дальше", 1)[0]
    assert f"rerun {int(order)} --run {run_dir} --yes --plan " in command

    done = _run(["rerun", *command.split(" rerun ", 1)[1].split()], capsys)[1]
    done = _wait_done(run_dir, capsys) if done.startswith("STATUS: running") else done

    assert ("Прошли на изменённом коде теста — проверка правки, не нестабильность (1):\n"
            f"- Проблема {int(order)}: {ORDER}.createOrder — прошёл на запуске 1 из 3; правка "
            "агента применена") in done
    assert f"{ORDER}.updateOrder — упал 3 из 3" in done
    question = done.split(rerun.RESULT_END, 1)[1]
    assert "в базе знаний" not in question  # проверка правки в базу знаний не идёт
    assert (f"отметить успешными в TestOps (статус passed в прогоне 777) 1 тест "
            f"({ORDER}.createOrder) — прошёл на изменённом коде теста, в CI этого изменения ещё "
            "нет. На «нет» ничего не выполняй. Прежние «да» (на перезапуск, на правку) сюда не "
            "относятся.") in question  # один пункт — без оговорки о части

    saved = _run(["rerun", *_remember_command(done)], capsys)[1]
    assert saved.startswith("STATUS: saved\nОтмечены успешными в TestOps")
    assert testops.resolved[101]["message"].startswith(
        "alla: тест прошёл при перезапуске на машине инженера (запуск 1 из 3). Прошёл на "
        "изменённом коде теста (правка агента применена); в CI этого изменения ещё нет.")


def test_applied_fix_without_maven_asks_the_user_to_run_the_test(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, out = _applied(project, capsys, "src/test/java/ru/company/orders/OrderTest.java")

    assert out.splitlines()[-1] == "Тесты не запускай: предложи пользователю запустить исправленный тест."
