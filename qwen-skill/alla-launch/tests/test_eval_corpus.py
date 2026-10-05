"""Синтетический корпус эталона: детерминирован, разметка полна, наборы разделены."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest
import skill_fixtures  # noqa: F401  # scripts/ в sys.path
from skill_fake_testops import LaunchFixture

from eval import corpus_dev, corpus_holdout
from eval.corpus import LaunchBuilder, active_failure_ids, validate_labels

ALL_CASES = {**corpus_dev.CASES, **corpus_holdout.CASES}


def _dump(case: object) -> str:
    return json.dumps(asdict(case), default=bytes.hex, sort_keys=True)


@pytest.mark.parametrize("name", [name for name in ALL_CASES if name != "big_launch"])
def test_case_is_deterministic_and_fully_labeled(name: str) -> None:
    case = ALL_CASES[name]()

    assert case.name == name
    assert _dump(case) == _dump(ALL_CASES[name]())
    validate_labels(case.fixture, case.labels)
    ids = [result["id"] for result in case.fixture.results]
    assert len(ids) == len(set(ids))


def test_dev_and_holdout_do_not_share_cases_or_launches() -> None:
    assert not set(corpus_dev.CASES) & set(corpus_holdout.CASES)
    dev = {factory().fixture.launch["id"] for name, factory in corpus_dev.CASES.items()
           if name != "big_launch"}
    holdout = {factory().fixture.launch["id"] for factory in corpus_holdout.CASES.values()}
    assert not dev & holdout


def test_big_launch_is_heavy_and_large() -> None:
    case = corpus_dev.big_launch()

    assert case.heavy
    assert len(active_failure_ids(case.fixture)) >= 300
    assert min(len(content) for content in case.fixture.contents.values()) > 100_000


def test_retries_keep_hidden_attempts_out_of_labels() -> None:
    case = corpus_dev.retries()
    hidden = [result for result in case.fixture.results if result.get("hidden")]
    labeled = {test for group in case.labels["groups"] for test in group["tests"]}

    assert len(hidden) == 8
    assert not labeled & {result["id"] for result in hidden}
    assert all("historyId" in result for result in case.fixture.results)


def test_validate_labels_rejects_gaps_and_duplicates() -> None:
    builder = LaunchBuilder(1, "x")
    first = builder.add_failure("a", cause=None, name="a")
    builder.add_failure("b", cause=None, name="b")
    fixture: LaunchFixture = builder.build("x").fixture

    with pytest.raises(ValueError, match="без группы"):
        validate_labels(fixture, {"groups": [{"id": "a", "tests": [first]}]})
    with pytest.raises(ValueError, match="в группах"):
        validate_labels(fixture, {"groups": [{"id": "a", "tests": [first]},
                                             {"id": "b", "tests": [first]}]})
    with pytest.raises(ValueError, match="другая причина"):
        builder.add_failure("a", cause="known", name="c")


def test_validate_labels_rejects_duplicate_group_ids() -> None:
    builder = LaunchBuilder(1, "x")
    first = builder.add_failure("a", cause=None, name="a")
    second = builder.add_failure("b", cause=None, name="b")

    with pytest.raises(ValueError, match="повторяется id группы a"):
        validate_labels(builder.build("x").fixture, {"groups": [
            {"id": "a", "tests": [first]}, {"id": "a", "tests": [second]}]})
