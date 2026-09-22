"""Protect copied grouping behavior; representative/triage differences are intentional."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qwen-skill/alla-analysis/scripts"))
from alla.models.testops import FailedTestSummary as ServerFailure
from alla.services.clustering_service import (
    ClusteringConfig as ServerConfig,
    ClusteringService as ServerService,
)
from alla_skill.models.testops import FailedTestSummary as SkillFailure
from alla_skill.services.clustering_service import (
    ClusteringConfig as SkillConfig,
    ClusteringService as SkillService,
)


@pytest.mark.parametrize(
    "threshold,log_weight,step_threshold", [(0.6, 0.15, 0.95), (0.8, 0, 0.95), (0.4, 0.3, 0)]
)
def test_group_members_match_for_equal_inputs_and_config(threshold, log_weight, step_threshold):
    payloads = [
        dict(
            test_result_id=i,
            name=f"test-{i}",
            status="failed",
            status_message=message,
            status_trace="at common.Framework.run(Test.java:2)",
            failed_step_path=step,
            log_snippet="[ERROR] HTTP/1.1 500 downstream unavailable" if i % 2 else None,
        )
        for i, (message, step) in enumerate(
            [
                ("expected <200> but was: <500>", "Request → Status"),
                ("expected <200> but was: <500>", "Request → Status"),
                ("expected <200> but was: <404>", "Request → Status"),
                ("Timeout waiting for user 123456", "Login → Submit"),
                ("Timeout waiting for user 987654", "Login → Submit"),
                ("Timeout waiting for user 123456", "Checkout → Pay"),
                (None, None),
            ],
            1,
        )
    ]
    args = dict(
        similarity_threshold=threshold,
        log_similarity_weight=log_weight,
        step_path_strict_threshold=step_threshold,
    )
    server = ServerService(ServerConfig(**args)).cluster_failures(
        1, [ServerFailure(**p) for p in payloads]
    )
    skill = SkillService(SkillConfig(**args)).cluster_failures(
        1, [SkillFailure(**p) for p in payloads]
    )
    assert sorted(sorted(c.member_test_ids) for c in server.clusters) == sorted(
        sorted(c.member_test_ids) for c in skill.clusters
    )
