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


def test_missing_registry_checks_only_the_structure() -> None:
    analysis = parse_analysis(HEAD + "НАБЛЮДЕНИЯ:\n- [S7] «anything at all here»\n" + TAIL)
    assert validate_analysis(analysis, Path("."), task_format=2, sources=None) == []


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
