"""Перезапуск тестов локально (``rerun``): план, Maven фоновым заданием, ход и итог.

Maven — фейковый ``mvnw`` проекта (``skill_fake_maven``): он пишет отчёты surefire так же,
как настоящий (``TEST-<класс>-<суффикс>.xml``, ``classname`` с суффиксом в скобках).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import skill_fake_maven
from skill_fake_testops import FakeTestOps, default_launch
from skill_fixtures import (  # noqa: F401
    multimodule_project_fixture,
    nested_project_fixture,
    project_fixture,
    testops_fixture,
    without_libmagic,
)
from test_skill_flow import MARKDOWN_ANALYSIS, _finish, _prepare, _run

from alla_skill_lib import flaky, rerun
from alla_skill_lib.rerun import (
    JobFiles,
    PlannedTest,
    method_result,
    read_reports,
    same_error,
    surefire_filter,
    worker_env,
)
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
    """Прогон по умолчанию, где у createOrder повтор упал иначе, а у updateOrder — прошёл."""
    launch = default_launch()
    by_id = {result["id"]: result for result in launch.results}
    by_id[101]["historyId"] = "h-create"
    by_id[102]["historyId"] = "h-update"
    by_id[106].update(historyId="h-create",
                      statusDetails={"message": "java.net.SocketTimeoutException: Read timed out"})
    launch.results.append({"id": 109, "name": "updateOrder retry", "status": "passed",
                           "hidden": True, "historyId": "h-update"})
    fake = FakeTestOps(launch)
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
    logs = run_dir / "rerun" / "logs"
    assert sorted(path.name for path in logs.iterdir()) == [
        "attempt-1-autotests.log", "attempt-2-autotests.log", "attempt-3-autotests.log"]
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
    log = (run_dir / "rerun" / "logs" / "attempt-1-autotests.log").read_text(encoding="utf-8")
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
    assert _rerun(run_dir, capsys, "2").startswith("STATUS: running")  # нового не начинает

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

    cases = read_reports(tmp_path, "alla-j-1", started=time.time())

    assert method_result(cases, _planned("param")) == rerun.CaseResult("failed", "boom 2")
    assert method_result(cases, _planned("flaky")) == rerun.CaseResult("passed", flaky=True)
    assert method_result(cases, _planned("always")) == rerun.CaseResult("failed", "again")
    assert method_result(cases, _planned("off")) == rerun.CaseResult("skipped")
    assert method_result(cases, _planned("missing")) is None


def test_reports_without_our_suffix_are_taken_by_time(tmp_path: Path) -> None:
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-ru.company.orders.OrderTest.xml").write_text(
        '<testsuite><testcase name="flaky" classname="ru.company.orders.OrderTest"/></testsuite>',
        encoding="utf-8")

    assert read_reports(tmp_path, "alla-j-1", started=time.time() - 10)
    assert not read_reports(tmp_path, "alla-j-1", started=time.time() + 10)


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
    files = JobFiles(tmp_path / "rerun")
    files.root.mkdir()
    group = rerun.MavenGroup(module="", cwd=str(tmp_path), tool=[str(tmp_path / "mvnw")],
                             select=[], fallback_cwd="")
    plan = {"job": "j", "attempts": 3, "timeout": 1, "maven_args": [],
            "tests": [vars(_planned("a"))], "groups": [vars(group)]}

    started = time.monotonic()
    result = execute(files, plan)

    assert time.monotonic() - started < 20  # Maven остановлен, а не дождались его 30 с
    assert result["timed_out"] is True
    assert [item["reason"] for item in result["tests"]] == ["не уложился в потолок времени"]


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
    assert "если в этом сеансе он уже ответил на этот вопрос — не спрашивай снова" in offer
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

    assert f"Итог добавлен в полный разбор: {run_dir / 'report.md'}" in out
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
