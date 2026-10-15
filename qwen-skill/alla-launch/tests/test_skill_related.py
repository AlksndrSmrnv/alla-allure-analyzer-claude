"""Похожие проблемы после разбора (``related.py``): кандидаты, решение модели, шаг ``relate``
в ``next``, пометки в отчёте и сводке."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from skill_fixtures import (  # noqa: F401
    answer_related,
    project_fixture,
    related_numbers,
    without_libmagic,
)
from eval.corpus_dev import step_split_one_cause
from skill_fake_testops import FakeTestOps
from test_skill_flow import _full_report, _next, _prepare

from alla_skill_lib.agent_rules import EXECUTOR_RULES
from alla_skill_lib.analysis_format import parse_analysis
from alla_skill_lib.known_issues import KnownIssue, KnownIssues, KnownRecord
from alla_skill_lib.related import (
    MAX_RELATE_GROUP,
    NEAR_WHY,
    candidate_groups,
    candidate_stats,
    parse_related,
    related_groups,
    relate_data,
    validate_related,
)

NPE = (
    "ЧТО СЛОМАЛОСЬ: Заказ не создан: сервис ответил 500.\n"
    "ПРИЧИНА: приложение — {cause}\n"
    "НАБЛЮДЕНИЯ:\n- {observation}\n"
    "КАК ИСПРАВИТЬ:\n1. {step}\n"
)
CUSTOMER = "сервис заказов падает с NullPointerException: покупатель в заказе не заполнен."
CUSTOMER_STEP = "Проверить заполнение покупателя в OrderService.create перед сохранением заказа."


def _analysis(cause: str = CUSTOMER, step: str = CUSTOMER_STEP, *, extra: str = "",
              category: str = "приложение") -> Any:
    text = NPE.format(cause=cause, observation="[S1] «expected: <200> but was: <500>»",
                      step=step) + extra
    return parse_analysis(text.replace("приложение —", f"{category} —", 1))


def _run(count: int, near: tuple[tuple[int, int, str], ...] = (),
         sizes: dict[int, int] | None = None) -> dict[str, Any]:
    sizes = sizes or {}
    return {
        "launch_id": 77,
        "clusters": [{"file_id": f"{n:02d}", "cluster_id": f"c{n}", "member_count": sizes.get(n, 1),
                      "auto": False, "label": "expected: <200> but was: <500>"}
                     for n in range(1, count + 1)],
        "clustering": {"near_pairs": [{"cluster_a": f"c{a}", "cluster_b": f"c{b}", "reason": reason,
                                       "pairs": 1} for a, b, reason in near]},
    }


def _groups(run: dict[str, Any], analyses: dict[str, Any], root: Path, **kwargs: Any
            ) -> list[tuple[list[int], list[str]]]:
    return [([int(file_id) for file_id in group.file_ids], [edge.kind for edge in group.edges])
            for group in candidate_groups(run, analyses, set(), root, **kwargs)]


# --- кандидаты -----------------------------------------------------------------------------


def test_near_pair_of_same_category_is_a_candidate_with_its_reason(tmp_path: Path) -> None:
    run = _run(3, near=((1, 2, "step"),))
    analyses = {"01": _analysis(), "02": _analysis("тест упал без причины рядом", "Перезапустить."),
                "03": _analysis("другая ошибка каталога цен", "Опубликовать прайс-лист.")}
    [group] = candidate_groups(run, analyses, set(), tmp_path)
    assert group.file_ids == ["01", "02"]
    assert [(edge.kind, edge.why) for edge in group.edges] == [("near", NEAR_WHY["step"])]


def test_similar_cause_and_step_link_problems_without_near_pair(tmp_path: Path) -> None:
    analyses = {"01": _analysis(), "02": _analysis(),
                "03": _analysis("в каталоге не опубликован прайс-лист PL-77.",
                                "Опубликовать прайс-лист в админке каталога.")}
    assert _groups(_run(3), analyses, tmp_path) == [([1, 2], ["text"])]


def test_same_place_in_code_links_problems(tmp_path: Path) -> None:
    source = tmp_path / "src" / "OrderSteps.java"
    source.parent.mkdir()
    source.write_text("class OrderSteps {}\n" * 60, encoding="utf-8")
    analyses = {
        "01": _analysis("локатор кнопки оформления устарел.", "Обновить локатор.",
                        extra="КОД: src/OrderSteps.java:40 — локатор\n", category="тест"),
        "02": _analysis("ожидание загрузки корзины слишком короткое.", "Добавить ожидание.",
                        extra="КОД: src/OrderSteps.java:43 — ожидание\n", category="тест"),
        "03": _analysis("тест берёт протухший токен.", "Обновить токен.",
                        extra="КОД: src/OrderSteps.java:58 — токен\n", category="тест"),
    }
    [group] = candidate_groups(_run(3), analyses, set(), tmp_path)
    assert group.file_ids == ["01", "02"]
    assert group.edges[0].why == "одно место в коде: src/OrderSteps.java:40"


def test_different_categories_mixed_flagged_and_rejected_are_not_compared(tmp_path: Path) -> None:
    run = _run(5, near=((1, 2, "step"), (1, 3, "step"), (1, 4, "log"), (1, 5, "step")))
    mixed = _analysis(extra="СОГЛАСОВАННОСТЬ: разные проблемы — у части тестов пул БД\n")
    record = KnownRecord(kb_dir="kb", id="orders-npe", title="NPE", category="приложение",
                         description="", first_step="")
    known = KnownIssues(rejected={"05": record})
    analyses = {"01": _analysis(), "02": _analysis(category="окружение"), "03": mixed,
                "04": _analysis(), "05": _analysis()}
    groups = candidate_groups(run, analyses, {"04"}, tmp_path, known)
    assert groups == []


def test_problems_with_a_confirmed_finding_are_not_compared(tmp_path: Path) -> None:
    run = _run(3, near=((1, 2, "step"), (2, 3, "step")))
    analyses = {file_id: _analysis() for file_id in ("01", "02", "03")}
    assert [numbers for numbers, _ in _groups(run, analyses, tmp_path)] == [[1, 2, 3]]
    assert [numbers for numbers, _ in _groups(run, analyses, tmp_path, confirmed={"02"})] == [[1, 3]]
    assert _groups(run, analyses, tmp_path, confirmed={"01", "03"}) == []  # один — сравнивать не с кем


def test_unknown_causes_are_compared_only_through_near_pairs(tmp_path: Path) -> None:
    unknown = _analysis("по данным причину не установить.", "Приложить лог сервиса заказов.",
                        category="неизвестно")
    analyses = {"01": unknown, "02": unknown, "03": unknown}
    assert _groups(_run(3), analyses, tmp_path) == []
    assert _groups(_run(3, near=((2, 3, "score"),)), analyses, tmp_path) == [([2, 3], ["near"])]


def _kb(records: dict[int, list[str]], kb_dir: str = "kb") -> dict[str, list[dict[str, Any]]]:
    """Записи, подходящие проблемам сейчас (как ``current_kb_matches``)."""
    return {f"{n:02d}": [{"id": record, "title": f"Запись {record}", "category": "приложение",
                          "origin": "fingerprint", "kb_dir": kb_dir, "kb_label": kb_dir}
                         for record in ids] for n, ids in records.items()}


def test_shared_kb_record_links_problems_even_with_unknown_cause(tmp_path: Path) -> None:
    """Запись узнаёт ошибку по данным кластера, а не по словам разбора: общая запись — повод
    сравнить, и у «неизвестно» тоже."""
    analyses = {"01": _analysis(), "02": _analysis("сервис заказов не принял заказ.", "Повторить."),
                "03": _analysis("каталог не опубликован.", "Опубликовать прайс-лист.")}
    kb = _kb({1: ["orders_npe_1a2b"], 2: ["orders_npe_1a2b"], 3: ["catalog_9f"]})
    [group] = candidate_groups(_run(3), analyses, set(), tmp_path, kb=kb)
    assert group.file_ids == ["01", "02"]
    assert [(edge.kind, edge.why) for edge in group.edges] == [
        ("kb", "обеим подходит запись базы знаний orders_npe_1a2b «Запись orders_npe_1a2b»")]

    unknown = _analysis("по данным причину не установить.", "Приложить лог.", category="неизвестно")
    assert _groups(_run(2), {"01": unknown, "02": unknown}, tmp_path,
                   kb=_kb({1: ["pool_1"], 2: ["pool_1"]})) == [([1, 2], ["kb"])]


def test_same_id_in_different_module_kbs_is_not_a_shared_record(tmp_path: Path) -> None:
    """У каждого модуля своя база знаний: orders/alla-kb/pool и payments/alla-kb/pool — разные."""
    unknown = _analysis("по данным причину не установить.", "Приложить лог.", category="неизвестно")
    kb = {**_kb({1: ["pool_1"]}, "orders/alla-kb"), **_kb({2: ["pool_1"]}, "payments/alla-kb")}
    assert _groups(_run(2), {"01": unknown, "02": unknown}, tmp_path, kb=kb) == []

    # В одной группе (по близкой паре) записи разных модулей подписаны папкой.
    run = _run(2, near=((1, 2, "step"),))
    groups = candidate_groups(run, {"01": unknown, "02": unknown}, set(), tmp_path, kb=kb)
    data = relate_data(run, groups, {"01": unknown, "02": unknown}, kb)
    assert "БАЗА ЗНАНИЙ: pool_1 «Запись pool_1» (orders/alla-kb; приложение;" in data
    assert "БАЗА ЗНАНИЙ: pool_1 «Запись pool_1» (payments/alla-kb; приложение;" in data


def test_relate_data_shows_kb_records_of_each_problem(tmp_path: Path) -> None:
    analyses = {"01": _analysis(), "02": _analysis()}
    kb = _kb({1: ["orders_npe_1a2b"], 2: ["pool_1"]})
    kb["02"][0]["origin"] = "exact"
    run = _run(2)
    groups = candidate_groups(run, analyses, set(), tmp_path, kb=kb)
    data = relate_data(run, groups, analyses, kb)
    assert ("БАЗА ЗНАНИЙ: orders_npe_1a2b «Запись orders_npe_1a2b» (приложение; признак найден "
            "в данных)") in data
    assert "БАЗА ЗНАНИЙ: pool_1 «Запись pool_1» (приложение; точное совпадение)" in data


def test_problems_with_a_confirmed_kb_record_are_not_compared(tmp_path: Path) -> None:
    """Причина уже известна и показана блоком известных проблем: иначе одна проблема попала
    бы в оба блока сводки."""
    record = KnownRecord(kb_dir="kb", id="orders-npe", title="NPE", category="приложение",
                         description="", first_step="")
    known = KnownIssues(refs={"01": record, "02": record},
                        groups=[KnownIssue(record, ["01", "02"], 2)])
    analyses = {"01": _analysis(), "02": _analysis(), "03": _analysis(), "04": _analysis()}
    assert _groups(_run(4), analyses, tmp_path, known=known) == [([3, 4], ["text"])]


def test_large_component_keeps_largest_problems(tmp_path: Path) -> None:
    count = MAX_RELATE_GROUP + 3
    analyses = {f"{n:02d}": _analysis() for n in range(1, count + 1)}
    run = _run(count, sizes={n: n for n in range(1, count + 1)})
    [group] = candidate_groups(run, analyses, set(), tmp_path)
    assert [int(file_id) for file_id in group.file_ids] == list(range(4, count + 1))
    stats = candidate_stats([group])
    assert stats == {"groups": 1, "problems": MAX_RELATE_GROUP,
                     "edges": {"text": MAX_RELATE_GROUP * (MAX_RELATE_GROUP - 1) // 2}}


# --- решение модели ------------------------------------------------------------------------


def _two_groups(tmp_path: Path) -> list[Any]:
    analyses = {"01": _analysis(), "02": _analysis(), "03": _analysis(),
                "04": _analysis("в каталоге не опубликован прайс-лист.", "Опубликовать прайс-лист."),
                "05": _analysis("в каталоге не опубликован прайс-лист.", "Опубликовать прайс-лист.")}
    groups = candidate_groups(_run(5), analyses, set(), tmp_path)
    assert [group.file_ids for group in groups] == [["01", "02", "03"], ["04", "05"]]
    return groups


def test_valid_decision_gives_related_groups(tmp_path: Path) -> None:
    groups = _two_groups(tmp_path)
    decision = parse_related(
        "```text\n**ОДНА ПРИЧИНА:** 1, 2 и 3 — покупатель в заказе не заполнен\n"
        "- разные: 4; 5. — прайс-листы разные\n```\n")
    assert validate_related(decision, groups) == []
    related = related_groups(decision, _run(5, sizes={1: 2, 2: 3, 3: 1}))
    assert [(group.file_ids, group.cause, group.size) for group in related.groups] == [
        (["01", "02", "03"], "покупатель в заказе не заполнен", 6)]
    assert related.group_of("02") is related.groups[0] and related.group_of("04") is None


@pytest.mark.parametrize(("text", "error"), [
    ("Похоже, у всех одна причина.", "строка не распознана"),
    ("", "нет ни одной строки"),
    ("ОДНА ПРИЧИНА: 1, 2, 3 — NPE\nРАЗНЫЕ: 4, 5, 9 — разные", "проблемы 9 нет в группах"),
    ("ОДНА ПРИЧИНА: 1, 2, 3 — NPE\nРАЗНЫЕ: 3, 4, 5", "встречается в нескольких строках"),
    ("ОДНА ПРИЧИНА: 1, 2 — NPE\nРАЗНЫЕ: 4", "нет решения по проблемам 3, 5"),
    ("ОДНА ПРИЧИНА: 1 — NPE\nРАЗНЫЕ: 2, 3, 4, 5", "минимум два номера"),
    ("ОДНА ПРИЧИНА: 3, 4 — NPE\nРАЗНЫЕ: 1, 2, 5", "из разных групп"),
    ("ОДНА ПРИЧИНА: 1, 2, 3\nРАЗНЫЕ: 4, 5", "нет общей причины"),
])
def test_invalid_decision_names_the_problem(tmp_path: Path, text: str, error: str) -> None:
    errors = validate_related(parse_related(text), _two_groups(tmp_path))
    assert any(error in item for item in errors), errors


# --- шаг relate в next ---------------------------------------------------------------------


def _observation(run_dir: Path, file_id: str) -> str:
    """Строка наблюдения: из лога, если он есть в задании (иначе «приложение» не примут)."""
    sources = json.loads((run_dir / "evidence" / f"{file_id}.sources.json").read_text("utf-8"))
    source_id = next((key for key, record in sources.items()
                      if record.get("kind") == "log" and record.get("section") in ("лог", "журнал")),
                     "S1")
    line = next(line for line in sources[source_id]["text"].splitlines() if line.strip())
    return f"[{source_id}] «{line.strip()[:60]}»"


def _step_split_run(project: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
                    ) -> tuple[Path, list[str]]:
    """Одна NPE, разделённая шагом теста: две проблемы с одинаковыми разборами."""
    FakeTestOps(step_split_one_cause().fixture).install(monkeypatch)
    run_dir, run, _ = _prepare(project, capsys, launch_id=5119)
    ids = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    assert len(ids) == 2
    assert [pair["reason"] for pair in run["clustering"]["near_pairs"]] == ["step"]
    for file_id in ids:
        text = NPE.format(cause=CUSTOMER, observation=_observation(run_dir, file_id), step=CUSTOMER_STEP)
        (run_dir / "analyses" / f"{file_id}.md").write_text(text, encoding="utf-8")
    return run_dir, ids


def test_related_problems_are_merged_in_report_brief_and_summary(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    run_dir, ids = _step_split_run(project, monkeypatch, capsys)
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: relate"), out
    task = (run_dir / "relate_task.md").read_text(encoding="utf-8")
    assert f"1 и 2: {NEAR_WHY['step']}" in task and str(run_dir / "related.md") in task
    assert EXECUTOR_RULES in task and "references/related-format.md" in task
    assert "references/related-format.md" in out
    assert related_numbers(run_dir) == [1, 2]

    (run_dir / "related.md").write_text(
        "ОДНА ПРИЧИНА: 1, 2 — покупатель в заказе не заполнен\n", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: summary"), out
    summary_task = (run_dir / "summary_task.md").read_text(encoding="utf-8")
    assert ("--- Объединённые проблемы (агент счёл одной причиной, сравнив разборы; пользователь "
            "не подтверждал) ---\nПроблем после объединения: 1\n"
            "проблема 1 и 2 — покупатель в заказе не заполнен — 6 тестов") in summary_task

    (run_dir / "summary.md").write_text("Итог прогона.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done"), out
    assert "6 упавших тестов -> 1 проблема (2 до объединения похожих):" in out
    assert "### 🔴 Требуют вашего внимания (1)" in out
    assert "**Проблема 1 и 2** · 6 тестов · [объединил агент]" in out
    assert "   Общая причина: [ПРИЛОЖЕНИЕ] покупатель в заказе не заполнен" in out
    assert out.count("**Проблема ") == 1 and "похоже, та же причина" not in out
    report = _full_report(run_dir)
    assert "В разборе: 6 упавших тестов → 1 проблема (2 до объединения похожих)" in report
    assert "### Требуют вашего внимания (1)" in report
    assert "**Проблема 1 и 2** — 6 тестов · объединил агент" in report
    assert ("- Общая причина: возможная ошибка приложения — покупатель в заказе не заполнен"
            in report)
    assert "- Разбор каждой проблемы — в подробностях ниже: проблемы 1, 2" in report
    assert "Похоже, одна причина" not in report
    assert "### Проблема 1 — " in report and "### Проблема 2 — " in report
    assert "- Агент счёл одной причиной с проблемой 2: покупатель в заказе не заполнен" in report
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["related"] == {"groups": 1, "problems": 2, "edges": {"near": 1},
                                "confirmed_groups": 1, "confirmed_problems": 2}


def test_changed_analysis_asks_to_compare_again(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    run_dir, ids = _step_split_run(project, monkeypatch, capsys)
    assert _next(run_dir, capsys).startswith("STATUS: relate")
    answer_related(run_dir)
    assert _next(run_dir, capsys).startswith("STATUS: summary")

    text = NPE.format(cause="сервис заказов теряет скидку в корзине.", observation=_observation(run_dir, ids[1]),
                      step="Проверить расчёт скидки в CartService.")
    (run_dir / "analyses" / f"{ids[1]}.md").write_text(text, encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: relate")  # данные групп другие
    assert not (run_dir / "related.md").exists()


def test_invalid_decision_is_fixed_then_dropped_with_a_note(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    run_dir, _ = _step_split_run(project, monkeypatch, capsys)
    assert _next(run_dir, capsys).startswith("STATUS: relate")
    related = run_dir / "related.md"
    for attempt in (1, 2):
        related.write_text(f"Похоже, одна причина {attempt}.\n", encoding="utf-8")
        out = _next(run_dir, capsys)
        assert out.startswith("STATUS: fix") and f"попытка {attempt} из 3" in out, out
        assert "строка не распознана" in out and "РАЗНЫЕ: <номера проблем через запятую>" in out
    assert "Это последняя попытка" in out
    related.write_text("Всё ещё не тот формат.\n", encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: summary")
    (run_dir / "summary.md").write_text("Итог прогона.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done") and "Похожие проблемы не сравнены" in out
    assert "похоже, та же причина" not in out


def test_no_candidates_means_no_relate_step(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    run_dir, ids = _step_split_run(project, monkeypatch, capsys)
    text = NPE.format(cause="сервис заказов теряет скидку в корзине.", observation=_observation(run_dir, ids[1]),
                      step="Проверить расчёт скидки в CartService.")
    (run_dir / "analyses" / f"{ids[1]}.md").write_text(
        text.replace("приложение —", "окружение —"), encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: summary")
    assert not (run_dir / "relate_task.md").exists()


def test_related_file_is_the_agents_own_file_for_stand_and_review() -> None:
    from alla_skill_lib.session_rules import WRITABLE_RE

    assert WRITABLE_RE.search("/p/alla-reports/5119-20261013-100000/related.md")
    assert not WRITABLE_RE.search("/p/alla-reports/5119-20261013-100000/relate_task.md")


def test_vanished_candidates_clear_both_hash_and_stats(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Группы исчезли — из state.json уходят и хэш, и числа: review не покажет старые."""
    run_dir, ids = _step_split_run(project, monkeypatch, capsys)
    assert _next(run_dir, capsys).startswith("STATUS: relate")
    (run_dir / "related.md").write_text("ОДНА ПРИЧИНА: 1, 2 — покупатель не заполнен\n",
                                        encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: summary")
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["related_data_hash"] and state["related"]["confirmed_groups"] == 1

    text = NPE.format(cause="сервис заказов теряет скидку в корзине.", observation=_observation(run_dir, ids[1]),
                      step="Проверить расчёт скидки в CartService.")
    (run_dir / "analyses" / f"{ids[1]}.md").write_text(
        text.replace("приложение —", "окружение —"), encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: summary")
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert "related_data_hash" not in state and "related" not in state
    assert not (run_dir / "related.md").exists()
