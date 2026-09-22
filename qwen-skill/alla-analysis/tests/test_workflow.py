import asyncio
import json
from pathlib import Path

import httpx
import pytest

from alla_skill.client import Client
from alla_skill.config import Settings
from alla_skill.evidence import compact_trace, redact
from alla_skill.workflow import prepare, context, finalize


def transport(request):
    path = request.url.path
    if path.endswith("/oauth/token"):
        return httpx.Response(200, json={"access_token": "jwt-secret", "expires_in": 3600})
    if path == "/api/launch/12":
        return httpx.Response(
            200, json={"id": 12, "name": "Regression", "projectId": 7, "closed": False}
        )
    if path == "/api/testresult":
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "id": 1,
                        "name": "test_payment",
                        "fullName": "tests.test_payment",
                        "status": "failed",
                        "statusDetails": {
                            "message": "Expected 200 but was: <500>",
                            "trace": "Caused by: payment error",
                        },
                    },
                    {"id": 2, "status": "passed"},
                    {"id": 3, "status": "failed", "hidden": True},
                    {"id": 4, "status": "broken", "muted": True},
                ],
                "totalElements": 4,
                "totalPages": 1,
                "last": True,
            },
        )
    if path.endswith("/execution"):
        return httpx.Response(503)
    if path == "/api/testresult/attachment":
        page = int(request.url.params.get("page", 0))
        return httpx.Response(
            200,
            json={
                "content": [{"id": 20 + page, "name": "app.log", "contentType": "text/plain"}],
                "totalPages": 2,
                "totalElements": 2,
                "last": page == 1,
            },
        )
    if path.endswith("/content"):
        return httpx.Response(
            200,
            text="2026-09-22 [ERROR] HTTP/1.1 500 Authorization: Bearer private-value\n"
            + "x" * 3000,
        )
    return httpx.Response(404)


def test_prepare_context_and_resume_validation(tmp_path):
    async def run():
        async with Client(
            Settings(
                endpoint="https://testops.test", token="secret", max_attachment_bytes=128, retries=0
            ),
            transport=httpx.MockTransport(transport),
        ) as client:
            return await prepare(12, tmp_path, client)

    run_dir = asyncio.run(run())
    data = json.loads((run_dir / "run.json").read_text())
    assert data["counters"]["active_failures"] == 1
    assert data["counters"]["muted_failures"] == 1
    assert data["counters"]["hidden_results"] == 1
    assert data["launch"]["closed"] is False
    assert len(data["clusters"]) == 1
    cid = data["clusters"][0]["cluster_id"]
    ctx = context(run_dir, cid)
    assert ctx["examples"][0]["test_result_id"] == 1
    assert "500" in ctx["examples"][0]["status_message"]
    sources = data["sources"]
    assert sources["attachment:20"]["state"] == "truncated"
    assert sources["attachment:21"]["state"] == "truncated"
    assert sources["execution:1"]["state"] == "unavailable"
    assert "private-value" not in "".join(p.read_text() for p in run_dir.rglob("*") if p.is_file())
    with pytest.raises(ValueError, match="анализ"):
        finalize(run_dir)
    assert not (run_dir / "report.md").exists()
    assert context(run_dir, cid) == ctx


def test_trace_keeps_causes_and_project_frames():
    trace = "\n".join(
        ["RuntimeException"]
        + ["at java.base.Framework.run(F.java:1)"] * 150
        + ["Caused by: DatabaseException", "at tests.Payment.check(Payment.java:55)"]
    )
    compact = compact_trace(trace, ["Payment"], max_lines=15)
    assert "Caused by: DatabaseException" in compact
    assert "Payment.java:55" in compact
    assert "пропущены строки" in compact


def test_redaction_preserves_diagnostics():
    result = redact(
        '2026-09-22 expected=200 actual=500 correlationId=abcd password="secret" Authorization: Bearer abcd-secret'
    )
    assert "secret" not in result
    assert "correlationId=abcd" in result
    assert "2026-09-22" in result
    assert "actual=500" in result


def prepared(tmp_path, handler=transport):
    async def run():
        async with Client(
            Settings(endpoint="https://testops.test", token="api-secret", retries=0),
            transport=httpx.MockTransport(handler),
        ) as client:
            return await prepare(12, tmp_path, client)

    return asyncio.run(run())


def finish_payloads(run_dir):
    data = json.loads((run_dir / "run.json").read_text())
    for cluster in data["clusters"]:
        cid = cluster["cluster_id"]
        payload = {
            "run_id": data["run_id"],
            "cluster_id": cid,
            "symptom": "Тест получил HTTP 500 вместо 200.",
            "cause": "Причина не установлена.",
            "category": "неизвестно",
            "confidence": "низкая",
            "evidence": [
                {"source": "test:1:status_message", "quote": "Expected 200 but was: <500>"}
            ],
            "limitations": ["Execution недоступен."],
            "contradictions": [],
            "next_action": "Сопоставить запрос с логом сервиса.",
            "code_alignment": "неизвестно",
            "code_alignment_reason": "В прогоне не указана ревизия.",
        }
        (run_dir / "analyses" / f"{cid}.json").write_text(json.dumps(payload))
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": data["run_id"],
                "summary_lines": [
                    "Обнаружено одно активное падение.",
                    "Первопричина пока не установлена.",
                ],
                "priority_actions": ["Проверить серверный ответ."],
                "findings": [],
            }
        )
    )
    return data


def test_finalize_and_pending_are_consistent(tmp_path):
    directory = prepared(tmp_path)
    data = finish_payloads(directory)
    state = context(directory)
    assert state["pending"] == []
    assert state["completed"] == [data["clusters"][0]["cluster_id"]]
    result = finalize(directory)
    text = Path(result["report"]).read_text()
    assert result["summary"] in text
    assert "Прогон открыт" in text
    assert "execution:1: unavailable" in text
    assert result["analyzed_clusters"] == 1
    assert directory != prepared(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_run",
        "wrong_cluster",
        "made_up_quote",
        "extra_file",
        "missing_analysis",
        "wrong_count",
        "duplicate_member",
        "wrong_summary",
    ],
)
def test_finalize_rejects_invalid_or_incomplete_analysis(tmp_path, mutation):
    directory = prepared(tmp_path)
    data = finish_payloads(directory)
    path = next((directory / "analyses").glob("*.json"))
    analysis = json.loads(path.read_text())
    if mutation == "wrong_run":
        analysis["run_id"] = "another-run"
    elif mutation == "wrong_cluster":
        analysis["cluster_id"] = "another-cluster"
    elif mutation == "made_up_quote":
        analysis["evidence"][0]["quote"] = "Database crashed"
    elif mutation == "extra_file":
        (directory / "analyses" / "duplicate.json").write_text(path.read_text())
    elif mutation == "missing_analysis":
        path.unlink()
    elif mutation == "wrong_count":
        data["counters"]["active_failures"] = 2
    elif mutation == "duplicate_member":
        data["clusters"][0]["member_test_ids"].append(1)
    elif mutation == "wrong_summary":
        summary = json.loads((directory / "summary.json").read_text())
        summary["run_id"] = "another-run"
        (directory / "summary.json").write_text(json.dumps(summary))
    if mutation != "missing_analysis":
        path.write_text(json.dumps(analysis))
    (directory / "run.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        finalize(directory)
    assert not (directory / "report.md").exists()


@pytest.mark.parametrize(
    "results,muted",
    [
        ([], 0),
        ([{"id": 1, "status": "passed"}], 0),
        ([{"id": 1, "status": "broken", "muted": True}], 1),
    ],
)
def test_no_active_failures_still_produces_report(tmp_path, results, muted):
    def handler(request):
        if request.url.path == "/api/testresult":
            return httpx.Response(
                200, json={"content": results, "totalElements": len(results), "last": True}
            )
        return transport(request)

    directory = prepared(tmp_path, handler)
    data = finish_payloads(directory)
    assert data["clusters"] == []
    assert data["counters"]["muted_failures"] == muted
    assert finalize(directory)["analyzed_clusters"] == 0


def test_sources_cannot_escape_snapshot(tmp_path):
    directory = prepared(tmp_path)
    with pytest.raises(ValueError):
        context(directory, source="code:../outside.py:1")
    with pytest.raises(ValueError):
        context(directory, source="attachment:9999")
    with pytest.raises(ValueError):
        context(directory, source="test:99:status_trace")
    part = context(directory, source="attachment:20", offset=0, limit=10)
    assert len(part["text"]) == 10
    assert part["next_offset"] == 10


def test_environment_takes_priority_without_exposing_token(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "ALLURE_ENDPOINT=https://testops.test\nALLURE_TOKEN=file-token\n"
    )
    monkeypatch.setenv("ALLURE_TOKEN", "env-token")
    monkeypatch.delenv("ALLURE_ENDPOINT", raising=False)
    settings = Settings.load(tmp_path, env_file=tmp_path / ".env")
    assert settings.token == "env-token"
    assert "env-token" not in repr(settings)


def test_detail_fallback_fills_missing_trace_without_overwriting_message(tmp_path):
    def handler(request):
        if request.url.path == "/api/testresult":
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "id": 1,
                            "status": "failed",
                            "statusDetails": {"message": "Original assertion"},
                        }
                    ],
                    "last": True,
                    "totalElements": 1,
                },
            )
        if request.url.path == "/api/testresult/1":
            return httpx.Response(200, json={"id": 1, "trace": "Caused by: useful root error"})
        return transport(request)

    directory = prepared(tmp_path, handler)
    test = json.loads((directory / "tests/1.json").read_text())
    assert test["status_message"] == "Original assertion"
    assert test["status_trace"] == "Caused by: useful root error"


def test_redaction_masks_secret_headers_and_truncated_quoted_values():
    assert redact({"X-API-Key": "private-value", "Set-Cookie": "session=private-value"}) == {
        "X-API-Key": "[REDACTED]",
        "Set-Cookie": "[REDACTED]",
    }
    assert "secondword" not in redact('password="firstword secondword')
    assert "private-value" not in redact("X-API-Key: private-value")


def test_finalization_rejects_cross_cluster_evidence(tmp_path):
    directory = prepared(tmp_path)
    data = finish_payloads(directory)
    other = dict(data["clusters"][0])
    other.update(
        cluster_id="second",
        member_test_ids=[9],
        member_count=1,
        representative_test_id=9,
        example_test_ids=[9],
    )
    data["clusters"].append(other)
    data["active_test_ids"].append(9)
    data["counters"]["active_failures"] += 1
    (directory / "run.json").write_text(json.dumps(data))
    (directory / "tests/9.json").write_text(
        json.dumps({"test_result_id": 9, "status_message": "Unrelated error"})
    )
    path = directory / "analyses" / (data["clusters"][0]["cluster_id"] + ".json")
    item = json.loads(path.read_text())
    item["evidence"] = [{"source": "test:9:status_message", "quote": "Unrelated error"}]
    path.write_text(json.dumps(item))
    with pytest.raises(ValueError, match="другому кластеру"):
        finalize(directory)


def test_redaction_keeps_json_attachment_parseable():
    payload = '{"password":"top secret", "status":500, "correlationId":"req-123"}'
    cleaned = json.loads(redact(payload))
    assert cleaned == {"password": "[REDACTED]", "status": 500, "correlationId": "req-123"}
