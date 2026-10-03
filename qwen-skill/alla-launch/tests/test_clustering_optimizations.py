"""Regression checks for log evidence and disabling its channel."""

from unittest.mock import patch

import numpy as np
import pytest
from skill_fixtures import without_libmagic  # noqa: F401

from alla_core.services import clustering_service as clustering
from alla_core.models.testops import FailedTestSummary


def test_disabled_logs_do_not_change_step_penalty_or_build_similarity() -> None:
    service = clustering.ClusteringService(clustering.ClusteringConfig(
        similarity_threshold=0.8, log_similarity_weight=0.0, step_path_strict_threshold=0.0,
    ))
    log_calls: list[list[str]] = []

    def similarity(documents: list[str]) -> np.ndarray:
        if documents[0] == "log":
            log_calls.append(documents)
        return np.array([[1.0, 0.5], [0.5, 1.0]]) if documents[0] == "step" else np.ones((2, 2))

    with patch.object(service, "_pairwise_similarity", side_effect=similarity):
        with_log = service._cluster_texts(["message"] * 2, [""] * 2, ["log"] * 2, ["step"] * 2)
        without_log = service._cluster_texts(["message"] * 2, [""] * 2, None, ["step"] * 2)
    assert with_log == without_log
    assert len(set(with_log)) == 2
    assert log_calls == []



@pytest.mark.parametrize("truncated", [False, True])
def test_log_document_strips_selection_markers_only_for_core_truncation(truncated: bool) -> None:
    failure = FailedTestSummary(
        test_result_id=1, name="test", status="failed", log_selection_truncated=truncated,
        log_snippet="--- [файл: app.log] ---\nbackend pool exhausted\n\n"
                    "[... обрезано: было 123456 символов, оставлено 1000 ...]",
    )
    document = clustering._build_log_document(failure, head_lines=50, tail_lines=50)
    assert "backend pool exhausted" in document
    assert ("обрезано" in document) is not truncated
