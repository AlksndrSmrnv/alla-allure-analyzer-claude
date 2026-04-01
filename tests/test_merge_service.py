"""Unit-тесты post-clustering merge rules."""

from __future__ import annotations

from alla.knowledge.feedback_signature import build_feedback_cluster_context
from alla.knowledge.merge_rules_models import MergeRule
from alla.services.merge_service import apply_merge_rules

from conftest import (
    make_clustering_report,
    make_failed_test_summary,
    make_failure_cluster,
)


def _signature_hash(cluster, failed_tests) -> str:
    test_by_id = {test.test_result_id: test for test in failed_tests}
    context = build_feedback_cluster_context(cluster, test_by_id)
    assert context is not None
    return context.base_issue_signature.signature_hash


def test_apply_merge_rules_merges_clusters_transitively() -> None:
    """Правила A-B и B-C должны транзитивно слить три компоненты."""
    cluster_a = make_failure_cluster(
        cluster_id="cluster-a",
        label="Gateway timeout",
        representative_test_id=1,
        member_test_ids=[1, 2, 3],
        member_count=3,
        example_message="Gateway timeout while saving order",
        example_trace_snippet="at gateway.TimeoutService:42",
    )
    cluster_b = make_failure_cluster(
        cluster_id="cluster-b",
        label="Connection refused",
        representative_test_id=4,
        member_test_ids=[4],
        member_count=1,
        example_message="Connection refused while saving order",
        example_trace_snippet="at gateway.HttpClient:77",
    )
    cluster_c = make_failure_cluster(
        cluster_id="cluster-c",
        label="Upstream HTTP 500",
        representative_test_id=5,
        member_test_ids=[5, 6],
        member_count=2,
        example_message="Upstream HTTP 500 while saving order",
        example_trace_snippet="at gateway.UpstreamApi:18",
    )
    failed_tests = [
        make_failed_test_summary(
            test_result_id=1,
            status_message=cluster_a.example_message,
            status_trace=cluster_a.example_trace_snippet,
            log_snippet="2026-03-31 [ERROR] Gateway timeout while saving order",
        ),
        make_failed_test_summary(
            test_result_id=4,
            status_message=cluster_b.example_message,
            status_trace=cluster_b.example_trace_snippet,
            log_snippet="2026-03-31 [ERROR] Connection refused while saving order",
        ),
        make_failed_test_summary(
            test_result_id=5,
            status_message=cluster_c.example_message,
            status_trace=cluster_c.example_trace_snippet,
            log_snippet="2026-03-31 [ERROR] Upstream HTTP 500 while saving order",
        ),
    ]
    report = make_clustering_report(
        clusters=[cluster_a, cluster_b, cluster_c],
        cluster_count=3,
        total_failures=6,
    )
    hash_a = _signature_hash(cluster_a, failed_tests)
    hash_b = _signature_hash(cluster_b, failed_tests)
    hash_c = _signature_hash(cluster_c, failed_tests)

    merged = apply_merge_rules(
        report,
        failed_tests,
        [
            MergeRule(
                rule_id=1,
                project_id=42,
                signature_hash_a=min(hash_a, hash_b),
                signature_hash_b=max(hash_a, hash_b),
            ),
            MergeRule(
                rule_id=2,
                project_id=42,
                signature_hash_a=min(hash_b, hash_c),
                signature_hash_b=max(hash_b, hash_c),
            ),
        ],
    )

    assert merged.cluster_count == 1
    assert merged.unclustered_count == 0
    assert merged.clusters[0].member_test_ids == [1, 2, 3, 4, 5, 6]
    assert merged.clusters[0].member_count == 6
    assert merged.clusters[0].representative_test_id == 1
    assert merged.clusters[0].label == cluster_a.label


def test_apply_merge_rules_skips_missing_signature_pairs() -> None:
    """Если правило ссылается на отсутствующую сигнатуру, отчёт не меняется."""
    cluster = make_failure_cluster(
        cluster_id="cluster-a",
        representative_test_id=10,
        member_test_ids=[10],
        member_count=1,
        example_message="Gateway timeout while saving order",
        example_trace_snippet="at gateway.TimeoutService:42",
    )
    no_signal_cluster = make_failure_cluster(
        cluster_id="cluster-empty",
        representative_test_id=11,
        member_test_ids=[11],
        member_count=1,
        example_message=None,
        example_trace_snippet=None,
    )
    failed_tests = [
        make_failed_test_summary(
            test_result_id=10,
            status_message=cluster.example_message,
            status_trace=cluster.example_trace_snippet,
            log_snippet="2026-03-31 [ERROR] Gateway timeout while saving order",
        ),
        make_failed_test_summary(
            test_result_id=11,
            status_message=None,
            status_trace=None,
            log_snippet=None,
        ),
    ]
    report = make_clustering_report(
        clusters=[cluster, no_signal_cluster],
        cluster_count=2,
        total_failures=2,
        unclustered_count=2,
    )
    hash_a = _signature_hash(cluster, failed_tests)

    merged = apply_merge_rules(
        report,
        failed_tests,
        [
            MergeRule(
                rule_id=1,
                project_id=42,
                signature_hash_a=min(hash_a, "f" * 64),
                signature_hash_b=max(hash_a, "f" * 64),
            )
        ],
    )

    assert merged.cluster_count == 2
    assert [cluster.cluster_id for cluster in merged.clusters] == [
        "cluster-a",
        "cluster-empty",
    ]


def test_apply_merge_rules_unions_all_clusters_for_same_signature_bucket() -> None:
    """Если одна сигнатура встречается в нескольких кластерах, правило захватывает их все."""
    cluster_a1 = make_failure_cluster(
        cluster_id="cluster-a1",
        label="Gateway timeout at checkout",
        representative_test_id=21,
        member_test_ids=[21, 22],
        member_count=2,
        example_message="Gateway timeout while saving order",
        example_trace_snippet="at checkout.TimeoutService:42",
        example_step_path="Checkout > Save order",
    )
    cluster_a2 = make_failure_cluster(
        cluster_id="cluster-a2",
        label="Gateway timeout at payment",
        representative_test_id=23,
        member_test_ids=[23],
        member_count=1,
        example_message="Gateway timeout while saving order",
        example_trace_snippet="at checkout.TimeoutService:42",
        example_step_path="Checkout > Confirm payment",
    )
    cluster_b = make_failure_cluster(
        cluster_id="cluster-b",
        representative_test_id=24,
        member_test_ids=[24],
        member_count=1,
        example_message="Gateway unreachable while saving order",
        example_trace_snippet="at gateway.HttpClient:77",
    )
    failed_tests = [
        make_failed_test_summary(
            test_result_id=21,
            status_message=cluster_a1.example_message,
            status_trace=cluster_a1.example_trace_snippet,
            log_snippet="2026-03-31 [ERROR] Gateway timeout while saving order",
        ),
        make_failed_test_summary(
            test_result_id=23,
            status_message=cluster_a2.example_message,
            status_trace=cluster_a2.example_trace_snippet,
            log_snippet="2026-03-31 [ERROR] Gateway timeout while saving order",
        ),
        make_failed_test_summary(
            test_result_id=24,
            status_message=cluster_b.example_message,
            status_trace=cluster_b.example_trace_snippet,
            log_snippet="2026-03-31 [ERROR] Gateway unreachable while saving order",
        ),
    ]
    report = make_clustering_report(
        clusters=[cluster_a1, cluster_a2, cluster_b],
        cluster_count=3,
        total_failures=4,
    )
    hash_a = _signature_hash(cluster_a1, failed_tests)
    assert hash_a == _signature_hash(cluster_a2, failed_tests)
    hash_b = _signature_hash(cluster_b, failed_tests)

    merged = apply_merge_rules(
        report,
        failed_tests,
        [
            MergeRule(
                rule_id=1,
                project_id=42,
                signature_hash_a=min(hash_a, hash_b),
                signature_hash_b=max(hash_a, hash_b),
            )
        ],
    )

    assert merged.cluster_count == 1
    assert merged.clusters[0].member_test_ids == [21, 22, 23, 24]
    assert merged.clusters[0].representative_test_id == 21
