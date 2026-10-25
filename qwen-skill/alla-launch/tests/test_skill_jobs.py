"""Долгие команды фоновым заданием (``jobs.py``): Qwen обрывает команду shell через 120 с.

Здесь задания настоящие: дочерний процесс скрипта и фейковый TestOps по HTTP
(``fake_testops_server.py``). Подмены в памяти процесса (папка скилла, ``FakeTestOps.install``)
до дочернего процесса не доходят, поэтому остальные тесты идут синхронно (``skill_fixtures``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_testops_server import FakeTestOpsServer
from skill_fake_testops import TOKEN, FakeTestOps, default_launch
from skill_journal import save_main
from skill_fixtures import (  # noqa: F401
    LOGIN_TEST_JAVA,
    ORDER_TEST_JAVA,
    project_fixture,
    testops_fixture,
    without_libmagic,
)
from test_skill_flow import _run
from test_skill_review import _fake_qwen, _finished_run, _journal

from alla_skill_lib import cli, jobs
from alla_skill_lib import review_model
from alla_skill_lib import workspace as ws

LAUNCH = str(default_launch().launch["id"])


@pytest.fixture(name="autotests")
def autotests_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Проект автотестов без подмены папки скилла: её не увидел бы дочерний процесс."""
    root = tmp_path / "autotests"
    (root / ".git").mkdir(parents=True)
    java = root / "src" / "test" / "java" / "ru" / "company"
    for path, text in ((java / "orders" / "OrderTest.java", ORDER_TEST_JAVA),
                       (java / "auth" / "LoginTest.java", LOGIN_TEST_JAVA)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for name in ("QWEN_CODE_SESSION_ID", "QWEN_CODE_PROJECT_DIR", "QWEN_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALLURE_TOKEN", TOKEN)
    monkeypatch.setattr(cli, "RUN_IN_BACKGROUND", True)
    return root


def _server(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.0) -> Iterator[FakeTestOpsServer]:
    with FakeTestOpsServer(default_launch(), delay=delay) as server:
        monkeypatch.setenv("ALLURE_ENDPOINT", server.endpoint)
        yield server


@pytest.fixture(name="server")
def server_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTestOpsServer]:
    yield from _server(monkeypatch)


@pytest.fixture(name="slow_server")
def slow_server_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTestOpsServer]:
    yield from _server(monkeypatch, delay=0.3)


def _prepare(root: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str]:
    return _run(["prepare", LAUNCH, "--project-root", str(root), *extra], capsys)


def _slot(root: Path, key: str = f"prepare-{LAUNCH}") -> jobs.JobSlot:
    return jobs.JobSlot(root / ws.REPORTS_DIRNAME / jobs.JOBS_DIRNAME / key)


def _job(root: Path, key: str = f"prepare-{LAUNCH}") -> jobs.JobFiles:
    """Текущий запуск задания."""
    files = _slot(root, key).current()
    assert files is not None
    return files


def test_quick_prepare_answers_with_the_job_output(
    autotests: Path, server: FakeTestOpsServer, capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = _prepare(autotests, capsys)

    assert code == 0 and out.startswith("STATUS: analyze"), out
    run_dir = Path(next(line for line in out.splitlines()
                        if line.startswith("Папка разбора:")).split(":", 1)[1].strip())
    assert (run_dir / "run.json").is_file()
    assert _job(autotests).delivered.is_file()  # итог показан один раз
    code, out = _prepare(autotests, capsys)  # повтор — продолжение того же разбора
    assert code == 0 and "Продолжаю неоконченный разбор" in out and str(run_dir) in out


def test_slow_prepare_answers_running_and_the_repeat_attaches(
    autotests: Path, slow_server: FakeTestOpsServer, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs, "WAIT_BUDGET", 1.0)
    code, out = _prepare(autotests, capsys, "--fresh")

    assert code == 0 and out.startswith("STATUS: running"), out
    assert f"Прогон #{LAUNCH} большой: выгрузка из TestOps идёт в фоне" in out
    repeat = ws.skill_command("prepare", LAUNCH, "--project-root", str(autotests))
    assert out.rstrip().splitlines()[-1] == repeat  # команда — последней строкой, без --fresh
    assert "пока первая строка ответа — STATUS: running" in out
    pid = _job(autotests).pid.read_text(encoding="utf-8")

    _prepare(autotests, capsys, "--fresh")  # и с --fresh: подключается, а не запускает снова
    assert _job(autotests).pid.read_text(encoding="utf-8") == pid
    monkeypatch.setattr(jobs, "WAIT_BUDGET", 60.0)
    code, out = _prepare(autotests, capsys)
    assert code == 0 and out.startswith("STATUS: analyze"), out
    assert _job(autotests).pid.read_text(encoding="utf-8") == pid
    assert len(list((autotests / ws.REPORTS_DIRNAME).glob(f"{LAUNCH}-*"))) == 1


def test_every_waiting_call_gets_the_result_of_the_job(
    autotests: Path, server: FakeTestOpsServer, capsys: pytest.CaptureFixture[str],
) -> None:
    # Два вызова ждут одно задание: первый показал итог, второй — тоже, а не «без итога»
    # (ревью Codex).
    code, first = _prepare(autotests, capsys)
    assert code == 0 and first.startswith("STATUS: analyze"), first
    files = _job(autotests)
    assert jobs.job_state(files).state == "done" and not jobs.job_state(files).active

    code = cli._finish_job(files, time.monotonic(), lambda state: "идёт", "повтор")
    second = capsys.readouterr().out
    assert code == 0 and second == first

    # Третий вызов начал новое задание, а второй всё ещё ждёт прежнее: его итог цел.
    with jobs.start_lock(_slot(autotests)):
        newer = jobs.start(_slot(autotests), ["prepare", LAUNCH, "--fresh", "--project-root",
                                              str(autotests)])
    assert newer.root != files.root and _job(autotests) == newer
    code = cli._finish_job(files, time.monotonic(), lambda state: "идёт", "повтор")
    assert code == 0 and capsys.readouterr().out == first
    jobs.wait(newer, 60)


def test_relative_paths_of_the_environment_keep_their_meaning(
    autotests: Path, server: FakeTestOpsServer, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Сертификат прокси задан путём от папки, где запущена команда: задание видит тот же файл.
    import certifi

    (autotests / "proxy-ca.pem").write_bytes(Path(certifi.where()).read_bytes())
    monkeypatch.chdir(autotests)
    monkeypatch.setenv("SSL_CERT_FILE", "proxy-ca.pem")

    code, out = _prepare(autotests, capsys)

    assert code == 0 and out.startswith("STATUS: analyze"), out


def test_fetch_error_of_the_job_is_shown(
    autotests: Path, server: FakeTestOpsServer, capsys: pytest.CaptureFixture[str],
) -> None:
    server.fake.auth_status = 401

    code, out = _prepare(autotests, capsys)

    assert code == 1 and out.startswith("STATUS: error"), out
    assert f"Не удалось получить прогон #{LAUNCH}" in out


def test_job_that_died_without_a_result_is_an_error(
    autotests: Path, server: FakeTestOpsServer, capsys: pytest.CaptureFixture[str],
) -> None:
    slot = _slot(autotests)
    files = jobs.JobFiles(slot.root / "20261025000000-dead00")
    files.root.mkdir(parents=True)
    (slot.root / "current").write_text(files.root.name, encoding="utf-8")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    # Процесс задания завершился, а итога (exit) нет: оборвался.
    files.pid.write_text(f"{dead.pid} {time.time():.0f}", encoding="utf-8")

    code, out = _prepare(autotests, capsys)

    assert code == 1 and out.startswith("STATUS: error"), out
    assert "завершился без итога" in out and str(files.log) in out
    code, out = _prepare(autotests, capsys)  # следующий вызов начинает выгрузку заново
    assert code == 0 and out.startswith("STATUS: analyze"), out


def test_config_error_is_answered_without_a_job(
    autotests: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLURE_ENDPOINT", raising=False)
    monkeypatch.setattr(ws, "SKILL_DIR", autotests.parent / "skill")

    code, out = _prepare(autotests, capsys)

    assert code == 2 and out.startswith("STATUS: error") and "ALLURE_ENDPOINT" in out
    assert _slot(autotests).current() is None


def test_same_command_drops_fresh_and_job() -> None:
    assert cli._same_command(["prepare", "7", "--fresh", "--job", "/tmp/j"]) == \
        ws.skill_command("prepare", "7")


def test_review_with_model_runs_as_a_job(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    run_dir, done, qdir = _finished_run(project, capsys, monkeypatch, tmp_path)
    save_main(_journal(project, run_dir, done), qdir)
    script, log = _fake_qwen(tmp_path, "ok")
    monkeypatch.setenv(review_model.QWEN_ENV, str(script))
    monkeypatch.setattr(cli, "RUN_IN_BACKGROUND", True)

    code, out = _run(["review", "--run", str(run_dir)], capsys)

    assert code == 0 and out.startswith("STATUS: reviewed"), out
    assert json.loads((run_dir / "review" / "facts.json").read_text(encoding="utf-8"))[
        "assessment"]["problems"]
    assert _job(project, f"review-{run_dir.name}").delivered.is_file()
    assert log.is_file()


def test_review_repeat_names_its_run_folder(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # review без папки взял последний разбор; повтор после running — с этой папкой: .last_run
    # мог смениться новым prepare (ревью Codex).
    run_dir, done, qdir = _finished_run(project, capsys, monkeypatch, tmp_path)
    save_main(_journal(project, run_dir, done), qdir)
    script, _ = _fake_qwen(tmp_path, "ok")
    slow = tmp_path / "slow-qwen"
    slow.write_text(f"#!/bin/sh\nsleep 3\nexec {script} \"$@\"\n", encoding="utf-8")
    slow.chmod(0o755)
    monkeypatch.setenv(review_model.QWEN_ENV, str(slow))
    monkeypatch.setattr(cli, "RUN_IN_BACKGROUND", True)
    monkeypatch.setattr(jobs, "WAIT_BUDGET", 0.5)

    code, out = _run(["review", "--reports-dir", str(run_dir.parent)], capsys)

    assert code == 0 and out.startswith("STATUS: running"), out
    assert out.rstrip().splitlines()[-1] == ws.skill_command("review", "--run", str(run_dir))
    assert jobs.wait(_job(project, f"review-{run_dir.name}"), 60).state == "done"
