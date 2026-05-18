"""Synchronous REST client for alla-server KB and feedback APIs."""

from __future__ import annotations

from typing import Any

import httpx

from alla.knowledge.feedback_models import (
    CreateKBEntryRequest,
    CreateKBEntryResponse,
    FeedbackRequest,
    FeedbackResolveRequest,
    FeedbackResolveResponse,
    FeedbackResponse,
    KBEntryDeleteResponse,
)
from alla.knowledge.merge_rules_models import (
    MergeRuleDeleteResponse,
    MergeRulesListResponse,
    MergeRulesRequest,
    MergeRulesResponse,
)
from alla.knowledge.models import KBEntry


class AllaApiError(Exception):
    """Base error for alla-server REST client failures."""


class AllaApiConnectionError(AllaApiError):
    """alla-server is unreachable or timed out."""


class AllaApiHTTPError(AllaApiError):
    """HTTP error response from alla-server."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        payload: Any,
    ) -> None:
        super().__init__(f"alla-server HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.payload = payload


class AllaApiValidationError(AllaApiHTTPError):
    """HTTP 422 from alla-server."""


class AllaApiNotFoundError(AllaApiHTTPError):
    """HTTP 404 from alla-server."""


class AllaApiConflictError(AllaApiHTTPError):
    """HTTP 409 from alla-server."""


class AllaApiClient:
    """Thin synchronous client over alla-server REST API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30,
        ssl_verify: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            verify=ssl_verify,
        )

    def create_kb_entry(
        self,
        req: CreateKBEntryRequest,
    ) -> tuple[CreateKBEntryResponse, bool]:
        """Create or idempotently return a KB entry."""
        response = self._request(
            "POST",
            "/api/v1/kb/entries",
            json=req.model_dump(mode="json"),
        )
        return CreateKBEntryResponse.model_validate(response.json()), response.status_code == 201

    def update_kb_entry(self, entry_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        """Update a KB entry."""
        response = self._request("PUT", f"/api/v1/kb/entries/{entry_id}", json=fields)
        data = response.json()
        return data if isinstance(data, dict) else {"response": data}

    def delete_kb_entry(
        self,
        entry_id: int,
        *,
        force: bool = False,
    ) -> KBEntryDeleteResponse:
        """Delete a KB entry, optionally cascading feedback."""
        response = self._request(
            "DELETE",
            f"/api/v1/kb/entries/{entry_id}",
            params={"force": str(force).lower()},
        )
        return KBEntryDeleteResponse.model_validate(response.json())

    def list_kb_entries(self, project_id: int | None = None) -> list[KBEntry]:
        """List KB entries visible globally or for a project."""
        params = {"project_id": project_id} if project_id is not None else None
        response = self._request("GET", "/api/v1/kb/entries", params=params)
        data = response.json()
        entries = data.get("entries", []) if isinstance(data, dict) else []
        return [KBEntry.model_validate(item) for item in entries]

    def submit_feedback(self, req: FeedbackRequest) -> FeedbackResponse:
        """Submit a like/dislike feedback vote."""
        response = self._request(
            "POST",
            "/api/v1/kb/feedback",
            json=req.model_dump(mode="json"),
        )
        return FeedbackResponse.model_validate(response.json())

    def resolve_feedback(self, req: FeedbackResolveRequest) -> FeedbackResolveResponse:
        """Resolve exact feedback votes for entry/signature pairs."""
        response = self._request(
            "POST",
            "/api/v1/kb/feedback/resolve",
            json=req.model_dump(mode="json"),
        )
        return FeedbackResolveResponse.model_validate(response.json())

    def create_merge_rules(self, req: MergeRulesRequest) -> MergeRulesResponse:
        """Create or update merge rules."""
        response = self._request(
            "POST",
            "/api/v1/merge-rules",
            json=req.model_dump(mode="json"),
        )
        return MergeRulesResponse.model_validate(response.json())

    def list_merge_rules(self, project_id: int) -> MergeRulesListResponse:
        """List merge rules for a project."""
        response = self._request(
            "GET",
            "/api/v1/merge-rules",
            params={"project_id": project_id},
        )
        return MergeRulesListResponse.model_validate(response.json())

    def delete_merge_rule(self, rule_id: int) -> MergeRuleDeleteResponse:
        """Delete one merge rule."""
        response = self._request("DELETE", f"/api/v1/merge-rules/{rule_id}")
        return MergeRuleDeleteResponse.model_validate(response.json())

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "AllaApiClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(method, path, params=params, json=json)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            raise AllaApiConnectionError(f"Не удалось подключиться к alla-server: {exc}") from exc

        if response.status_code >= 400:
            self._raise_for_status(response)
        return response

    def _raise_for_status(self, response: httpx.Response) -> None:
        payload = _decode_error_payload(response)
        detail = _extract_error_detail(payload)
        error_cls: type[AllaApiHTTPError]
        if response.status_code == 422:
            error_cls = AllaApiValidationError
        elif response.status_code == 404:
            error_cls = AllaApiNotFoundError
        elif response.status_code == 409:
            error_cls = AllaApiConflictError
        else:
            error_cls = AllaApiHTTPError
        raise error_cls(response.status_code, detail, payload)


def _decode_error_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"detail": response.text[:500]}


def _extract_error_detail(payload: Any) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict) and isinstance(detail.get("detail"), str):
            return detail["detail"]
        return str(detail)
    return str(payload)
