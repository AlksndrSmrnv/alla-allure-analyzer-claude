"""Triage service: fetch test results, filter failures, produce summary."""

from __future__ import annotations

import logging
from collections import Counter

from alla.clients.base import TestResultsProvider
from alla.config import Settings
from alla.models.common import TestStatus
from alla.models.testops import FailedTestSummary, TestResultResponse, TriageReport

logger = logging.getLogger(__name__)


class TriageService:
    """Orchestrates the test failure triage workflow.

    Phase 1 (MVP): fetch and summarize failed tests.
    Future phases will add clustering, LLM analysis, knowledge base lookup.
    """

    def __init__(self, client: TestResultsProvider, settings: Settings) -> None:
        self._client = client
        self._endpoint = str(settings.endpoint).rstrip("/")

    async def analyze_launch(self, launch_id: int) -> TriageReport:
        """Fetch test results for a launch and produce a triage report.

        Steps:
            1. Fetch launch metadata (name, closed status).
            2. Fetch all test results for the launch (paginated).
            3. Count results by status.
            4. Build FailedTestSummary for each failed/broken test.
            5. Return TriageReport.
        """
        # 1. Launch metadata
        launch = await self._client.get_launch(launch_id)
        logger.info("Analyzing launch #%d (%s)", launch_id, launch.name or "unnamed")

        # 2. All test results
        results = await self._client.get_all_test_results_for_launch(launch_id)

        # 3. Count by status
        status_counts = Counter(
            self._normalize_status(r.status) for r in results
        )

        # 4. Failed/broken summaries
        failure_statuses = TestStatus.failure_statuses()
        failed_tests = [
            self._build_failed_summary(r, launch_id)
            for r in results
            if self._normalize_status(r.status) in failure_statuses
        ]

        report = TriageReport(
            launch_id=launch_id,
            launch_name=launch.name,
            total_results=len(results),
            passed_count=status_counts.get(TestStatus.PASSED, 0),
            failed_count=status_counts.get(TestStatus.FAILED, 0),
            broken_count=status_counts.get(TestStatus.BROKEN, 0),
            skipped_count=status_counts.get(TestStatus.SKIPPED, 0),
            unknown_count=status_counts.get(TestStatus.UNKNOWN, 0),
            failed_tests=failed_tests,
        )

        self._log_report(report)
        return report

    # --- Internal helpers ---

    @staticmethod
    def _normalize_status(raw: str | None) -> TestStatus:
        """Convert raw status string to TestStatus enum, defaulting to UNKNOWN."""
        if raw is None:
            return TestStatus.UNKNOWN
        try:
            return TestStatus(raw.lower())
        except ValueError:
            return TestStatus.UNKNOWN

    def _build_failed_summary(
        self, result: TestResultResponse, launch_id: int,
    ) -> FailedTestSummary:
        """Convert a raw test result into a triage-focused summary."""
        status_message = None
        if result.status_details and isinstance(result.status_details, dict):
            status_message = result.status_details.get("message")

        link = (
            f"{self._endpoint}/launch/{launch_id}/testresult/{result.id}"
        )

        return FailedTestSummary(
            test_result_id=result.id,
            name=result.name or f"test-result-{result.id}",
            full_name=result.full_name,
            status=self._normalize_status(result.status),
            category=result.category,
            status_message=status_message,
            test_case_id=result.test_case_id,
            link=link,
            duration_ms=result.duration,
        )

    @staticmethod
    def _log_report(report: TriageReport) -> None:
        """Log the triage report summary."""
        logger.info(
            "Launch #%d (%s): %d total | passed=%d failed=%d broken=%d skipped=%d unknown=%d",
            report.launch_id,
            report.launch_name or "unnamed",
            report.total_results,
            report.passed_count,
            report.failed_count,
            report.broken_count,
            report.skipped_count,
            report.unknown_count,
        )

        if report.failed_tests:
            logger.info("Failures (%d):", report.failure_count)
            for t in report.failed_tests:
                logger.info(
                    "  [%s] %s (ID: %d) %s",
                    t.status.value.upper(),
                    t.name,
                    t.test_result_id,
                    t.link or "",
                )
        else:
            logger.info("No failures found.")
