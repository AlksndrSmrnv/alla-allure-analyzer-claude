from alla_skill.models.testops import FailedTestSummary
from alla_skill.models.common import TestStatus as Status
from alla_skill.services.clustering_service import ClusteringService, ClusteringConfig


def test_medoid_is_not_longest_error_and_input_order_does_not_matter():
    tests = [
        FailedTestSummary(
            test_result_id=i, name=f"test_{i}", status=Status.FAILED, status_message=msg
        )
        for i, msg in [
            (1, "database connection timeout"),
            (2, "database connection timeout"),
            (3, "database connection timeout extra diagnostic details in a much longer message"),
            (4, "database connection timeout"),
        ]
    ]
    config = ClusteringConfig(similarity_threshold=0.1)
    report = ClusteringService(config).cluster_failures(1, tests)
    assert report.cluster_count == 1
    cluster = report.clusters[0]
    assert cluster.representative_test_id == 1
    assert cluster.example_test_ids[:2] == [1, 3]
    assert ClusteringService(config).cluster_failures(1, list(reversed(tests))) == report


def test_thousand_failures_have_exact_coverage():
    tests = [
        FailedTestSummary(
            test_result_id=i,
            name=f"test_{i}",
            status=Status.FAILED,
            status_message=f"Error group-{i % 10} expected <200> but was: <{i % 10}>",
        )
        for i in range(1000)
    ]
    report = ClusteringService().cluster_failures(1, tests)
    ids = [tid for c in report.clusters for tid in c.member_test_ids]
    assert sorted(ids) == list(range(1000))
    assert report.cluster_count == 10
