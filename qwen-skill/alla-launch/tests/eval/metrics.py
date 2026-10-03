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
    groups = {group["id"]: group for group in labels["groups"]}
    group_of = {test: group_id for group_id, group in groups.items() for test in group["tests"]}
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
