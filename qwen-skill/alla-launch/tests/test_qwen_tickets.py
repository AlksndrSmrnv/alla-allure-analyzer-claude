"""Объективные проверки стенда: одобрение по репликам, полный просмотр и demo link."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import skill_fixtures  # noqa: F401 — scripts/ в sys.path
from qwen_stand import (
    LONG_TICKET_QUESTION,
    SCENARIOS,
    STAND_TICKETS_DB,
    Context,
    Trace,
    build_project,
    check_tickets_created,
    check_tickets_linked,
    check_tickets_long_preview,
    check_tickets_not_sent,
    check_tickets_prepared,
    check_tickets_waited,
    parse_traces,
)

from alla_skill_lib.session_rules import ToolCall


def shell(action: str, *, subagent: bool = False, index: int = 0) -> ToolCall:
    return ToolCall(index, "run_shell_command", {
        "command": f"python3 .qwen/skills/alla-launch/scripts/alla_skill.py "
                   f"extensions action tickets {action} --context /context.json",
    }, subagent)


def test_trace_preserves_human_turn_boundaries(tmp_path: Path) -> None:
    paths = []
    for turn, action in enumerate(("show", "submit"), start=1):
        path = tmp_path / f"trace-{turn}.jsonl"
        path.write_text("\n".join(json.dumps(event) for event in (
            {"type": "assistant", "message": {"content": [{"type": "tool_use",
             "id": f"tool-{turn}", "name": "run_shell_command",
             "input": shell(action).input}]}},
            {"type": "result", "result": f"ответ {turn}"},
        )), encoding="utf-8")
        paths.append(path)
    trace = parse_traces(paths)
    assert [len(calls) for calls in trace.turn_calls] == [1, 1]
    assert trace.turn_finals == ["ответ 1", "ответ 2"]
    assert [call.index for calls in trace.turn_calls for call in calls] == [0, 1]


def test_approval_check_rejects_early_and_subagent_submits(tmp_path: Path) -> None:
    scenario = SCENARIOS["T01"]
    early = Context(scenario, Trace(turn_calls=[[], [shell("submit")], []]), tmp_path, [])
    assert check_tickets_waited(early)["status"] == "fail"
    child = Context(scenario, Trace(turn_calls=[[], [], [shell("submit", subagent=True)]]),
                    tmp_path, [])
    assert check_tickets_waited(child)["status"] == "fail"
    parent = Context(scenario, Trace(turn_calls=[[], [], [shell("submit")]]), tmp_path, [])
    assert check_tickets_waited(parent)["status"] == "pass"


def preview_context(tmp_path: Path) -> tuple[Context, Path]:
    state = tmp_path / "alla-reports" / ".extensions" / "tickets" / "project-id"
    records = state / "requests"
    records.mkdir(parents=True)
    request_id = "9647bbdb-cda4-496d-8675-0e006690df59"
    payload = '{"ticket":"full approved content"}'
    (records / f"{request_id}.json").write_text(json.dumps({
        "extension": "tickets", "state": "ready", "request_id": request_id, "numbers": [1],
        "version": {"prepared": {"payload": base64.b64encode(payload.encode()).decode(),
                                 "scope": {"site": "demo://local/test", "project_or_queue": "DEMO/backend"}},
                    "search": {"matches": []}},
    }), encoding="utf-8")
    preview = state / f"{request_id}.review.md"
    preview.write_text("# Исследование\n\n" + LONG_TICKET_QUESTION, encoding="utf-8")
    show = shell("show", subagent=True)
    show.input["command"] += f" -- --request {request_id}"
    read = ToolCall(1, "read_file", {"absolute_path": str(preview)}, False,
                    result=preview.read_text(encoding="utf-8"))
    trace = Trace(calls=[show, read], turn_calls=[[], [show, read]],
                  turn_finals=["отчёт", preview.read_text() + f"\ndemo://local/test DEMO/backend\n{payload}"])
    return Context(SCENARIOS["T05"], trace, tmp_path, []), preview


def test_long_preview_requires_parent_to_read_the_end(tmp_path: Path) -> None:
    ctx, _ = preview_context(tmp_path)
    assert check_tickets_prepared(ctx)["status"] == "pass"
    assert check_tickets_long_preview(ctx)["status"] == "pass"
    ctx.trace.calls[1].result = ctx.trace.calls[1].result[:4000]
    assert check_tickets_long_preview(ctx)["status"] == "fail"


def test_preview_read_by_child_is_not_parent_review(tmp_path: Path) -> None:
    ctx, _ = preview_context(tmp_path)
    ctx.trace.calls[1].subagent = True
    assert check_tickets_prepared(ctx)["status"] == "fail"


def test_parent_must_read_after_latest_show(tmp_path: Path) -> None:
    ctx, _ = preview_context(tmp_path)
    latest = shell("show", index=2)
    latest.input["command"] = ctx.trace.calls[0].input["command"]
    ctx.trace.calls.append(latest)
    assert check_tickets_prepared(ctx)["status"] == "fail"


def test_reading_question_file_does_not_prove_full_preview_read(tmp_path: Path) -> None:
    ctx, _ = preview_context(tmp_path)
    ctx.trace.calls[1].result = ctx.trace.calls[1].result[:4000]
    ctx.trace.calls.append(ToolCall(2, "read_file", {
        "absolute_path": str(tmp_path / "question.txt"),
    }, False, result=LONG_TICKET_QUESTION))
    assert check_tickets_long_preview(ctx)["status"] == "fail"


def test_filename_only_final_does_not_count_as_full_transmission(tmp_path: Path) -> None:
    ctx, preview = preview_context(tmp_path)
    ctx.trace.turn_finals[1] = f"Полный просмотр: {preview}. Создавать?"
    assert check_tickets_prepared(ctx)["status"] == "fail"
    assert check_tickets_long_preview(ctx)["status"] == "fail"


def test_unjournaled_external_mutation_is_not_ignored(tmp_path: Path) -> None:
    ctx, _ = preview_context(tmp_path)
    with sqlite3.connect(tmp_path / STAND_TICKETS_DB) as connection:
        connection.execute("CREATE TABLE requests(request_id TEXT, operation TEXT, "
                           "payload TEXT, ticket_number INTEGER)")
        connection.execute("INSERT INTO requests VALUES ('rogue-request', 'create', 'x', 2)")
    assert check_tickets_not_sent(ctx)["status"] == "fail"


def test_demo_seed_uses_external_protocol_and_stays_out_of_git(tmp_path: Path) -> None:
    project = build_project(tmp_path, "http://127.0.0.1:1", None,
                            extensions=("tickets",), ticket_seed=True)
    config = (project / "alla-extensions" / "config.toml").read_text(encoding="utf-8")
    assert 'team_application = "backend"' in config
    assert STAND_TICKETS_DB in config
    with sqlite3.connect(project / STAND_TICKETS_DB) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
        assert connection.execute("SELECT operation FROM requests").fetchall() == [("create",)]


def test_local_receipt_does_not_prove_external_create_or_link(tmp_path: Path) -> None:
    ctx, preview = preview_context(tmp_path)
    path = preview.parent / "requests" / (preview.stem.removesuffix(".review") + ".json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["state"] = "created"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert check_tickets_created(ctx)["status"] == "fail"
    record["state"] = "linked"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert check_tickets_linked(ctx)["status"] == "fail"
    assert check_tickets_not_sent(ctx)["status"] == "fail"
