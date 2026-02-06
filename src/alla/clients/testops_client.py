"""Allure TestOps HTTP client implementation."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from alla.clients.auth import AllureAuthManager
from alla.config import Settings
from alla.exceptions import AllureApiError, PaginationLimitError
from alla.models.common import PageResponse
from alla.models.testops import LaunchResponse, TestResultResponse

logger = logging.getLogger(__name__)


class AllureTestOpsClient:
    """HTTP client for Allure TestOps REST API.

    Implements the :class:`~alla.clients.base.TestResultsProvider` protocol.

    Endpoint paths are class attributes so they can be overridden if the
    target Allure TestOps version uses a different API structure.
    """

    LAUNCH_ENDPOINT = "/api/launch"
    TESTRESULT_ENDPOINT = "/api/testresult"

    def __init__(self, settings: Settings, auth_manager: AllureAuthManager) -> None:
        self._endpoint = str(settings.endpoint).rstrip("/")
        self._auth = auth_manager
        self._page_size = settings.page_size
        self._max_pages = settings.max_pages
        self._http = httpx.AsyncClient(
            timeout=settings.request_timeout,
            verify=settings.ssl_verify,
        )

    # --- Public API (TestResultsProvider protocol) ---

    async def get_launch(self, launch_id: int) -> LaunchResponse:
        """Fetch launch metadata by ID.

        ``GET /api/launch/{id}``
        """
        data = await self._request("GET", f"{self.LAUNCH_ENDPOINT}/{launch_id}")
        return LaunchResponse.model_validate(data)

    async def get_test_results_for_launch(
        self,
        launch_id: int,
        page: int = 0,
        size: int | None = None,
    ) -> PageResponse[TestResultResponse]:
        """Fetch a single page of test results for a given launch.

        ``GET /api/testresult?launchId={id}&page={page}&size={size}``
        """
        params: dict[str, Any] = {
            "launchId": launch_id,
            "page": page,
            "size": size or self._page_size,
        }
        data = await self._request("GET", self.TESTRESULT_ENDPOINT, params=params)
        return PageResponse[TestResultResponse].model_validate(data)

    async def get_all_test_results_for_launch(
        self, launch_id: int,
    ) -> list[TestResultResponse]:
        """Fetch ALL test results for a launch, iterating through pages.

        Stops when all pages are retrieved or ``max_pages`` safety limit
        is reached.
        """
        all_results: list[TestResultResponse] = []
        page = 0

        while True:
            page_resp = await self.get_test_results_for_launch(
                launch_id, page=page, size=self._page_size,
            )
            all_results.extend(page_resp.content)

            logger.debug(
                "Fetched page %d/%d (%d results so far)",
                page + 1,
                page_resp.total_pages,
                len(all_results),
            )

            if page + 1 >= page_resp.total_pages:
                break

            page += 1
            if page >= self._max_pages:
                logger.warning(
                    "Reached max_pages limit (%d). %d/%d total results fetched.",
                    self._max_pages,
                    len(all_results),
                    page_resp.total_elements,
                )
                raise PaginationLimitError(
                    f"Exceeded max_pages={self._max_pages}. "
                    f"Fetched {len(all_results)}/{page_resp.total_elements} results. "
                    f"Increase ALLURE_MAX_PAGES if needed."
                )

        logger.info(
            "Fetched %d test results for launch %d (%d pages)",
            len(all_results),
            launch_id,
            page + 1,
        )
        return all_results

    # --- Internal HTTP ---

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an authenticated HTTP request with retry on 401.

        Raises:
            AllureApiError: On HTTP error responses.
            AuthenticationError: If re-authentication also fails.
        """
        url = f"{self._endpoint}{path}"
        auth_header = await self._auth.get_auth_header()

        logger.debug("%s %s params=%s", method, url, params)

        try:
            resp = await self._http.request(
                method, url, params=params, headers=auth_header,
            )
        except httpx.RequestError as exc:
            raise AllureApiError(0, str(exc), path) from exc

        # Retry once on 401 (expired JWT)
        if resp.status_code == 401:
            logger.debug("Got 401, re-authenticating and retrying")
            self._auth.invalidate()
            auth_header = await self._auth.get_auth_header()
            try:
                resp = await self._http.request(
                    method, url, params=params, headers=auth_header,
                )
            except httpx.RequestError as exc:
                raise AllureApiError(0, str(exc), path) from exc

        if resp.status_code == 404:
            raise AllureApiError(
                404,
                f"Endpoint not found. Check your Allure TestOps version. "
                f"Swagger UI: {self._endpoint}/swagger-ui.html",
                path,
            )

        if resp.status_code >= 400:
            body_text = resp.text[:500]
            raise AllureApiError(resp.status_code, body_text, path)

        return resp.json()  # type: ignore[no-any-return]

    # --- Lifecycle ---

    async def close(self) -> None:
        """Release HTTP client and auth manager resources."""
        await self._http.aclose()
        await self._auth.close()

    async def __aenter__(self) -> AllureTestOpsClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
