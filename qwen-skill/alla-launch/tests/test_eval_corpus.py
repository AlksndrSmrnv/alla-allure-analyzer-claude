"""Синтетический корпус эталона: детерминирован, разметка полна, наборы разделены."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest
import skill_fixtures  # noqa: F401  # scripts/ в sys.path
from skill_fake_testops import LaunchFixture

from eval import corpus_dev, corpus_holdout, journal
from eval.corpus import LaunchBuilder, active_failure_ids, validate_labels
from eval.metrics import normalize_space

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


def _raw_text(fixture: LaunchFixture, test_id: int) -> str:
    """Всё, что TestOps отдаёт о результате: сообщение, трейс, шаги, детали, вложения."""
    result = next(result for result in fixture.results if result["id"] == test_id)
    parts = [json.dumps(result, ensure_ascii=False)]
    parts += [json.dumps(step, ensure_ascii=False) for step in fixture.executions.get(test_id, [])]
    if test_id in fixture.details:
        parts.append(json.dumps(fixture.details[test_id], ensure_ascii=False))
    parts += [fixture.contents[attachment["id"]].decode("utf-8")
              for attachment in fixture.attachments.get(test_id, [])]
    # Сообщение и трейс в JSON экранированы: сравниваем и с исходными строками.
    details = result.get("statusDetails") or {}
    parts += [str(value) for value in details.values()]
    return "\n".join(parts)


@pytest.mark.parametrize("name", list(ALL_CASES))
def test_evidence_is_in_the_raw_data_of_its_group(name: str) -> None:
    """Строка ``evidence`` дословно (с точностью до пробелов) есть в данных теста группы.

    Опечатка в разметке ловится без прогона ``prepare``: новый holdout проверяется так до
    первого прогона и после него не правится (README, «Правило работы с holdout»).
    """
    case = ALL_CASES[name]()
    missing = [
        f"{group['id']}: {line}"
        for group in case.labels["groups"] for line in group.get("evidence") or []
        if not any(normalize_space(line) in normalize_space(_raw_text(case.fixture, test))
                   for test in group["tests"])
    ]
    assert not missing


def test_holdout_changes_are_journaled() -> None:
    """Сценарии и базовая линия holdout меняются только с записью в журнале просмотров."""
    last = journal.last_entry_digests()

    assert last.get("корпус") == journal.corpus_digest(), (
        "Сценарии holdout изменились без записи в holdout_journal.md (README, «Правило "
        "работы с holdout»); отпечатки — `python tests/eval/journal.py`.")
    assert last.get("базовая линия") == journal.baseline_digest(), (
        "Базовая линия holdout изменилась без записи в holdout_journal.md.")


def test_journal_digests_follow_the_last_entry() -> None:
    text = ("- корпус: `aaaa`\n- базовая линия: `bbbb`\n## следующая\n"
            "- корпус: `cccc`\n- базовая линия: `dddd`\n")

    assert journal.last_entry_digests(text) == {"корпус": "cccc", "базовая линия": "dddd"}
    assert journal.baseline_digest({"dev": {"x": 1}}) == journal.baseline_digest({})
    assert journal.baseline_digest({"holdout": {"x": 1}}) != journal.baseline_digest({})


def test_builder_attaches_several_logs() -> None:
    builder = LaunchBuilder(1, "x")
    test = builder.add_failure("a", cause=None, name="a", log="plain", log_name="app.log",
                               logs=[("events.json", "[]", "application/json")])
    fixture = builder.build("x").fixture

    assert [(item["name"], item["type"]) for item in fixture.attachments[test]] == [
        ("app.log", "text/plain"), ("events.json", "application/json")]
    assert [fixture.contents[item["id"]] for item in fixture.attachments[test]] == [
        b"plain", b"[]"]
