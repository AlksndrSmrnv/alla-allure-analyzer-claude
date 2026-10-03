"""Regression checks for condensed distances and disabling the log channel."""

from unittest.mock import patch

import numpy as np
import pytest
from skill_fixtures import without_libmagic  # noqa: F401

from alla_core.services import clustering_service as clustering
from alla_core.models.testops import FailedTestSummary


@pytest.mark.parametrize("documents", [["a !", "word tokens"], ["a !", "", "word tokens"]])
def test_tokenless_nonempty_documents_keep_unit_diagonal(documents: list[str]) -> None:
    matrix = clustering.ClusteringService()._pairwise_similarity(documents)
    np.testing.assert_array_equal(matrix, np.eye(len(documents)))


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


@pytest.mark.parametrize("strict", [0.0, 0.5])
def test_condensed_distances_preserve_gates_fallback_and_step_penalties(strict: float) -> None:
    # Captured from the original algorithm: message/log gates, missing channels,
    # assertion gate and step penalties all contribute to this small example.
    matrices = [
        [[1, .75, .2, .1, .65], [.75, 1, .4, .2, .9], [.2, .4, 1, .3, .1],
         [.1, .2, .3, 1, .5], [.65, .9, .1, .5, 1]],
        [[1, .4, .8, .7, .2], [.4, 1, .3, .1, 0], [.8, .3, 1, .6, .4],
         [.7, .1, .6, 1, .2], [.2, 0, .4, .2, 1]],
        [[1, .8, .2, .9, .95], [.8, 1, .1, .7, .4], [.2, .1, 1, .2, .1],
         [.9, .7, .2, 1, .6], [.95, .4, .1, .6, 1]],
        [[1, .8, .2, .5, .9], [.8, 1, .3, .5, .8], [.2, .3, 1, .5, .1],
         [.5, .5, .5, 1, .5], [.9, .8, .1, .5, 1]],
    ]
    expected = [0.2884782608695652, 1.0, 0.2, 0.33336956521739125,
                1.0 if strict else 0.915, 0.3, 0.2102173913043478, 1.0, 1.0, 0.4]
    service = clustering.ClusteringService(clustering.ClusteringConfig(
        log_similarity_weight=0.15, step_path_strict_threshold=strict,
    ))
    with patch.object(service, "_pairwise_similarity", side_effect=[np.array(m) for m in matrices]):
        with patch.object(clustering, "linkage", wraps=clustering.linkage) as linkage:
            service._cluster_texts(
                ["m0", "m1", "m2", "", "m4"], ["t0", "", "t2", "t3", ""],
                ["l0", "l1", "", "l3", "l4"], ["s0", "s1", "s2", "", "s4"],
                assertion_actuals=[None, None, "x", "y", None],
            )
    np.testing.assert_allclose(linkage.call_args.args[0], expected, atol=1e-14)


def test_non_debug_clustering_does_not_compute_similarity_statistics() -> None:
    service = clustering.ClusteringService()
    with patch.object(service, "_similarity_stats", side_effect=AssertionError("unexpected stats")):
        assert service._cluster_texts(["same message"] * 2, [""] * 2) == [1, 1]


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
