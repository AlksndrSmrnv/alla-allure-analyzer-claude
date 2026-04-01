"""Post-clustering merge правил для кластеров падений."""

from __future__ import annotations

from collections import defaultdict

from alla.knowledge.feedback_signature import build_feedback_cluster_context
from alla.knowledge.merge_rules_models import MergeRule
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.testops import FailedTestSummary
from alla.services.clustering_service import generate_cluster_id


class _UnionFind:
    """Минимальный union-find для объединения кластеров по правилам."""

    def __init__(self, items: list[str]) -> None:
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        parent = self._parent[item]
        if parent != item:
            parent = self.find(parent)
            self._parent[item] = parent
        return parent

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            self._parent[right_root] = left_root
        else:
            self._parent[left_root] = right_root


def apply_merge_rules(
    clustering_report: ClusteringReport,
    failed_tests: list[FailedTestSummary],
    rules: list[MergeRule],
) -> ClusteringReport:
    """Применить сохранённые merge rules к уже построенным кластерам."""
    if not clustering_report.clusters or not rules:
        return clustering_report

    test_by_id = {test.test_result_id: test for test in failed_tests}
    signature_to_cluster_ids: dict[str, list[str]] = defaultdict(list)

    for cluster in clustering_report.clusters:
        context = build_feedback_cluster_context(cluster, test_by_id)
        if context is None:
            continue
        signature_hash = context.base_issue_signature.signature_hash
        if not signature_hash:
            continue
        signature_to_cluster_ids[signature_hash].append(cluster.cluster_id)

    if not signature_to_cluster_ids:
        return clustering_report

    union_find = _UnionFind([cluster.cluster_id for cluster in clustering_report.clusters])
    applied_rule_count = 0

    for rule in rules:
        left_ids = signature_to_cluster_ids.get(rule.signature_hash_a, [])
        right_ids = signature_to_cluster_ids.get(rule.signature_hash_b, [])
        if not left_ids or not right_ids:
            continue

        applied_rule_count += 1
        for left_id in left_ids:
            for right_id in right_ids:
                union_find.union(left_id, right_id)

    if applied_rule_count == 0:
        return clustering_report

    order_by_cluster_id = {
        cluster.cluster_id: idx
        for idx, cluster in enumerate(clustering_report.clusters)
    }
    groups: dict[str, list[FailureCluster]] = defaultdict(list)
    for cluster in clustering_report.clusters:
        groups[union_find.find(cluster.cluster_id)].append(cluster)

    if all(len(group) == 1 for group in groups.values()):
        return clustering_report

    merged_clusters = [
        _merge_cluster_group(group, order_by_cluster_id)
        for group in groups.values()
    ]
    merged_clusters.sort(key=lambda cluster: (-cluster.member_count, cluster.cluster_id))

    return ClusteringReport(
        launch_id=clustering_report.launch_id,
        total_failures=clustering_report.total_failures,
        cluster_count=len(merged_clusters),
        clusters=merged_clusters,
        unclustered_count=sum(1 for cluster in merged_clusters if cluster.member_count == 1),
    )


def _merge_cluster_group(
    group: list[FailureCluster],
    order_by_cluster_id: dict[str, int],
) -> FailureCluster:
    """Слить компоненту связности в один FailureCluster."""
    if len(group) == 1:
        return group[0]

    representative = min(
        group,
        key=lambda cluster: (
            -cluster.member_count,
            order_by_cluster_id[cluster.cluster_id],
        ),
    )
    member_test_ids = sorted(
        {
            test_id
            for cluster in group
            for test_id in cluster.member_test_ids
        }
    )
    signature = representative.signature.model_copy(deep=True)

    return FailureCluster(
        cluster_id=generate_cluster_id(signature, member_test_ids),
        label=representative.label,
        signature=signature,
        member_test_ids=member_test_ids,
        member_count=len(member_test_ids),
        representative_test_id=representative.representative_test_id,
        example_message=representative.example_message,
        example_trace_snippet=representative.example_trace_snippet,
        example_step_path=representative.example_step_path,
    )
