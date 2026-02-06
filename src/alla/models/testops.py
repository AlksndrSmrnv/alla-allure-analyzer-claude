"""Pydantic models for Allure TestOps API responses and domain objects."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from alla.models.common import TestStatus


class TestResultResponse(BaseModel):
    """Raw test result from Allure TestOps API.

    Fields are intentionally Optional where the API might not return them,
    making the model resilient to API variations. ``extra="allow"`` captures
    any undocumented fields without validation errors.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    name: str | None = None
    full_name: str | None = Field(None, alias="fullName")
    status: str | None = None
    status_details: dict | None = Field(None, alias="statusDetails")
    duration: int | None = None
    test_case_id: int | None = Field(None, alias="testCaseId")
    test_case_name: str | None = Field(None, alias="testCaseName")
    launch_id: int | None = Field(None, alias="launchId")
    created_date: int | None = Field(None, alias="createdDate")
    category: str | None = None
    muted: bool = False
    hidden: bool = False


class LaunchResponse(BaseModel):
    """Launch metadata from Allure TestOps API."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    name: str | None = None
    closed: bool = False
    created_date: int | None = Field(None, alias="createdDate")


class FailedTestSummary(BaseModel):
    """Domain model: summarized view of a failed test for triage output."""

    test_result_id: int
    name: str
    full_name: str | None = None
    status: TestStatus
    category: str | None = None
    status_message: str | None = None
    status_trace: str | None = None
    test_case_id: int | None = None
    link: str | None = None
    duration_ms: int | None = None


class TriageReport(BaseModel):
    """Output of the triage step: summary of a launch's failures."""

    launch_id: int
    launch_name: str | None = None
    total_results: int
    passed_count: int = 0
    failed_count: int = 0
    broken_count: int = 0
    skipped_count: int = 0
    unknown_count: int = 0
    failed_tests: list[FailedTestSummary] = []

    @property
    def failure_count(self) -> int:
        return self.failed_count + self.broken_count
