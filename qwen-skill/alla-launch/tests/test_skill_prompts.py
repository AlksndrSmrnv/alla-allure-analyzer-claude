"""Задание на кластер: каждое сочетание данных сохраняет все ключевые директивы.

Текст «Правил» и «Задания» сокращали, убирая повторы. Этот тест — страховка: если
правка потеряет инструкцию, от которой зависит качество разбора, он покажет, какую.
Формулировки проверяются по ключевым словам, а не по точному тексту.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alla_skill_lib.agent_rules import EXECUTOR_RULES
from alla_skill_lib.analysis_format import parse_analysis, validate_analysis
from alla_skill_lib.cluster_task import RULES, build_task_text, no_evidence_analysis

VARIANTS = {
    "symptom+log": {"has_symptom": True, "has_log": True},
    "log only": {"has_symptom": False, "has_log": True},
    "symptom only": {"has_symptom": True, "has_log": False},
}
CATEGORIES = ("тест", "приложение", "окружение", "данные", "неизвестно")


def _task(variant: str, *, low_evidence: bool = False, has_kb: bool = False) -> str:
    return build_task_text(low_evidence=low_evidence, has_kb=has_kb, **VARIANTS[variant])


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("has_kb", [False, True])
def test_every_variant_keeps_the_answer_format_and_step_rules(variant: str, has_kb: bool) -> None:
    task = _task(variant, has_kb=has_kb)
    for marker in ("ЧТО СЛОМАЛОСЬ:", "ПРИЧИНА:", "КАК ИСПРАВИТЬ:", "КОД:", "без вступления"):
        assert marker in task, marker
    assert "<тест|приложение|окружение|данные|неизвестно>" in task
    assert "только если ты открывал код проекта" in task
    # Шаги: действие с глагола, без «проверьте», без абстрактных советов и выдумок.
    for phrase in ("начинай с глагола", "«проверьте»", "абстрактных советов", "Не выдумывай"):
        assert phrase in task, phrase
    # Ни слова про базу знаний, пока записей нет; с записями — строка БАЗА ЗНАНИЙ.
    assert ("БАЗА ЗНАНИЙ:" in task) is has_kb
    if not has_kb:
        assert "знаний" not in task


def test_symptom_and_log_task_requires_log_evidence_before_a_quote() -> None:
    task = _task("symptom+log")
    assert "2 предложения" in task and "дословной цитатой" in task
    assert "Если в логе есть явная ошибка" in task
    assert "явной ошибки в логе нет" in task
    assert "по ошибке, трейсу и коду" in task
    # Все четыре примера выбора категории.
    for example in ("→ приложение", "→ тест", "→ окружение", "→ данные"):
        assert example in task, example
    assert "первый шаг — с конкретикой из лога" in task
    assert "только если лог подтверждает причину" in task


def test_log_only_task_says_there_is_no_symptom_and_does_not_invent_one() -> None:
    task = _task("log only")
    assert "стек-трейса нет, анализ построен по логу" in task
    assert "дословная цитата" in task and "Симптом со стороны теста не выдумывай" in task
    assert "первый шаг — с конкретикой из лога" in task
    assert "явной ошибки в логе нет" in task
    assert "категория «неизвестно»" in task and "чего не хватает" in task
    assert "только если лог подтверждает причину" in task


def test_log_only_task_does_not_discard_a_confirmed_exact_kb_cause() -> None:
    task = _task("log only", has_kb=True)
    assert "точная запись базы знаний не подтверждает причину" in task
    assert "по подтверждённой ошибке в логе или точной записи базы знаний" in task


def test_symptom_only_task_says_no_log_in_data_and_does_not_invent_it() -> None:
    # Без фрагмента лога неизвестно, был ли лог: «лог пуст» модель писала и тогда, когда
    # вложений не было вовсе (стенд Qwen, сценарий A01, кластер login).
    task = _task("symptom only")
    assert "лога приложения в данных нет" in task
    assert "не пиши, что лог пуст или без ошибок" in task
    assert "лог приложения пуст" not in task
    assert "содержимое лога не выдумывай" in task
    assert "с учётом «Шага теста»" in task
    assert "первый шаг" not in task  # про лог в шагах говорить нечего


@pytest.mark.parametrize("variant", VARIANTS)
def test_unknown_category_needs_both_data_and_code_to_fail(variant: str) -> None:
    task = _task(variant)
    cause_line = next(line for line in task.splitlines() if line.startswith("ПРИЧИНА:"))
    assert "«неизвестно» — только если ни одну из четырёх остальных нельзя обосновать" in cause_line


@pytest.mark.parametrize("variant", VARIANTS)
def test_low_evidence_adds_the_step_hint(variant: str) -> None:
    assert "Шаг теста" not in _task(variant).split("КОД:", 1)[1]
    note = _task(variant, low_evidence=True)
    assert "Данных об ошибке немного" in note and "не выдумывай по нему деталей" in note


def test_rules_keep_the_core_directives() -> None:
    text = " ".join(RULES.split())
    for phrase in (
        "по-русски", "простым языком", "без жаргона", "стек-трейс не пересказывай",
        "ничего не додумывай", "категория «неизвестно»", "чего не хватает",  # мало данных
        "симптом", "поведение системы", "первопричина", "свяжи их",
        "игнорировать нельзя", "важнее текста assertion",
        "Лог пуст или без ошибок", "«Шаг теста» — вспомогательный контекст",
        "Где искать код автотеста", "не больше 3 файлов", "Ничего не изменяй",
        "тесты и сборку не запускай", "Код мог измениться после прогона",
    ):
        assert phrase in text, phrase


def test_categories_in_the_format_match_the_validator() -> None:
    from alla_skill_lib.analysis_format import CATEGORIES as accepted

    assert tuple(accepted) == CATEGORIES
    assert all(f"{c}" in _task("symptom+log") for c in CATEGORIES)


def test_executor_rules_name_every_prohibition() -> None:
    text = " ".join(EXECUTOR_RULES.split())
    for phrase in (
        "только то, что сказано", "ничего не добавляй", "только читай", "Своих скриптов не пиши",
        "python -c", "heredoc", "write_file", ".env", "run.json", "evidence/", "не обходи",
        "Проблема скилла:",
    ):
        assert phrase in text, phrase


def test_ready_no_evidence_analysis_is_valid_and_does_not_claim_logs_were_missing(
    tmp_path: Path,
) -> None:
    # Без фрагмента лога неизвестно, были ли логи: на стенде Qwen (E03) разбор утверждал
    # «нет … ни лога» у теста с INFO-логом и предлагал искать пропавшие вложения.
    text = no_evidence_analysis()
    analysis = parse_analysis(text)
    assert validate_analysis(analysis, tmp_path) == []
    assert analysis.category == "неизвестно"
    assert "ни лога" not in text and "не сохранились" not in text
    assert "если они есть" in text
