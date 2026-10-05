"""Эталон точности: метрики корпуса не хуже базовой линии ``tests/eval/baseline.json``.

Ухудшение допустимо только осознанно: обновите базовую линию в том же коммите
(``run_eval.py --write-baseline``) и объясните изменение цифр в сообщении коммита.
Базовая линия holdout обновляется отдельным коммитом.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import skill_fixtures  # noqa: F401  # scripts/ в sys.path

from eval import run_eval

TOLERANCE = 0.001
UPDATE_HINT = (
    "Если изменение осознанное — `.venv/bin/python qwen-skill/alla-launch/tests/eval/"
    "run_eval.py --write-baseline` и цифры в сообщение коммита (подробности — `--details`)."
)


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, dict[str, Any]]]:
    return run_eval.evaluate_cases(heavy=True)


def _baseline() -> dict[str, dict[str, dict[str, Any]]]:
    data: dict[str, dict[str, dict[str, Any]]] = json.loads(
        run_eval.BASELINE.read_text(encoding="utf-8"))
    return data


def test_baseline_covers_the_corpus(results: dict[str, dict[str, dict[str, Any]]]) -> None:
    baseline = _baseline()

    assert {name: sorted(cases) for name, cases in results.items()} == {
        name: sorted(cases) for name, cases in baseline.items()
    }, "Корпус изменился. " + UPDATE_HINT
    for set_name, cases in results.items():
        for case_name, result in cases.items():
            expected = baseline[set_name][case_name]
            assert (result["tests"], result["groups"], result["evidence_total"]) == (
                expected["tests"], expected["groups"], expected["evidence_total"]
            ), f"{set_name}/{case_name}: сценарий или разметка изменились. " + UPDATE_HINT


def test_quality_is_not_worse_than_baseline(
    results: dict[str, dict[str, dict[str, Any]]],
) -> None:
    baseline = _baseline()
    worse: list[str] = []
    for set_name, cases in results.items():
        for case_name, result in cases.items():
            expected = baseline[set_name][case_name]
            where = f"{set_name}/{case_name}"
            for key in ("precision", "recall"):
                if result[key] < expected[key] - TOLERANCE:
                    worse.append(f"{where}: {key} {expected[key]} → {result[key]}")
            for key in ("hidden_groups", "evidence_lost", "retry_links_wrong",
                        "passed_after_retry_wrong"):
                if result.get(key, 0) > expected.get(key, 0):
                    worse.append(f"{where}: {key} {expected.get(key, 0)} → {result[key]}")
            for key in ("retry_links_found", "retry_same_found", "passed_after_retry_found"):
                if result.get(key, 0) < expected.get(key, 0):
                    worse.append(f"{where}: {key} {expected[key]} → {result.get(key, 0)}")
    assert not worse, "Эталон ухудшился:\n" + "\n".join(worse) + "\n" + UPDATE_HINT
