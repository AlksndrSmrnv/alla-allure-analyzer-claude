"""Наблюдения разбора: цитаты сверяются со своим куском данных из реестра."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fake_testops import FakeTestOps
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from test_skill_flow import MARKDOWN_ANALYSIS, VALID_ANALYSIS, _next, _prepare, _run

from alla_skill_lib.analysis_format import (
    EXPECTED_FORMAT,
    LEGACY_EXPECTED_FORMAT,
    parse_analysis,
    quote_found,
    validate_analysis,
)

SOURCES = {
    "S1": {"kind": "message", "text": "expected: <200> but was: <500>"},
    "S2": {"kind": "trace", "text": "java.lang.AssertionError: expected: <200>\n\tat a.B.c(B.java:6)"},
    "S3": {"kind": "log", "text": (
        "2026-09-01 10:00:01 [ERROR] OrderService: failed to create order\n"
        "java.lang.NullPointerException: customer is null\n"
        'Caused by: constraint "uk_profile_email" violated')},
}
HEAD = "ЧТО СЛОМАЛОСЬ: Тест получил 500.\nПРИЧИНА: приложение — NPE в сервисе.\n"
TAIL = "КАК ИСПРАВИТЬ:\n1. Добавить проверку customer.\n"


def _errors(observations: str, *, head: str = HEAD, missing: str = "") -> list[str]:
    text = head + "НАБЛЮДЕНИЯ:\n" + observations + missing + TAIL
    return validate_analysis(parse_analysis(text), Path("."), task_format=2, sources=SOURCES)


@pytest.mark.parametrize("line", [
    "- [S3] «java.lang.NullPointerException: customer is null»",
    "- [S3] \"java.lang.nullpointerexception:   customer is null\"",  # регистр и пробелы
    "* S3: «NullPointerException: customer is null» — сервис не проверяет customer",
    "1. [s3] “[ERROR] OrderService: failed … customer is null”",
    "- [S3] «constraint «uk_profile_email» violated»",  # кавычки внутри цитаты
    "- [S3] «failed to create order.»",  # точка в конце цитаты
])
def test_accepted_observation_forms(line: str) -> None:
    assert _errors(line + "\n") == []


@pytest.mark.parametrize(("line", "message"), [
    ("- [S1] «customer is null»", "цитата «customer is null» есть в S3, а не в S1 — укажи [S3]"),
    ("- [S3] «Connection pool exhausted»", "цитаты «Connection pool exhausted» нет в S3"),
    ("- [S9] «customer is null»", "источника S9 нет в задании (есть S1, S2, S3)"),
    ("- [S3] «is null»", "слишком короткая"),
    ("- S3: customer is null", "не в формате «- [S3] «дословная цитата»»"),
    ("- [S3] «customer is … failed to create»", "нет в S3"),  # части не по порядку
])
def test_rejected_observations_name_the_quote(line: str, message: str) -> None:
    errors = _errors(line + "\n")
    assert any(message in error for error in errors), errors


def test_observations_are_required_unless_the_cause_is_unknown() -> None:
    text = HEAD + TAIL
    assert any("нет раздела «НАБЛЮДЕНИЯ:»" in error for error in validate_analysis(
        parse_analysis(text), Path("."), task_format=2, sources=SOURCES))

    unknown = "ЧТО СЛОМАЛОСЬ: Тест получил 500.\nПРИЧИНА: неизвестно — данных мало.\n"
    errors = validate_analysis(parse_analysis(unknown + "НЕ ХВАТАЕТ: нет\n" + TAIL), Path("."),
                               task_format=2, sources=SOURCES)
    assert any("при категории «неизвестно» в «НЕ ХВАТАЕТ:»" in error for error in errors)
    ok = unknown + "НЕ ХВАТАЕТ: лога сервиса заказов за время теста.\n" + TAIL
    assert validate_analysis(parse_analysis(ok), Path("."), task_format=2, sources=SOURCES) == []


def test_legacy_format_needs_no_observations() -> None:
    assert validate_analysis(parse_analysis(HEAD + TAIL), Path("."), task_format=1) == []
    assert "НАБЛЮДЕНИЯ" in EXPECTED_FORMAT and "НАБЛЮДЕНИЯ" not in LEGACY_EXPECTED_FORMAT


def test_missing_registry_rejects_observations_of_the_new_format() -> None:
    analysis = parse_analysis(HEAD + "НАБЛЮДЕНИЯ:\n- [S999] «anything at all here»\n" + TAIL)
    errors = validate_analysis(analysis, Path("."), task_format=2, sources=None)
    assert any("реестр источников этого кластера" in error and "Проблемы скилла" in error
               for error in errors)


@pytest.mark.parametrize("content", [None, "{не json", "[]", '{"S1": "текст"}',
                                     '{"S1": {"kind": "message"}}'])
def test_next_and_verify_reject_quotes_when_the_registry_is_broken(
    content: str | None, project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order = run["clusters"][0]["file_id"]
    registry = run_dir / "evidence" / f"{order}.sources.json"
    if content is None:
        registry.unlink()
    else:
        registry.write_text(content, encoding="utf-8")
    fake = VALID_ANALYSIS.replace("- [S3] «java.lang", "- [S999] «java.lang")
    (run_dir / "analyses" / f"{order}.md").write_text(fake, encoding="utf-8")

    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: fix") and "реестр источников этого кластера" in out
    _code, verify = _run(["verify", order, "--run", str(run_dir)], capsys)
    assert verify.startswith("STATUS: fix")


def test_quote_parts_must_follow_in_order() -> None:
    text = "alpha beta gamma delta"
    assert quote_found("alpha … gamma", text)
    assert quote_found("alpha...delta", text)
    assert not quote_found("gamma … alpha", text)


def test_next_and_verify_reject_a_quote_from_another_source(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order = run["clusters"][0]["file_id"]
    assert run["clusters"][0]["task_format"] == 2
    assert all("task_format" not in entry for entry in run["clusters"] if entry["auto"])
    wrong = VALID_ANALYSIS.replace("- [S3] «java.lang.NullPointerException",
                                   "- [S1] «java.lang.NullPointerException")
    (run_dir / "analyses" / f"{order}.md").write_text(wrong, encoding="utf-8")

    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: fix")
    assert "есть в S3, а не в S1 — укажи [S3]" in out
    assert "НАБЛЮДЕНИЯ: 2" in out and EXPECTED_FORMAT in out

    code, verify = _run(["verify", order, "--run", str(run_dir)], capsys)
    assert code == 0 and verify.startswith("STATUS: fix") and "а не в S1" in verify


def test_remember_from_analysis_checks_quotes(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    for file_id, text in ((order, VALID_ANALYSIS), (login, MARKDOWN_ANALYSIS)):
        (run_dir / "analyses" / f"{file_id}.md").write_text(text, encoding="utf-8")
    (run_dir / "summary.md").write_text("Итог.", encoding="utf-8")
    _next(run_dir, capsys)
    _next(run_dir, capsys)
    # Разбор поправили после отчёта — цитата больше не из своего куска.
    (run_dir / "analyses" / f"{order}.md").write_text(
        VALID_ANALYSIS.replace("«java.lang.NullPointerException: customer is null»",
                               "«pool exhausted somewhere»"), encoding="utf-8")

    code, out = _run(["remember", order, "--run", str(run_dir), "--from-analysis"], capsys)
    assert out.startswith("STATUS: fix") and "не прошёл проверку формата" in out


def test_registry_is_what_the_task_shows(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order = run["clusters"][0]["file_id"]
    task = (run_dir / "clusters" / f"{order}.md").read_text(encoding="utf-8")
    sources = json.loads((run_dir / "evidence" / f"{order}.sources.json").read_text("utf-8"))

    for record in sources.values():
        assert record["text"] in task
    assert "НАБЛЮДЕНИЯ:" in task and "НЕ ХВАТАЕТ:" in task


def _done(project: Path, capsys: pytest.CaptureFixture[str]) -> tuple[Path, str, str]:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    login_text = MARKDOWN_ANALYSIS.replace(
        "**Как исправить:**",
        "НЕ ХВАТАЕТ: лога auth-service за время теста.\n\n**Как исправить:**")
    for file_id, text in ((order, VALID_ANALYSIS), (login, login_text)):
        (run_dir / "analyses" / f"{file_id}.md").write_text(text, encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: summary")
    summary_task = (run_dir / "summary_task.md").read_text(encoding="utf-8")
    (run_dir / "summary.md").write_text("Итог прогона.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done")
    brief = out.split("===ОТЧЁТ===\n", 1)[1].split("\n===КОНЕЦ===", 1)[0]
    return run_dir, brief, summary_task


def test_report_separates_observations_from_the_presumed_cause(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, brief, summary_task = _done(project, capsys)
    report = (run_dir / "report.md").read_text(encoding="utf-8")

    # Цитаты — с понятным источником: тест, вложение, строки.
    assert "- Наблюдения:\n   - «java.lang.NullPointerException: customer is null» — лог app.log, " \
           "строки 2–4, тест createOrder" in report
    assert "   - «expected: <200> but was: <500>» — сообщение об ошибке, тест createOrder" in report
    assert "- **Наблюдения:**" in report and "- **Не хватает:** лога auth-service" in report
    assert "- Не хватает: лога auth-service за время теста." in report
    assert "Не хватает: нет" not in report  # «нет» не показывается
    # «окружение» только по сообщению теста — пометка; у ошибки приложения есть цитата из лога.
    assert "не принимает соединения. (причина не подтверждена логом)" in report
    assert report.count("(причина не подтверждена логом)") == 2  # раздел и подробности
    assert "(причина не подтверждена логом)" in summary_task
    # В терминале — одна короткая метка, без цитат.
    login_line = next(line for line in brief.splitlines() if "не подтверждено логом" in line)
    assert login_line.startswith("**Проблема ") and login_line.endswith("[не подтверждено логом]")
    assert brief.count("не подтверждено логом") == 1
    assert "Наблюдения" not in brief and "«java.lang.NullPointerException" not in brief


def test_legacy_run_report_has_no_observations(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from alla_skill_lib import workspace
    from eval.legacy import restore_legacy_run

    run_dir = restore_legacy_run(project, workspace.SKILL_DIR, workspace.ENTRYPOINT)
    (run_dir / "analyses" / "03.md").write_text(MARKDOWN_ANALYSIS, encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: summary")
    (run_dir / "summary.md").write_text("Итог.", encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: done")
    report = (run_dir / "report.md").read_text(encoding="utf-8")

    # Старый разбор: реестра нет — ни подписей источников, ни пометки о логе.
    assert "Наблюдения" not in report and "не подтверждена логом" not in report


# --- регрессии по чтению инструкций глазами исполнителя ---------------------------


@pytest.mark.parametrize(("line", "quote"), [
    ("- [S2] «at a.B.c(B.java:6)» — проверка в `createOrder`", "at a.B.c(B.java:6)"),
    ('- [S3] «constraint «uk_profile_email» violated» — ключ "uk"',
     "constraint «uk_profile_email» violated"),
    ("- **[S3]** «customer is null»", "customer is null"),
    ("- [S3] «error: x — y» — пояснение", "error: x — y"),
    ('- [S3] «key "id" — missing required value»', 'key "id" — missing required value'),
    ("- [S3] «customer is null» (поле «customer» отсутствует)", "customer is null"),
    ('- [S3] "key "id" — missing required value"', 'key "id" — missing required value'),
    ('- [S3] "key "id" — missing" — note "x"', 'key "id" — missing'),
    ("- [S2] `at a.B.c` — проверка в `createOrder`", "at a.B.c"),
    ("- [S1] 'can't connect' - see log", "can't connect"),
    ("- [S1] 'can't connect' — ошибка в 'login'", "can't connect"),
    ("- [S1] 'it's the user's fault' — x", "it's the user's fault"),
    ('- [S3] "value \\"id\\" missing" — note', 'value \\"id\\" missing'),
    ('- [S1] "missing directory C:\\\\logs\\\\"', 'missing directory C:\\\\logs\\\\'),
    ('- [S1] "path C:\\\\logs\\\\" — нет каталога', 'path C:\\\\logs\\\\'),
    ('- [S1] "a \\\\\\"b\\" c" — x', 'a \\\\\\"b\\" c'),
])
def test_comment_after_the_quote_is_not_part_of_it(line: str, quote: str) -> None:
    observation, = parse_analysis(HEAD + "НАБЛЮДЕНИЯ:\n" + line + "\n" + TAIL).observations
    assert observation.quote == quote


@pytest.mark.parametrize("written", ["НАБЛЮДЕНИЯ: нет\n", "НАБЛЮДЕНИЯ:\n- нет\n", "НАБЛЮДЕНИЯ:\n-\n"])
def test_unknown_cause_with_no_observations_written_as_none(written: str) -> None:
    text = ("ЧТО СЛОМАЛОСЬ: Тест упал.\nПРИЧИНА: неизвестно — данных мало.\n" + written
            + "НЕ ХВАТАЕТ: лога сервиса за время теста.\n" + TAIL)
    analysis = parse_analysis(text)
    assert analysis.observations == [] and analysis.bad_observations == []
    assert validate_analysis(analysis, Path("."), task_format=2, sources=SOURCES) == []


def test_verify_names_the_cluster_task_and_remember_lists_the_reasons(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    bad = VALID_ANALYSIS.replace("«java.lang.NullPointerException: customer is null»",
                                 "«pool exhausted somewhere»")
    (run_dir / "analyses" / f"{order}.md").write_text(bad, encoding="utf-8")

    _code, verify = _run(["verify", order, "--run", str(run_dir)], capsys)
    assert f"Задание кластера (данные и куски S…): {run_dir / 'clusters' / f'{order}.md'}" in verify

    _code, out = _run(["remember", order, "--run", str(run_dir), "--from-analysis"], capsys)
    assert "цитаты «pool exhausted somewhere» нет в S3" in out
    assert "analyses/NN.md не переписывай: спроси пользователя причину и рецепт" in out


def test_what_is_missing_reaches_the_summary_and_refreshes_it(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, _brief, summary_task = _done(project, capsys)
    assert "НЕ ХВАТАЕТ: лога auth-service за время теста." in summary_task
    assert "НЕ ХВАТАЕТ: нет" not in summary_task

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]][1]
    path = run_dir / "analyses" / f"{login}.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "лога auth-service за время теста.", "адреса auth-service в конфигурации тестов."),
        encoding="utf-8")
    # Поменялось только «НЕ ХВАТАЕТ» — сводка устарела и запрашивается заново.
    assert _next(run_dir, capsys).startswith("STATUS: summary")
    assert "адреса auth-service в конфигурации" in (run_dir / "summary_task.md").read_text("utf-8")
