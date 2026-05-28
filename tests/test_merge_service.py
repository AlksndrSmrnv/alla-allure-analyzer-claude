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


def _step_signature_hash(cluster, failed_tests) -> str:
    test_by_id = {test.test_result_id: test for test in failed_tests}
    context = build_feedback_cluster_context(cluster, test_by_id)
    assert context is not None
    assert context.step_issue_signature is not None, (
        "step_issue_signature должен быть, иначе тест step-aware merge не имеет смысла"
    )
    return context.step_issue_signature.signature_hash


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


# ---------------------------------------------------------------------------
# Step-aware merge rules (rule_kind="step")
# ---------------------------------------------------------------------------


def _two_clusters_same_base_different_steps() -> tuple:
    """Помощник: два кластера с одинаковым message+log (=одинаковый base hash),
    но разными step paths. Имитирует разделение, сделанное step-path hard gate
    в ClusteringService — и возвращает данные для тестов step-aware merge.
    """
    common_message = "AssertionError: условие не выполняется"
    common_trace = "at form.FieldValidator:42"
    common_log = "2026-05-28 [ERROR] AssertionError: условие не выполняется"

    cluster_pol = make_failure_cluster(
        cluster_id="cluster-pol",
        label="screen-pol",
        representative_test_id=301,
        member_test_ids=[301],
        member_count=1,
        example_message=common_message,
        example_trace_snippet=common_trace,
        example_step_path=(
            "Открыть форму → "
            "Для текста в поле qwerty выполняется условие равно screen-pol"
        ),
    )
    cluster_contract = make_failure_cluster(
        cluster_id="cluster-contract",
        label="screen-contract",
        representative_test_id=302,
        member_test_ids=[302],
        member_count=1,
        example_message=common_message,
        example_trace_snippet=common_trace,
        example_step_path=(
            "Открыть форму → "
            "Для текста в поле qwerty выполняется условие равно screen-contract"
        ),
    )
    failed_tests = [
        make_failed_test_summary(
            test_result_id=301,
            status_message=common_message,
            status_trace=common_trace,
            log_snippet=common_log,
            failed_step_path=cluster_pol.example_step_path,
        ),
        make_failed_test_summary(
            test_result_id=302,
            status_message=common_message,
            status_trace=common_trace,
            log_snippet=common_log,
            failed_step_path=cluster_contract.example_step_path,
        ),
    ]
    report = make_clustering_report(
        clusters=[cluster_pol, cluster_contract],
        cluster_count=2,
        total_failures=2,
        unclustered_count=2,
    )
    return cluster_pol, cluster_contract, failed_tests, report


def test_apply_merge_rules_step_kind_merges_same_base_different_steps() -> None:
    """rule_kind="step" объединяет кластера, у которых одинаковый base hash, но разные step hashes."""
    cluster_pol, cluster_contract, failed_tests, report = (
        _two_clusters_same_base_different_steps()
    )

    base_pol = _signature_hash(cluster_pol, failed_tests)
    base_contract = _signature_hash(cluster_contract, failed_tests)
    # Базовые хэши должны совпадать — это и есть слепое пятно старого merge UI.
    assert base_pol == base_contract

    step_pol = _step_signature_hash(cluster_pol, failed_tests)
    step_contract = _step_signature_hash(cluster_contract, failed_tests)
    assert step_pol != step_contract

    merged = apply_merge_rules(
        report,
        failed_tests,
        [
            MergeRule(
                rule_id=1,
                project_id=42,
                signature_hash_a=min(step_pol, step_contract),
                signature_hash_b=max(step_pol, step_contract),
                rule_kind="step",
            )
        ],
    )

    assert merged.cluster_count == 1
    assert sorted(merged.clusters[0].member_test_ids) == [301, 302]


def test_apply_merge_rules_base_kind_does_not_match_step_hashes() -> None:
    """rule_kind="base" со step hashes в полях ничего не сольёт — индексы независимы.

    Защищает от регрессии: step rule НЕ должен случайно сработать через base
    индекс (и наоборот), даже если хэши «выглядят как 64 hex».
    """
    cluster_pol, cluster_contract, failed_tests, report = (
        _two_clusters_same_base_different_steps()
    )
    step_pol = _step_signature_hash(cluster_pol, failed_tests)
    step_contract = _step_signature_hash(cluster_contract, failed_tests)

    merged = apply_merge_rules(
        report,
        failed_tests,
        [
            MergeRule(
                rule_id=1,
                project_id=42,
                signature_hash_a=min(step_pol, step_contract),
                signature_hash_b=max(step_pol, step_contract),
                rule_kind="base",  # неверный kind для step hashes
            )
        ],
    )

    # Правило ничего не нашло в base индексе → отчёт не изменился.
    assert merged.cluster_count == 2


def test_merge_rule_pair_defaults_rule_kind_to_base() -> None:
    """Backward compat: MergeRulePair без явного rule_kind считается «base»."""
    from alla.knowledge.merge_rules_models import MergeRulePair

    pair = MergeRulePair(
        signature_hash_a="a" * 64,
        signature_hash_b="b" * 64,
    )
    assert pair.rule_kind == "base"


def test_merge_rule_defaults_rule_kind_to_base() -> None:
    """Backward compat: MergeRule без явного rule_kind (т.е. строки из старой БД) считается «base»."""
    rule = MergeRule(
        rule_id=99,
        project_id=42,
        signature_hash_a="a" * 64,
        signature_hash_b="b" * 64,
    )
    assert rule.rule_kind == "base"
