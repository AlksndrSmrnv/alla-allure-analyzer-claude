"""Parity tests for REST API and alla-skill CLI wrappers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alla.clients.alla_api_client import AllaApiClient, AllaApiConflictError
from alla.knowledge.feedback_models import FeedbackRequest, FeedbackResponse, FeedbackVote
from alla.knowledge.models import KBEntry
from alla.server import app

SKILL_SCRIPTS = Path(__file__).resolve().parent.parent / "alla-skill" / "scripts"


class _MemoryFeedbackStore:
    def __init__(self) -> None:
        self.entries: dict[int, KBEntry] = {}
        self.by_key: dict[tuple[str, int | None], int] = {}
        self.feedback: dict[tuple[int, str], int] = {}
        self.next_entry_id = 1
        self.next_feedback_id = 1

    def create_kb_entry(self, entry: KBEntry, project_id: int | None) -> int | None:
        key = (entry.id, project_id)
        if key in self.by_key:
            return None
        entry_id = self.next_entry_id
        self.next_entry_id += 1
        stored = entry.model_copy(update={"entry_id": entry_id, "project_id": project_id})
        self.entries[entry_id] = stored
        self.by_key[key] = entry_id
        return entry_id

    def find_kb_entry_by_slug(self, slug: str, project_id: int | None) -> KBEntry | None:
        entry_id = self.by_key.get((slug, project_id))
        if entry_id is None:
            return None
        return self.entries[entry_id]

    def update_kb_entry(self, entry_id: int, fields: dict[str, Any]) -> bool:
        entry = self.entries.get(entry_id)
        if entry is None:
            return False
        self.entries[entry_id] = entry.model_copy(update=fields)
        return True

    def list_kb_entries(self, project_id: int | None = None) -> list[KBEntry]:
        entries = list(self.entries.values())
        if project_id is not None:
            entries = [entry for entry in entries if entry.project_id in (None, project_id)]
        return sorted(entries, key=lambda entry: (entry.project_id is not None, entry.id))

    def record_vote(self, request: FeedbackRequest) -> FeedbackResponse:
        key = (request.kb_entry_id, request.issue_signature_hash)
        created = key not in self.feedback
        if created:
            self.feedback[key] = self.next_feedback_id
            self.next_feedback_id += 1
        return FeedbackResponse(
            kb_entry_id=request.kb_entry_id,
            audit_text_preview=request.audit_text[:80],
            vote=request.vote,
            created=created,
            feedback_id=self.feedback[key],
        )

    def resolve_votes(
        self,
        items: list[tuple[int, str, int, str]],
    ) -> dict[str, tuple[FeedbackVote, int | None]]:
        return {}

    def count_feedback_for_entry(self, entry_id: int) -> int:
        return sum(1 for feedback_entry_id, _ in self.feedback if feedback_entry_id == entry_id)

    def delete_kb_entry(self, entry_id: int) -> bool:
        if entry_id not in self.entries:
            return False
        entry = self.entries.pop(entry_id)
        self.by_key.pop((entry.id, entry.project_id), None)
        self.feedback = {
            key: value
            for key, value in self.feedback.items()
            if key[0] != entry_id
        }
        return True


@pytest.fixture
def api_client(monkeypatch) -> AllaApiClient:
    store = _MemoryFeedbackStore()
    monkeypatch.setattr("alla.server._get_feedback_store", lambda: store)
    test_client = TestClient(app)
    client = AllaApiClient("http://testserver", client=test_client)
    client._test_store = store  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        test_client.close()


@pytest.fixture
def manage_kb_module():
    sys.path.insert(0, str(SKILL_SCRIPTS))
    try:
        import manage_kb

        yield manage_kb
    finally:
        try:
            sys.path.remove(str(SKILL_SCRIPTS))
        except ValueError:
            pass


def _payload() -> dict[str, Any]:
    return {
        "title": "Connection timeout",
        "error_example": "socket.timeout: 30s",
        "step_path": "Login -> Submit",
        "project_id": 1,
    }


def _stdout_json(capsys) -> dict[str, Any]:
    captured = capsys.readouterr()
    return json.loads(captured.out)


def test_slug_parity_rest_then_cli(api_client, manage_kb_module, capsys) -> None:
    from alla.knowledge.feedback_models import CreateKBEntryRequest

    created_response, created = api_client.create_kb_entry(
        CreateKBEntryRequest.model_validate(_payload())
    )
    assert created is True

    manage_kb_module._cmd_create(api_client, _payload())
    cli_response = _stdout_json(capsys)

    assert cli_response["entry_id"] == created_response.entry_id
    assert cli_response["id"] == created_response.id
    assert cli_response["created"] is False


def test_slug_parity_cli_then_rest(api_client, manage_kb_module, capsys) -> None:
    from alla.knowledge.feedback_models import CreateKBEntryRequest

    manage_kb_module._cmd_create(api_client, _payload())
    cli_response = _stdout_json(capsys)

    rest_response, created = api_client.create_kb_entry(
        CreateKBEntryRequest.model_validate(_payload())
    )

    assert rest_response.entry_id == cli_response["entry_id"]
    assert rest_response.id == cli_response["id"]
    assert created is False


def test_cli_create_uses_server_canonicalize(api_client, manage_kb_module, capsys) -> None:
    payload = {
        "title": "Gateway timeout",
        "error_example": (
            "Order 123e4567-e89b-12d3-a456-426614174000 failed at 2026-02-10 12:00:00\n"
            "--- Лог приложения ---\n"
            "2026-02-10 12:00:00 [ERROR] from 10.20.30.40 build 123456"
        ),
        "project_id": 1,
    }

    manage_kb_module._cmd_create(api_client, payload)
    _stdout_json(capsys)
    entries = api_client.list_kb_entries(project_id=1)

    assert entries[0].error_example == (
        "Order <ID> failed at <TS>\n"
        "<TS> [ERROR] from <IP> build <NUM>"
    )


def test_cli_repeated_create_is_idempotent(api_client, manage_kb_module, capsys) -> None:
    manage_kb_module._cmd_create(api_client, _payload())
    first = _stdout_json(capsys)

    manage_kb_module._cmd_create(api_client, _payload())
    second = _stdout_json(capsys)

    assert first["entry_id"] == second["entry_id"]
    assert first["created"] is True
    assert second["created"] is False


def test_delete_force_gate_cascades_feedback(api_client, manage_kb_module, capsys) -> None:
    manage_kb_module._cmd_create(api_client, _payload())
    entry_id = _stdout_json(capsys)["entry_id"]
    api_client.submit_feedback(
        FeedbackRequest(
            kb_entry_id=entry_id,
            audit_text="[message]\nsocket.timeout: 30s",
            vote=FeedbackVote.LIKE,
            issue_signature_hash="c" * 64,
        )
    )

    with pytest.raises(SystemExit) as err:
        manage_kb_module._cmd_delete(api_client, entry_id, force=False)

    assert err.value.code == 1
    stderr = json.loads(capsys.readouterr().err)
    assert stderr["feedback_count"] == 1
    with pytest.raises(AllaApiConflictError):
        api_client.delete_kb_entry(entry_id, force=False)

    manage_kb_module._cmd_delete(api_client, entry_id, force=True)
    deleted = _stdout_json(capsys)

    assert deleted == {"ok": True, "entry_id": entry_id, "deleted": True}
    assert api_client._test_store.count_feedback_for_entry(entry_id) == 0  # type: ignore[attr-defined]
