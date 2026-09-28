"""Протокол ``STATUS:``, возобновление ``prepare``, ``skip``, ``check``, ``clean``, подсказки об ошибках."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
from skill_fake_testops import TOKEN, FakeTestOps, LaunchFixture, default_launch
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from test_skill_flow import MARKDOWN_ANALYSIS, VALID_ANALYSIS, _finish, _next, _prepare, _run

from alla_core.config import Settings
from alla_core.exceptions import AllureApiError, AuthenticationError, PaginationLimitError
from alla_skill_lib import cli
from alla_skill_lib.errors import fetch_error_hint


def _clusters(run: dict) -> tuple[str, str]:
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    return order, login


# --- каждый вывод начинается со STATUS ------------------------------------------


def test_next_output_names_the_launch(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, _, _ = _prepare(project, capsys)

    lines = _next(run_dir, capsys).splitlines()
    assert lines[0] == "STATUS: analyze"
    assert lines[1] == f"Прогон #777 «Regression nightly» · папка {run_dir}"

    code, out = _run(["next", "--project-root", str(project)], capsys)
    assert code == 0 and "взят последний разбор" in out
    code, out = _run(["next", "--run", str(run_dir), "--project-root", str(project)], capsys)
    assert code == 0 and "взят последний разбор" not in out


def test_argument_errors_start_with_status(project: Path, capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["prepare", "не-номер", "--project-root", str(project)])
    assert raised.value.code == 2
    out = capsys.readouterr().out
    assert out.startswith("STATUS: error") and "не вижу номера запуска" in out


def test_unexpected_exception_becomes_error_status(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def boom(argv: list[str] | None) -> int:
        raise RuntimeError("что-то сломалось")

    monkeypatch.setattr(cli, "_dispatch", boom)
    code, out = _run(["next"], capsys)
    assert code == 1
    assert out.startswith("STATUS: error") and "RuntimeError: что-то сломалось" in out
    assert "остановись" in out


def test_prepare_accepts_launch_url_and_hash(project: Path, testops: FakeTestOps, capsys) -> None:
    for reference in ("https://testops.example/project/5/launch/777/tree", "#777", "777"):
        code, out = _run(["prepare", reference, "--fresh", "--project-root", str(project)], capsys)
        assert code == 0 and out.startswith("STATUS: analyze"), (reference, out)
        assert "Прогон #777" in out


def test_prepare_progress_goes_to_stderr(project: Path, testops: FakeTestOps, capsys) -> None:
    code = cli.main(["prepare", "777", "--project-root", str(project)])
    captured = capsys.readouterr()
    assert code == 0 and captured.out.startswith("STATUS: ")
    assert "Получаю результаты прогона #777" in captured.err
    assert "Кластеризую 4 падений" in captured.err


# --- возобновление prepare -------------------------------------------------------


def test_prepare_resumes_unfinished_run(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, _ = _clusters(run)
    (run_dir / "analyses" / f"{order}.md").write_text(VALID_ANALYSIS, encoding="utf-8")
    requests_before = len(testops.requests)

    code, out = _run(["prepare", "777", "--project-root", str(project)], capsys)
    assert code == 0 and out.startswith("STATUS: analyze")
    assert "Продолжаю неоконченный разбор прогона #777" in out
    assert f"Папка разбора: {run_dir}" in out and "--fresh" in out
    assert len(testops.requests) == requests_before  # TestOps заново не опрашивался
    assert len(list((project / "alla-reports").glob("777-*"))) == 1

    code, out = _run(["prepare", "777", "--fresh", "--project-root", str(project)], capsys)
    assert code == 0 and "Продолжаю" not in out
    assert len(list((project / "alla-reports").glob("777-*"))) == 2


def test_prepare_starts_new_run_after_finished_or_stale(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = _clusters(run)
    _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: MARKDOWN_ANALYSIS})
    _, again, _ = _prepare(project, capsys)  # прошлый разбор закончен — новый
    assert len(list((project / "alla-reports").glob("777-*"))) == 2

    old = time.time() - 3 * 86400  # неоконченный, но давний — тоже новый
    for path in (project / "alla-reports").glob("777-*"):
        if not (path / "report.md").exists():
            os.utime(path / "run.json", (old, old))
    _prepare(project, capsys)
    assert len(list((project / "alla-reports").glob("777-*"))) == 3


# --- ошибки получения прогона ----------------------------------------------------


def test_empty_launch_is_an_error(project: Path, monkeypatch, capsys) -> None:
    empty = LaunchFixture(launch={"id": 780, "name": "Пустой", "projectId": 5}, results=[])
    FakeTestOps(empty).install(monkeypatch)
    code, out = _run(["prepare", "780", "--project-root", str(project)], capsys)
    assert code == 1
    assert out.startswith("STATUS: error") and "нет ни одного результата" in out
    assert not (project / "alla-reports").exists()


def test_bad_token_gets_a_hint(project: Path, monkeypatch, capsys) -> None:
    FakeTestOps(default_launch(), auth_status=401).install(monkeypatch)
    code, out = _run(["prepare", "777", "--project-root", str(project)], capsys)
    assert code == 1 and "Проверь ALLURE_TOKEN" in out and TOKEN not in out


def test_unknown_launch_gets_a_hint(project: Path, testops: FakeTestOps, capsys) -> None:
    code, out = _run(["prepare", "999", "--project-root", str(project)], capsys)
    assert code == 1 and "Запуск не найден" in out


def _settings() -> Settings:
    return Settings(endpoint="https://testops.example", token="t", max_pages=7, request_timeout=11)


def _wrapped(cause: BaseException) -> AllureApiError:
    error = AllureApiError(0, str(cause), "/api/launch/1")
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_wrapped(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] bad")), "ALLURE_SSL_VERIFY=false"),
        (_wrapped(httpx.ReadTimeout("slow")), "ALLURE_REQUEST_TIMEOUT (сейчас 11 с)"),
        (_wrapped(httpx.ConnectError("refused")), "Нет соединения с https://testops.example"),
        (PaginationLimitError("много"), "ALLURE_MAX_PAGES (сейчас 7)"),
        (AllureApiError(404, "нет", "/api/launch/1"), "Запуск не найден"),
        (AllureApiError(403, "нельзя", "/api/launch/1"), "Проверь ALLURE_TOKEN"),
        (AuthenticationError("HTTP 401"), "Проверь ALLURE_TOKEN"),
        (RuntimeError("непонятно"), ""),
    ],
)
def test_fetch_error_hints(exc: BaseException, expected: str) -> None:
    hint = fetch_error_hint(exc, _settings(), Path("/skill/.env"))
    assert (expected in hint) if expected else hint == ""


# --- зависшая модель, файлы модели, битые кластеры ---------------------------------


def test_unchanged_invalid_file_is_eventually_flagged(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, _ = _clusters(run)
    (run_dir / "analyses" / f"{order}.md").write_text("ПРИЧИНА: баг\nбез формата", encoding="utf-8")

    statuses = [_next(run_dir, capsys).splitlines()[0] for _ in range(5)]
    assert statuses[:4] == ["STATUS: fix"] * 4
    assert statuses[4] == "STATUS: analyze"  # третья засчитанная попытка — принято с пометкой


def test_model_file_with_bom_and_bad_bytes_does_not_crash(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, _ = _clusters(run)
    (run_dir / "analyses" / f"{order}.md").write_bytes(
        b"\xef\xbb\xbf" + VALID_ANALYSIS.encode("utf-8") + b"\n\xff\xfe\n"
    )
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: analyze")  # разбор принят, дальше — второй кластер


def test_broken_cluster_degrades_to_unknown(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def boom(**kwargs: object) -> str:
        raise ValueError("битый кластер")

    monkeypatch.setattr(cli, "build_cluster_task", boom)
    run_dir, run, out = _prepare(project, capsys)

    assert all(entry["auto"] for entry in run["clusters"])
    assert any("задание не подготовлено (ValueError: битый кластер)" in w for w in run["warnings"])
    assert "Внимание: Кластер 1: задание не подготовлено" in out
    assert out.startswith("STATUS: summary")
    first = (run_dir / "analyses" / f"{run['clusters'][0]['file_id']}.md").read_text(encoding="utf-8")
    assert "Не удалось подготовить данные этого кластера (ValueError)" in first


# --- skip ---------------------------------------------------------------------------


def test_skip_writes_stub_and_reports_it(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = _clusters(run)

    code, out = _run(["skip", str(int(order)), "--reason", "чужой сервис", "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: saved")
    stub = (run_dir / "analyses" / f"{order}.md").read_text(encoding="utf-8")
    assert "Причина пропуска: чужой сервис." in stub

    out = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    assert f"Пропущено без разбора по просьбе пользователя: {int(order)}." in out
    assert "### Замечания" in out

    auto = next(entry["file_id"] for entry in run["clusters"] if entry["auto"])
    code, out = _run(["skip", str(int(auto)), "--run", str(run_dir)], capsys)
    assert code == 1 and out.startswith("STATUS: error") and "разобрана автоматически" in out


def test_analyze_progress_counts_only_clusters_with_data(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, _, out = _prepare(project, capsys)
    assert "Готово разборов: 0 из 2 (ещё 1 без данных об ошибке разобраны автоматически)" in out


# --- check и clean ------------------------------------------------------------------


def test_check_reports_ready_without_leaking_token(project: Path, testops: FakeTestOps, capsys) -> None:
    code, out = _run(["check", "--project-root", str(project)], capsys)
    assert code == 0 and out.startswith("STATUS: ready")
    assert "ALLURE_ENDPOINT: https://testops.example" in out and "токен принят" in out
    assert TOKEN not in out
    assert not (project / "alla-reports").exists()  # check ничего не создаёт


def test_check_reports_missing_config_and_bad_token(project: Path, monkeypatch, capsys) -> None:
    FakeTestOps(default_launch(), auth_status=401).install(monkeypatch)
    code, out = _run(["check", "--project-root", str(project)], capsys)
    assert code == 1 and out.startswith("STATUS: error") and "Проверь ALLURE_TOKEN" in out

    monkeypatch.delenv("ALLURE_TOKEN")
    code, out = _run(["check", "--project-root", str(project)], capsys)
    assert code == 2 and "ALLURE_TOKEN" in out


def test_clean_removes_only_old_runs(project: Path, testops: FakeTestOps, capsys) -> None:
    old_dir, _, _ = _prepare(project, capsys, launch_id=777)
    reports = project / "alla-reports"
    (reports / "history.jsonl").write_text("{}\n", encoding="utf-8")
    fresh = reports / "555-20260101-000000"
    fresh.mkdir()
    (fresh / "run.json").write_text("{}", encoding="utf-8")
    long_ago = time.time() - 30 * 86400
    os.utime(old_dir / "run.json", (long_ago, long_ago))

    code, out = _run(["clean", "--dry-run", "--project-root", str(project)], capsys)
    assert code == 0 and "Будут удалены" in out and str(old_dir) in out
    assert old_dir.exists()

    code, out = _run(["clean", "--project-root", str(project)], capsys)
    assert code == 0 and "Удалены" in out
    assert not old_dir.exists() and fresh.exists() and (reports / "history.jsonl").exists()


# --- обёртка setup ---------------------------------------------------------------------


def test_setup_argument_split() -> None:
    import alla_skill

    assert alla_skill.split_setup_args([]) == (None, [])
    assert alla_skill.split_setup_args(["--python", "/opt/py311"]) == ("/opt/py311", [])
    assert alla_skill.split_setup_args(
        ["--python", "/opt/py311", "--", "--index-url", "https://mirror/simple"]
    ) == ("/opt/py311", ["--index-url", "https://mirror/simple"])
    assert alla_skill.split_setup_args(["--", "--proxy", "http://p:3128"]) == (
        None, ["--proxy", "http://p:3128"],
    )


def test_setup_creates_env_from_example_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import alla_skill

    monkeypatch.setattr(alla_skill, "SKILL_DIR", tmp_path)
    assert alla_skill.create_env_file() is None  # образца нет

    (tmp_path / ".env.example").write_text("ALLURE_ENDPOINT=\nALLURE_TOKEN=\n", encoding="utf-8")
    created = alla_skill.create_env_file()
    assert created == tmp_path / ".env" and created.read_text(encoding="utf-8").startswith("ALLURE_ENDPOINT=")
    if os.name != "nt":
        assert created.stat().st_mode & 0o077 == 0  # токен потом лежит только для владельца

    created.write_text("ALLURE_TOKEN=already-filled\n", encoding="utf-8")
    assert alla_skill.create_env_file() is None  # существующий .env не перезаписывается
    assert "already-filled" in created.read_text(encoding="utf-8")
