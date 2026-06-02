"""Tests for the synchronous alla-server REST client."""

from __future__ import annotations

import httpx
import pytest

from alla.clients.alla_api_client import (
    AllaApiClient,
    AllaApiConflictError,
    AllaApiConnectionError,
    AllaApiHTTPError,
    AllaApiNotFoundError,
    AllaApiValidationError,
)
from alla.knowledge.feedback_models import (
    CreateKBEntryRequest,
    FeedbackRequest,
    FeedbackResolveRequest,
    FeedbackVote,
)
from alla.knowledge.merge_rules_models import MergeRulesRequest


def _client(handler) -> AllaApiClient:
    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://alla.test",
    )
    return AllaApiClient("http://alla.test", client=http)


def test_create_kb_entry_serializes_request_and_reports_created() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["json"] = request.read().decode()
        return httpx.Response(
            201,
            json={
                "entry_id": 7,
                "id": "connection_timeout_241f9620",
                "title": "Connection timeout",
                "category": "service",
                "created": True,
            },
        )

    client = _client(handler)
    response, created = client.create_kb_entry(
        CreateKBEntryRequest(
            title="Connection timeout",
            error_example="socket.timeout: 30s",
            step_path="Login -> Submit",
            project_id=1,
        )
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/kb/entries"
    assert '"title":"Connection timeout"' in captured["json"]
    assert response.entry_id == 7
    assert response.id == "connection_timeout_241f9620"
    assert created is True


def test_create_kb_entry_treats_200_as_idempotent_existing_entry() -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            json={
                "entry_id": 7,
                "id": "connection_timeout_241f9620",
                "title": "Connection timeout",
                "category": "service",
                "created": False,
            },
        )
    )

    response, created = client.create_kb_entry(
        CreateKBEntryRequest(title="Connection timeout", error_example="socket.timeout: 30s")
    )

    assert response.entry_id == 7
    assert response.created is False
    assert created is False


def test_update_kb_entry_returns_json_dict() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"entry_id": 7, "updated": True})

    response = _client(handler).update_kb_entry(7, {"title": "New title"})

    assert captured == {
        "method": "PUT",
        "path": "/api/v1/kb/entries/7",
        "body": '{"title":"New title"}',
    }
    assert response == {"entry_id": 7, "updated": True}


def test_delete_kb_entry_sends_force_query_and_parses_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["force"] = request.url.params["force"]
        return httpx.Response(200, json={"entry_id": 7, "deleted": True})

    response = _client(handler).delete_kb_entry(7, force=True)

    assert captured == {"path": "/api/v1/kb/entries/7", "force": "true"}
    assert response.entry_id == 7
    assert response.deleted is True


def test_list_kb_entries_parses_kb_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/kb/entries"
        assert request.url.params["project_id"] == "42"
        return httpx.Response(
            200,
            json={
                "count": 1,
                "entries": [
                    {
                        "entry_id": 5,
                        "id": "gateway_timeout_abc12345",
                        "title": "Gateway timeout",
                        "description": "desc",
                        "error_example": "timeout",
                        "step_path": None,
                        "category": "service",
                        "resolution_steps": ["retry"],
                        "project_id": 42,
                        "error_example_chars": 7,
                        "resolution_steps_count": 1,
                    }
                ],
            },
        )

    entries = _client(handler).list_kb_entries(42)

    assert len(entries) == 1
    assert entries[0].entry_id == 5
    assert entries[0].id == "gateway_timeout_abc12345"


def test_submit_feedback_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/kb/feedback"
        return httpx.Response(
            200,
            json={
                "kb_entry_id": 5,
                "audit_text_preview": "audit",
                "vote": "like",
                "created": True,
                "feedback_id": 8,
            },
        )

    response = _client(handler).submit_feedback(
        FeedbackRequest(
            kb_entry_id=5,
            audit_text="audit",
            vote=FeedbackVote.LIKE,
            issue_signature_hash="a" * 64,
        )
    )

    assert response.feedback_id == 8
    assert response.vote == FeedbackVote.LIKE


def test_resolve_feedback_parses_votes() -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            json={"votes": {"5:c1": {"vote": "dislike", "feedback_id": 9}}},
        )
    )

    response = client.resolve_feedback(
        FeedbackResolveRequest(
            items=[
                {
                    "kb_entry_id": 5,
                    "issue_signature_hash": "b" * 64,
                    "cluster_id": "c1",
                }
            ]
        )
    )

    assert response.votes["5:c1"].vote == FeedbackVote.DISLIKE
    assert response.votes["5:c1"].feedback_id == 9


def test_merge_rules_methods_use_expected_endpoints() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "rules": [
                        {
                            "rule_id": 1,
                            "project_id": 42,
                            "signature_hash_a": "a" * 64,
                            "signature_hash_b": "b" * 64,
                        }
                    ],
                    "created_count": 1,
                    "updated_count": 0,
                },
            )
        if request.method == "GET":
            assert request.url.params["project_id"] == "42"
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(200, json={"rule_id": 1, "deleted": True})

    client = _client(handler)
    created = client.create_merge_rules(
        MergeRulesRequest(
            project_id=42,
            pairs=[
                {
                    "signature_hash_a": "a" * 64,
                    "signature_hash_b": "b" * 64,
                }
            ],
        )
    )
    listed = client.list_merge_rules(42)
    deleted = client.delete_merge_rule(1)

    assert created.created_count == 1
    assert listed.rules == []
    assert deleted.deleted is True
    assert seen == [
        ("POST", "/api/v1/merge-rules"),
        ("GET", "/api/v1/merge-rules"),
        ("DELETE", "/api/v1/merge-rules/1"),
    ]


@pytest.mark.parametrize(
    ("status_code", "exc_type"),
    [
        (404, AllaApiNotFoundError),
        (409, AllaApiConflictError),
        (422, AllaApiValidationError),
        (500, AllaApiHTTPError),
    ],
)
def test_http_errors_are_mapped_to_specific_exceptions(status_code: int, exc_type: type[Exception]) -> None:
    client = _client(
        lambda request: httpx.Response(
            status_code,
            json={"detail": "broken", "feedback_count": 2},
        )
    )

    with pytest.raises(exc_type) as err:
        client.list_kb_entries()

    assert err.value.status_code == status_code
    assert err.value.detail == "broken"
    assert err.value.payload["feedback_count"] == 2


def test_create_skill_run_posts_triage_and_clustering() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "run_id": 42, "clusters": []})

    response = _client(handler).create_skill_run(
        {"launch_id": 1}, {"cluster_count": 0}
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/skill/runs"
    assert '"triage_report"' in captured["json"]
    assert '"clustering_report"' in captured["json"]
    assert response["run_id"] == 42


def test_create_skill_run_allows_null_clustering() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "run_id": 7})

    _client(handler).create_skill_run({"launch_id": 1}, None)

    assert '"clustering_report":null' in captured["json"]


def test_get_skill_run_uses_get_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/skill/runs/42"
        return httpx.Response(200, json={"ok": True, "run_id": 42, "launch_id": 1})

    assert _client(handler).get_skill_run(42)["launch_id"] == 1


def test_get_cluster_context_passes_char_limits() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "cluster_id": "c-1"})

    response = _client(handler).get_cluster_context(
        42, "c-1", max_log_chars=111, max_message_chars=222, max_trace_chars=333
    )

    assert captured["path"] == "/api/v1/skill/runs/42/clusters/c-1/context"
    assert captured["params"] == {
        "max_log_chars": "111",
        "max_message_chars": "222",
        "max_trace_chars": "333",
    }
    assert response["cluster_id"] == "c-1"


def test_get_summary_context_posts_clusters_when_given() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    _client(handler).get_summary_context(42, {"c-1": {"analysis_text": "x"}})

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/skill/runs/42/summary-context"
    assert '"clusters"' in captured["json"]


def test_get_summary_context_omits_clusters_when_none() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    _client(handler).get_summary_context(42, None)

    assert captured["json"] == "{}"


def test_submit_skill_analysis_forwards_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "clusters_received": 1})

    response = _client(handler).submit_skill_analysis(42, {"schema_version": 1})

    assert captured["path"] == "/api/v1/skill/runs/42/analysis"
    assert '"schema_version":1' in captured["json"]
    assert response["clusters_received"] == 1


def test_generate_skill_report_posts_and_returns_html() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/skill/runs/42/report"
        return httpx.Response(
            200,
            json={"ok": True, "report_filename": "r.html", "html": "<html>"},
        )

    response = _client(handler).generate_skill_report(42)

    assert response["report_filename"] == "r.html"
    assert response["html"] == "<html>"


def test_save_skill_push_result_posts_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "run_id": 42})

    _client(handler).save_skill_push_result(42, {"comments_posted": 3})

    assert captured["path"] == "/api/v1/skill/runs/42/push-result"
    assert '"comments_posted":3' in captured["json"]


def test_connection_errors_are_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)

    with pytest.raises(AllaApiConnectionError) as err:
        client.list_kb_entries()

    assert "alla-server" in str(err.value)
