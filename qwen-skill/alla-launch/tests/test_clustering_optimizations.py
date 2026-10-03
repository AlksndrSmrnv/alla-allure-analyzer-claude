"""Regression checks for log evidence and disabling its channel."""

import pytest
from skill_fixtures import without_libmagic  # noqa: F401

from alla_core.services import clustering_service as clustering
from alla_core.models.testops import FailedTestSummary


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
