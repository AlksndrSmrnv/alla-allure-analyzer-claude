"""Повторы (шаг 5) в задании кластера и в отчёте: факты, а не диагноз."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skill_fixtures import project_fixture, without_libmagic  # noqa: F401
from eval import corpus_dev
from skill_fake_testops import FakeTestOps
from alla_skill_lib.report import render_green_report
from alla_skill_lib.retries import (
    MAX_REPORT_PASSED,
    passed_after_retry,
    render_task_section,
    report_line,
    retry_facts,
)
from alla_skill_lib.workspace import RunPaths
from test_skill_flow import _prepare
from test_skill_report import APP, ENV, _render, _run, _section

SAME = {"status": "failed", "message": "Total 0", "same_as_final": True}
OTHER = {"status": "broken", "message": "ConnectException: refused", "same_as_final": False}
UNKNOWN = {"status": "failed", "message": None, "same_as_final": None}


def test_facts_put_each_retried_test_in_one_group() -> None:
    facts = retry_facts([[SAME, SAME], [SAME, OTHER], [{"status": "passed"}], [UNKNOWN], []])

    assert (facts.tests, facts.retried, facts.attempts) == (5, 4, 5)
    assert (facts.same, facts.different, facts.passed, facts.unknown) == (1, 1, 1, 1)
    assert facts.different_messages == ["ConnectException: refused"]


def test_task_section_states_facts_and_that_retries_are_not_a_cause() -> None:
    lines = render_task_section(retry_facts([[SAME, SAME]] * 4 + [[OTHER], [], []]))
    text = "\n".join(lines)

    assert lines[0] == "--- Повторы в TestOps ---"
    assert "Повторы были у 5 из 7 тестов (неудачных попыток до финальной: 9):" in text
    assert "- у 4 тестов все попытки упали с той же ошибкой;" in text
    assert "- у 1 теста есть попытка с другой ошибкой: «ConnectException: refused»." in text
    assert "причину не устанавливают" in text and "не довод ни за одну категорию" in text
    assert "не выдавай её ошибку за причину" in text
    assert "не цитируй" in text


def test_task_section_for_one_test_and_without_different_attempts() -> None:
    text = "\n".join(render_task_section(retry_facts([[SAME, SAME]])))

    assert "Тест запускался повторно (неудачных попыток до финальной: 2):" in text
    assert "- все попытки упали с той же ошибкой." in text
    assert "другой ошибкой" not in text


def test_no_retries_no_section() -> None:
    facts = retry_facts([[], []])
    assert render_task_section(facts) == [] and report_line(facts) is None


def test_different_messages_are_limited() -> None:
    attempts = [{**OTHER, "message": f"error {index}"} for index in range(4)]
    line = report_line(retry_facts([attempts]))

    assert line is not None and "«error 0», «error 1» и ещё 2" in line


@pytest.fixture(name="retries_testops")
def retries_testops_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeTestOps:
    fake = FakeTestOps(corpus_dev.retries().fixture)
    fake.install(monkeypatch)
    return fake


def test_prepare_puts_retries_into_the_cluster_task(
    project: Path, retries_testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, run, out = _prepare(project, capsys, launch_id=5111)

    tasks = {
        entry["label"]: (run_dir / "clusters" / f"{entry['file_id']}.md").read_text(encoding="utf-8")
        for entry in run["clusters"]
    }
    removed = next(text for label, text in tasks.items() if "SKU-9" in label)
    total = next(text for label, text in tasks.items() if "Cart total" in label)
    checkout = next(text for label, text in tasks.items() if "Checkout" in label)
    assert "есть попытка с другой ошибкой: «java.net.ConnectException: Connection refused" in removed
    assert "- все попытки упали с той же ошибкой." in total
    assert "Повторы в TestOps" not in checkout  # другое окружение — не повтор
    assert run["triage"]["retries"]["passed_after_retry"][0]["name"] == "addItem"
    assert "Внимание: Не удалось связать 1 из 8 скрытых попыток" in out
    # Попытки не меняют ни кластеры, ни сигнатуру: у кластеров нет следов попыток.
    assert all("1006" not in str(entry["signature"]) for entry in run["clusters"])


def _with_retries(run: dict[str, Any], passed: int = 0) -> dict[str, Any]:
    tests = run["triage"]["failed_tests"]
    tests[0]["attempts"] = [SAME, SAME]
    tests[1]["attempts"] = [OTHER]
    run["triage"]["retries"] = {"linked_by": "historyId", "passed_after_retry": [
        {"test_result_id": 900 + index, "name": f"flaky_{index}",
         "link": f"https://testops.example/testresult/{900 + index}", "failed_attempts": 1,
         "message": "expected: <3> but was: <2>"}
        for index in range(passed)
    ]}
    return run


def test_report_shows_retries_and_tests_passed_after_retry(tmp_path: Path) -> None:
    run = _with_retries(_run([3, 1]), passed=MAX_REPORT_PASSED + 2)

    console, full = _render(tmp_path, run, [APP, ENV])

    assert f"Прошли после повтора: {MAX_REPORT_PASSED + 2} (список в полном разборе)" in console
    assert "flaky_0" not in console  # список только в report.md
    passed = _section(full, f"Прошли после повтора ({MAX_REPORT_PASSED + 2})")
    assert "- [flaky_0](https://testops.example/testresult/900) — 1 неудачная попытка: " \
           "«expected: <3> but was: <2>»" in passed
    assert passed.count("[flaky_") == MAX_REPORT_PASSED and "- … и ещё 2" in passed
    assert ("- **Повторы:** Повторы были у 2 из 3 тестов (неудачных попыток до финальной: 3): "
            "у 1 теста все попытки упали с той же ошибкой; у 1 теста есть попытка с другой "
            "ошибкой: «ConnectException: refused».") in full


def test_report_without_retries_has_no_retry_lines(tmp_path: Path) -> None:
    run = _run([2])
    del run["triage"]["retries"]  # папка разбора до шага 5

    console, full = _render(tmp_path, run, [APP])

    assert "повтор" not in console.lower() and "Повторы" not in full
    assert passed_after_retry(run) == []


def test_green_report_lists_tests_passed_after_retry(tmp_path: Path) -> None:
    run = _with_retries(_run([1, 1]), passed=1)
    run["clusters"] = []

    console, text = render_green_report(run, RunPaths(tmp_path))

    assert "Прошли после повтора: 1 — в разбор не входят, список ниже." in text
    assert "### Прошли после повтора (1)" in console and "[flaky_0]" in text
