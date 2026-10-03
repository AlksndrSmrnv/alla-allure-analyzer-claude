"""Метрики эталона на ручных примерах и сквозной прогон ``run_eval``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import skill_fixtures  # noqa: F401  # scripts/ в sys.path
from skill_fake_testops import default_launch

from eval import run_eval
from eval.cassette import save_cassette
from eval.metrics import ClusterView, combine, evaluate, summary

LABELS = {"groups": [
    {"id": "db", "cause": "db-pool", "tests": [1, 2], "evidence": ["HikariPool-1 - timeout"]},
    {"id": "npe", "cause": "discount-null", "tests": [3, 4], "evidence": ["discount is null"]},
    {"id": "502", "cause": "auth-down", "tests": [5]},
    {"id": "ui", "cause": "auth-down", "tests": [6]},
    {"id": "silent-1", "cause": None, "tests": [7]},
    {"id": "silent-2", "cause": None, "tests": [8]},
]}


def _view(file_id: str, members: tuple[int, ...], visible: tuple[int, ...] = (),
          text: str = "") -> ClusterView:
    return ClusterView(file_id, members, visible or members[:1], text)


def _perfect() -> list[ClusterView]:
    return [
        _view("01", (1, 2), text="ERROR HikariPool-1 -   timeout\n"),
        _view("02", (3, 4), text="NPE: discount is null"),
        _view("03", (5,)), _view("04", (6,)), _view("05", (7,)), _view("06", (8,)),
    ]


def test_perfect_clustering() -> None:
    result = evaluate(LABELS, _perfect())

    assert (result["precision"], result["recall"]) == (1.0, 1.0)
    assert result["hidden_groups"] == 0
    assert (result["evidence_lost"], result["evidence_total"]) == (0, 2)
    assert not result["merged"] and not result["split"]


def test_merging_different_causes_costs_precision_and_hides_a_group() -> None:
    clusters = [_view("01", (1, 2, 3, 4), (1,), "HikariPool-1 - timeout"),
                *_perfect()[2:]]

    result = evaluate(LABELS, clusters)

    assert result["precision"] == pytest.approx((4 * 0.5 + 4) / 8, abs=1e-4)
    assert result["recall"] == 1.0
    assert result["merged"] == [["db", "npe"]]
    assert result["hidden"] == ["npe"]
    assert result["lost"] == [{"group": "npe", "line": "discount is null",
                               "reason": "группа скрыта"}]


def test_symptoms_of_one_cause_are_not_penalized_together_or_apart() -> None:
    together = evaluate(LABELS, [*_perfect()[:2], _view("03", (5, 6), (5, 6)),
                                 *_perfect()[4:]])

    assert together["precision"] == 1.0
    assert together["recall"] == 1.0
    assert not together["merged"]


def test_unknown_causes_never_merge() -> None:
    result = evaluate(LABELS, [*_perfect()[:4], _view("05", (7, 8), (7, 8))])

    assert result["precision"] < 1.0
    assert result["merged"] == [["silent-1", "silent-2"]]


def test_split_group_costs_recall() -> None:
    result = evaluate(LABELS, [_view("01", (1,), text="HikariPool-1 - timeout"),
                               _view("07", (2,)), *_perfect()[1:]])

    assert result["recall"] == pytest.approx((0.5 + 0.5 + 6) / 8, abs=1e-4)
    assert result["split"] == {"db": ["01", "07"]}


def test_evidence_missing_from_the_visible_task_is_lost() -> None:
    clusters = _perfect()
    clusters[1] = _view("02", (3, 4), text="Сообщение: expected 200 but was 500")

    result = evaluate(LABELS, clusters)

    assert result["lost"] == [{"group": "npe", "line": "discount is null",
                               "reason": "нет в задании"}]
    assert result["evidence_loss"] == 0.5


def test_combine_weights_by_tests() -> None:
    first = summary({**evaluate(LABELS, _perfect())})
    second = {**first, "tests": 2, "precision": 0.5}

    combined = combine([first, second])

    assert combined["tests"] == 10
    assert combined["precision"] == pytest.approx((8 + 1) / 10)


def test_run_eval_on_a_cassette_prints_only_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save_cassette(default_launch(), tmp_path / "cassette")
    labels = {"groups": [
        {"id": "orders", "cause": "npe", "tests": [101, 102],
         "evidence": ["java.lang.NullPointerException: customer is null"]},
        {"id": "auth", "cause": "auth-down", "tests": [103]},
        {"id": "silent", "cause": None, "tests": [108]},
    ]}
    (tmp_path / "labels.json").write_text(json.dumps(labels), encoding="utf-8")

    assert run_eval.main(["--cassette", str(tmp_path / "cassette"),
                          "--labels", str(tmp_path / "labels.json")]) == 0
    out = capsys.readouterr().out
    result = json.loads(out)

    assert result["precision"] == result["recall"] == 1.0
    assert result["evidence_lost"] == 0
    assert "customer is null" not in out


def test_analyses_checklist_pairs_labels_with_model_answers(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "analyses").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "clusters": [{"file_id": "01", "cluster_id": "c1", "label": "HTTP 500"},
                     {"file_id": "02", "cluster_id": "c2", "label": "silent"}],
        "clustering": {"clusters": [{"cluster_id": "c1", "member_test_ids": [1, 2, 3]},
                                    {"cluster_id": "c2", "member_test_ids": [7]}]},
    }), encoding="utf-8")
    (run_dir / "analyses" / "01.md").write_text(
        "ЧТО СЛОМАЛОСЬ: HTTP 500.\n\nПРИЧИНА: приложение — пул соединений исчерпан.\n",
        encoding="utf-8")

    text = run_eval.analyses_checklist(run_dir, LABELS)

    assert "- эталон: db × 2 — причина db-pool" in text
    assert "- эталон: npe × 1 — причина discount-null" in text
    assert "- разбор: приложение — пул соединений исчерпан." in text
    assert "- эталон: silent-1 × 1 — причина неизвестна" in text
    assert "- разбор: нет" in text


def test_duplicate_group_ids_are_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    labels = {"groups": [
        {"id": "orders", "cause": "npe", "tests": [101, 102]},
        {"id": "orders", "cause": "auth-down", "tests": [103, 108]},
    ]}
    with pytest.raises(ValueError, match="повторяется id группы orders"):
        evaluate(labels, [_view("01", (101, 102, 103, 108))])

    save_cassette(default_launch(), tmp_path / "cassette")
    (tmp_path / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    code = run_eval.main(["--cassette", str(tmp_path / "cassette"),
                          "--labels", str(tmp_path / "labels.json")])

    assert code == 2
    assert "повторяется id группы orders" in capsys.readouterr().err
