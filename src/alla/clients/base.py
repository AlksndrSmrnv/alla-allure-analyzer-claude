"""Abstract interface for test results data sources."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from alla.models.common import PageResponse
from alla.models.testops import LaunchResponse, TestResultResponse


@runtime_checkable
class TestResultsProvider(Protocol):
    """Protocol defining what any test-results data source must provide.

    Implementations:
    - AllureTestOpsClient (MVP): fetches from Allure TestOps HTTP API
    - Future: LocalAllureReportClient (reads allure-report JSON files)
    - Future: CachedTestResultsClient (reads from local DB/cache)
    """

    async def get_launch(self, launch_id: int) -> LaunchResponse:
        """Fetch launch metadata by ID."""
        ...

    async def get_test_result(self, test_result_id: int) -> TestResultResponse:
        """Fetch full details for a single test result by ID."""
        ...

    async def get_test_results_for_launch(
        self,
        launch_id: int,
        page: int = 0,
        size: int = 100,
    ) -> PageResponse[TestResultResponse]:
        """Fetch a page of test results for a given launch."""
        ...

    async def get_all_test_results_for_launch(
        self, launch_id: int,
    ) -> list[TestResultResponse]:
        """Fetch ALL test results for a launch, handling pagination."""
        ...
