"""Метрики эталона: ошибочные объединения, дробление, скрытые группы, потеря доказательств.

Чистые функции над разметкой (``labels.json``) и тем, что ``prepare`` показал модели
(:class:`ClusterView`). Правильность диагнозов здесь не оценивается.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ClusterView:
    """Кластер глазами модели.

    ``members`` — все тесты кластера; ``visible`` — тесты, чьи данные попали в задание
    (представитель и источник лога); ``task_text`` — текст ``clusters/NN.md`` (у
    кластера без задания — пусто).
    """

    file_id: str
    members: tuple[int, ...]
    visible: tuple[int, ...]
    task_text: str


def normalize_space(text: str) -> str:
    return " ".join(text.split())


def evaluate(labels: dict[str, Any], clusters: Iterable[ClusterView]) -> dict[str, Any]:
    """Посчитать метрики одного прогона.

    * ``precision`` — B-Cubed: пара тестов в одном кластере верна, если они из одной группы
      симптомов или у их групп одна известная ``cause``; группы с ``cause: null`` между
      собой всегда разные.
    * ``recall`` — B-Cubed по группам симптомов: разнесение разных симптомов одной причины
      по кластерам не штрафуется (они и так разные группы).
    * ``hidden_groups`` — группы, ни один тест которых не виден модели.
    * ``evidence_loss`` — доля строк ``evidence``, которых нет (с точностью до пробелов) в
      заданиях кластеров, где группа видима; у скрытой группы потеряны все строки.
    """
    clusters = list(clusters)
    groups: dict[str, dict[str, Any]] = {}
    group_of: dict[int, str] = {}
    for group in labels["groups"]:
        # Повтор id или теста молча выбросил бы часть разметки из всех метрик.
        if group["id"] in groups:
            raise ValueError(f"повторяется id группы {group['id']}")
        groups[group["id"]] = group
        for test in group["tests"]:
            if test in group_of:
                raise ValueError(f"тест {test} в группах {group_of[test]} и {group['id']}")
            group_of[test] = group["id"]
    cluster_of: dict[int, ClusterView] = {}
    for cluster in clusters:
        for test in cluster.members:
            cluster_of[test] = cluster

    def same_problem(a: int, b: int) -> bool:
        if group_of[a] == group_of[b]:
            return True
        cause = groups[group_of[a]].get("cause")
        return cause is not None and cause == groups[group_of[b]].get("cause")

    precision_sum = recall_sum = 0.0
    scored = 0
    merged: set[tuple[str, str]] = set()
    for test, group_id in group_of.items():
        cluster = cluster_of.get(test)
        if cluster is None:
            continue
        labeled = [other for other in cluster.members if other in group_of]
        precision_sum += sum(same_problem(test, other) for other in labeled) / len(labeled)
        group_tests = groups[group_id]["tests"]
        recall_sum += sum(cluster_of.get(other) is cluster for other in group_tests) / len(
            group_tests)
        scored += 1
        for other in labeled:
            if not same_problem(test, other):
                merged.add(tuple(sorted((group_id, group_of[other]))))  # type: ignore[arg-type]

    split = {
        group_id: sorted({cluster_of[test].file_id for test in group["tests"] if test in cluster_of})
        for group_id, group in groups.items()
    }
    split = {group_id: file_ids for group_id, file_ids in split.items() if len(file_ids) > 1}

    visible_in: dict[str, list[ClusterView]] = defaultdict(list)
    for cluster in clusters:
        for group_id in {group_of[test] for test in cluster.visible if test in group_of}:
            visible_in[group_id].append(cluster)
    hidden = sorted(group_id for group_id in groups if not visible_in[group_id])

    lost: list[dict[str, str]] = []
    evidence_total = 0
    for group_id, group in groups.items():
        tasks = [normalize_space(cluster.task_text) for cluster in visible_in[group_id]]
        for line in group.get("evidence") or []:
            evidence_total += 1
            if not tasks:
                lost.append({"group": group_id, "line": line, "reason": "группа скрыта"})
            elif not any(normalize_space(line) in task for task in tasks):
                lost.append({"group": group_id, "line": line, "reason": "нет в задании"})

    unclustered = sorted(test for test in group_of if test not in cluster_of)
    unlabeled = sorted(test for test in cluster_of if test not in group_of)
    return {
        "tests": len(group_of),
        "groups": len(groups),
        "clusters": len(clusters),
        "precision": round(precision_sum / scored, 4) if scored else 1.0,
        "recall": round(recall_sum / scored, 4) if scored else 1.0,
        "hidden_groups": len(hidden),
        "evidence_total": evidence_total,
        "evidence_lost": len(lost),
        "evidence_loss": round(len(lost) / evidence_total, 4) if evidence_total else 0.0,
        "merged": [list(pair) for pair in sorted(merged)],
        "split": split,
        "hidden": hidden,
        "lost": lost,
        "unclustered": unclustered,
        "unlabeled": unlabeled,
    }


SUMMARY_KEYS = (
    "tests", "groups", "clusters", "precision", "recall", "hidden_groups",
    "evidence_total", "evidence_lost", "evidence_loss",
)


def summary(result: dict[str, Any]) -> dict[str, Any]:
    """Только числа — то, что можно показать за пределами команды."""
    return {key: result[key] for key in SUMMARY_KEYS}


def coverage(result: dict[str, Any]) -> dict[str, int]:
    """Расхождение разметки и прогона числами: тесты вне кластеров и без разметки."""
    return {"unclustered": len(result["unclustered"]), "unlabeled": len(result["unlabeled"])}


def combine(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Сводка по набору прогонов: precision/recall взвешены по числу тестов, остальное — суммы."""
    results = list(results)
    tests = sum(result["tests"] for result in results)
    total = {key: sum(result[key] for result in results)
             for key in ("tests", "groups", "clusters", "hidden_groups", "evidence_total",
                         "evidence_lost")}
    for key in ("precision", "recall"):
        total[key] = (round(sum(result[key] * result["tests"] for result in results) / tests, 4)
                      if tests else 1.0)
    total["evidence_loss"] = (round(total["evidence_lost"] / total["evidence_total"], 4)
                              if total["evidence_total"] else 0.0)
    return {key: total[key] for key in SUMMARY_KEYS}


RETRY_KEYS = (
    "retry_links", "retry_links_found", "retry_links_wrong", "retry_same", "retry_same_found",
    "passed_after_retry", "passed_after_retry_found", "passed_after_retry_wrong",
)


def evaluate_retries(expected: dict[str, Any], triage: dict[str, Any]) -> dict[str, Any]:
    """Связь попыток (шаг 5) против разметки ``retries``.

    * ``retry_links`` / ``_found`` — ожидаемые попытки и найденные у своего финального
      результата; ``retry_links_wrong`` — попытки у размеченных финальных результатов,
      которых там быть не должно (смешаны параметры, окружение или чужой тест);
    * ``retry_same`` / ``_found`` — попытки с размеченным «та же ошибка» и верно
      определённые среди найденных;
    * ``passed_after_retry`` / ``_found`` / ``_wrong`` — прошедшие после повтора.
    """
    found = {int(test["test_result_id"]): {int(a["test_result_id"]): a
                                           for a in test.get("attempts") or []}
             for test in triage.get("failed_tests", [])}
    totals = dict.fromkeys(RETRY_KEYS, 0)
    problems: list[str] = []
    for link in expected.get("links", []):
        final = int(link["final"])
        actual = found.get(final, {})
        wanted = {int(a["id"]): a.get("same") for a in link["attempts"]}
        totals["retry_links"] += len(wanted)
        for attempt_id, same in wanted.items():
            got = actual.get(attempt_id)
            if got is None:
                problems.append(f"{final}: не связана попытка {attempt_id}")
                continue
            totals["retry_links_found"] += 1
            if same is not None:
                totals["retry_same"] += 1
                if got.get("same_as_final") is same:
                    totals["retry_same_found"] += 1
                else:
                    problems.append(f"{final}: попытка {attempt_id} — «та же ошибка» "
                                    f"{got.get('same_as_final')}, ожидалось {same}")
        for attempt_id in sorted(set(actual) - set(wanted)):
            totals["retry_links_wrong"] += 1
            problems.append(f"{final}: лишняя попытка {attempt_id}")
    wanted_passed = {int(item) for item in expected.get("passed_after_retry", [])}
    got_passed = {int(item["test_result_id"])
                  for item in (triage.get("retries") or {}).get("passed_after_retry", [])}
    totals["passed_after_retry"] = len(wanted_passed)
    totals["passed_after_retry_found"] = len(wanted_passed & got_passed)
    totals["passed_after_retry_wrong"] = len(got_passed - wanted_passed)
    problems += [f"не найден прошедший после повтора {item}"
                 for item in sorted(wanted_passed - got_passed)]
    problems += [f"лишний прошедший после повтора {item}"
                 for item in sorted(got_passed - wanted_passed)]
    return {**totals, "retry_problems": problems}


KB_OFFER_KEYS = ("kb_offers", "kb_offers_found", "kb_offers_wrong")


def kb_offer_records(labels: dict[str, Any]) -> list[dict[str, Any]]:
    """Синтетические записи базы знаний для причин, у которых несколько групп симптомов.

    Признак записи — первая строка ``evidence`` первой группы причины (как если бы
    пользователь запомнил её по этой группе). Остальные группы той же причины узнаются,
    только если эта строка есть и в их данных.
    """
    by_cause: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in labels["groups"]:
        if group.get("cause"):
            by_cause[group["cause"]].append(group)
    records: list[dict[str, Any]] = []
    for cause, groups in by_cause.items():
        evidence = next((group["evidence"][0] for group in groups if group.get("evidence")), None)
        if len(groups) > 1 and evidence:
            records.append({"cause": cause, "category": groups[0].get("category"),
                            "error_example": evidence})
    return records


def evaluate_kb_offers(
    labels: dict[str, Any],
    offers: Iterable[tuple[str, tuple[int, ...], set[str]]],
) -> dict[str, Any]:
    """Кому ``prepare`` предложил записи из :func:`kb_offer_records` (шаг 6).

    ``offers`` — (кластер, его тесты, причины предложенных записей). Пара «кластер — запись»
    ожидается, если в кластере есть тест этой причины:

    * ``kb_offers`` / ``_found`` — ожидаемые пары и предложенные из них (запись дошла до
      всех проблем своей причины — иначе их не сгруппировать);
    * ``kb_offers_wrong`` — предложения кластерам без тестов этой причины (ложное
      предложение и согласие модели дали бы ложную известную проблему).
    """
    causes = {record["cause"] for record in kb_offer_records(labels)}
    cause_of = {test: group.get("cause") for group in labels["groups"] for test in group["tests"]}
    totals = dict.fromkeys(KB_OFFER_KEYS, 0)
    problems: list[str] = []
    for file_id, members, offered in offers:
        present = {cause_of.get(test) for test in members} & causes
        totals["kb_offers"] += len(present)
        totals["kb_offers_found"] += len(present & offered)
        totals["kb_offers_wrong"] += len(offered - present)
        problems += [f"кластер {file_id}: не предложена запись причины {cause}"
                     for cause in sorted(present - offered)]
        problems += [f"кластер {file_id}: предложена запись чужой причины {cause}"
                     for cause in sorted(offered - present)]
    return {**totals, "kb_offer_problems": problems}
