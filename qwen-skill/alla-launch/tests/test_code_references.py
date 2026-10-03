"""References with spaces extend, rather than replace, older CODE heuristics."""

from __future__ import annotations

from pathlib import Path

import pytest
from skill_fixtures import without_libmagic  # noqa: F401

from alla_skill_lib.analysis_format import code_ref_errors, parse_analysis


def _errors(root: Path, reference: str) -> list[str]:
    return code_ref_errors(parse_analysis(f"КОД: {reference}\n"), root)


@pytest.mark.parametrize(
    "reference",
    [
        "`src/My Tests/OrderTest.java:2` — метод create",
        '"src/My Tests/OrderTest.java":2 — метод create',
        "'src/My Tests/OrderTest.java:2' — метод create",
        "src/My Tests/OrderTest.java:2 — метод create",
        "см. `src/My Tests/OrderTest.java:2` — метод create",
    ],
)
def test_references_with_spaces_find_the_complete_path(tmp_path: Path, reference: str) -> None:
    target = tmp_path / "src" / "My Tests" / "OrderTest.java"
    target.parent.mkdir(parents=True)
    target.write_text("one\ntwo\n", encoding="utf-8")
    assert _errors(tmp_path, reference) == []


def test_existing_prefix_does_not_absorb_the_description(tmp_path: Path) -> None:
    target = tmp_path / "My Tests" / "OrderTest.java"
    target.parent.mkdir()
    target.write_text("one\ntwo\n", encoding="utf-8")
    assert _errors(tmp_path, "My Tests/OrderTest.java:2 метод create") == []


def test_legacy_prose_method_and_basename_references_still_work(tmp_path: Path) -> None:
    target = tmp_path / "src" / "test" / "OrderTest.java"
    target.parent.mkdir(parents=True)
    target.write_text("line\n" * 42, encoding="utf-8")
    assert _errors(tmp_path, "см. src/test/OrderTest.java:42") == []
    assert _errors(tmp_path, "OrderTest.java метод create") == []
    assert _errors(tmp_path, "метод orderApi.create() падает") == []


def test_quoted_fixture_in_method_call_is_not_treated_as_code_reference(tmp_path: Path) -> None:
    assert _errors(tmp_path, 'orderApi.create("orders.json") — загрузка фикстуры') == []


def test_quoted_missing_path_is_reported_without_losing_its_prefix(tmp_path: Path) -> None:
    errors = _errors(tmp_path, "`My Tests/Missing.java:1` — нет файла")
    assert errors and "My Tests/Missing.java" in errors[0]


def test_quoted_number_is_checked_against_the_complete_file(tmp_path: Path) -> None:
    target = tmp_path / "My Tests" / "OrderTest.java"
    target.parent.mkdir()
    target.write_text("one\n", encoding="utf-8")
    errors = _errors(tmp_path, '`My Tests/OrderTest.java`:42 — нет строки')
    assert errors and "строка 42 вне файла" in errors[0]


def test_quoted_basename_still_rejects_ambiguity(tmp_path: Path) -> None:
    for folder in ("a", "b"):
        target = tmp_path / folder / "My Test.java"
        target.parent.mkdir()
        target.write_text("one\n", encoding="utf-8")
    errors = _errors(tmp_path, '`My Test.java:1`')
    assert errors and "нескольких файлах" in errors[0]


@pytest.mark.parametrize("quoted", [False, True])
def test_existing_external_spaced_path_is_rejected(tmp_path: Path, quoted: bool) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "Private Tests" / "Secret.py"
    target.parent.mkdir()
    target.write_text("private\n", encoding="utf-8")
    reference = "../Private Tests/Secret.py:1"
    if quoted:
        reference = f"`{reference}`"
    errors = _errors(project, reference)
    assert errors and "../Private Tests/Secret.py" in errors[0]


def test_indexed_basename_cannot_follow_a_symlink_outside_the_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    external = tmp_path / "Secret.py"
    external.write_text("private\n", encoding="utf-8")
    (project / "src" / "Secret.py").symlink_to(external)
    assert _errors(project, "Secret.py:1")


def test_quoted_description_does_not_override_the_first_reference(tmp_path: Path) -> None:
    (tmp_path / "Real.java").write_text("line\n", encoding="utf-8")
    assert _errors(tmp_path, 'Real.java:1 — compare with "Other.java"') == []
    errors = _errors(tmp_path, 'Missing.java:1 — compare with "Real.java"')
    assert errors and "Missing.java" in errors[0]


def test_config_references_remain_readable(tmp_path: Path) -> None:
    (tmp_path / "playwright.config.ts").write_text("config\n", encoding="utf-8")
    assert _errors(tmp_path, "playwright.config.ts:1 — настройка") == []
