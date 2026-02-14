"""Общие фабрики и фикстуры для тестов alla."""

from __future__ import annotations

from alla.knowledge.models import (
    KBEntry,
    KBMatchResult,
    RootCauseCategory,
)
from alla.models.clustering import ClusterSignature, ClusteringReport, FailureCluster
from alla.models.common import TestStatus
from alla.models.testops import CommentResponse, ExecutionStep, FailedTestSummary, TriageReport


def make_kb_entry(**overrides) -> KBEntry:
    """Фабрика KBEntry с разумными дефолтами."""
    defaults = {
        "id": "test_entry",
        "title": "Test Entry",
        "description": "A test KB entry",
        "error_example": "test error",
        "category": RootCauseCategory.SERVICE,
        "resolution_steps": ["Fix the issue"],
    }
    defaults.update(overrides)
    return KBEntry.model_validate(defaults)


def make_execution_step(**overrides) -> ExecutionStep:
    """Фабрика ExecutionStep с дефолтами."""
    defaults: dict = {}
    defaults.update(overrides)
    return ExecutionStep.model_validate(defaults)


def make_failure_cluster(**overrides) -> FailureCluster:
    """Фабрика FailureCluster с разумными дефолтами."""
    defaults: dict = {
        "cluster_id": "c1",
        "label": "NullPointerException in Service",
        "signature": ClusterSignature(),
        "member_test_ids": [1, 2],
        "member_count": 2,
        "example_message": "NPE at line 42",
        "example_trace_snippet": "at Service.java:42",
    }
    defaults.update(overrides)
    return FailureCluster.model_validate(defaults)


def make_clustering_report(**overrides) -> ClusteringReport:
    """Фабрика ClusteringReport с разумными дефолтами."""
    defaults: dict = {
        "launch_id": 1,
        "total_failures": 2,
        "cluster_count": 1,
        "clusters": [make_failure_cluster()],
        "unclustered_count": 0,
    }
    defaults.update(overrides)
    return ClusteringReport.model_validate(defaults)


def make_comment_response(**overrides) -> CommentResponse:
    """Фабрика CommentResponse с разумными дефолтами."""
    defaults: dict = {
        "id": 1,
        "body": "[alla] Test comment",
        "testCaseId": 100,
    }
    defaults.update(overrides)
    return CommentResponse.model_validate(defaults)


def make_failed_test_summary(**overrides) -> FailedTestSummary:
    """Фабрика FailedTestSummary с разумными дефолтами."""
    defaults: dict = {
        "test_result_id": 1,
        "name": "test_example",
        "status": TestStatus.FAILED,
    }
    defaults.update(overrides)
    return FailedTestSummary.model_validate(defaults)


def make_triage_report(**overrides) -> TriageReport:
    """Фабрика TriageReport с разумными дефолтами."""
    defaults: dict = {
        "launch_id": 1,
        "total_results": 10,
        "passed_count": 8,
        "failed_count": 2,
    }
    defaults.update(overrides)
    return TriageReport.model_validate(defaults)


def make_kb_match_result(**overrides) -> KBMatchResult:
    """Фабрика KBMatchResult с разумными дефолтами."""
    defaults: dict = {
        "entry": make_kb_entry(),
        "score": 0.8,
        "matched_on": ["example_similarity"],
    }
    defaults.update(overrides)
    if isinstance(defaults.get("entry"), dict):
        defaults["entry"] = make_kb_entry(**defaults["entry"])
    return KBMatchResult.model_validate(defaults)
