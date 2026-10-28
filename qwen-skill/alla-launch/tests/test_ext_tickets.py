"""Ticket state and adapter protocol exercised with actual child processes."""

from __future__ import annotations

import base64
import importlib.util
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "extensions" / "tickets" / "scripts"
DEMO = SCRIPTS.parent / "adapters" / "demo.py"
spec = importlib.util.spec_from_file_location("ext_ticketlib", SCRIPTS / "ticketlib.py")
assert spec is not None and spec.loader is not None
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


def problem(number: int, **changes: Any) -> dict[str, Any]:
    return {"number": number, "label": f"Ошибка операции {number}", "category": "приложение",
            "cause": f"не обработано значение {number}", "what": f"Операция {number} падает",
            "fix": f"Вернуть обработку значения {number}", "missing": "нет", "valid": True,
            "confirmed": False, "has_fix": False, "rerun": None,
            "tests": [{"name": f"test{number}", "full_name": f"Tests.test{number}",
                       "link": f"https://testops.example/result/{number}"}],
            "sources": [{"id": "S1", "kind": "log", "title": f"app.log {number}",
                         "test_name": f"test{number}", "text": f"ERROR invalid value {number}"}],
            "observations": [{"source_id": "S1", "quote": f"invalid value {number}"}],
            **changes}


@pytest.fixture
def case(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], Any]:
    project = tmp_path / "project"
    project.mkdir()
    python = project / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    run_dir = project / "reports" / "777-run"
    out = run_dir / "extensions" / "tickets"
    out.mkdir(parents=True)
    settings = {"adapter_command": [".venv/bin/python", str(DEMO), "--db", str(project / "demo.db")],
                "adapter_env": [], "adapter_timeout": 5, "hint": True,
                "team_application": "Product", "team_test": "QA", "team_environment": "SRE",
                "team_data": "Data", "team_unknown": "QA"}
    snap = {"schema": 1, "run_uid": str(uuid.uuid4()), "launch_id": 777,
            "project_root": str(project), "settings": settings,
            "problems": [problem(2), problem(5), problem(7)], "groups": []}
    snapshot_file = project / "snapshot.json"
    snapshot_file.write_bytes(t.canonical(snap))
    exporter = project / "export_snapshot.py"
    exporter.write_text(
        "import json, sys\nprint('STATUS: ok')\n"
        "print(json.dumps({'path': sys.argv[1], 'sha256':'not-used'}))\n", encoding="utf-8")
    ctx = {"api": 1, "extension": "tickets", "event": "on_request_many",
           "run_uid": snap["run_uid"], "run_dir": str(run_dir), "project_root": str(project),
           "out_dir": str(out), "state_dir": str(project / "reports" / ".extensions" / "tickets"),
           "settings": settings, "selected_numbers": [2], "params": {}, "text_params": {},
           "stem": "on_request_many-test", "snapshot_command": [sys.executable, str(exporter),
                                                                     str(snapshot_file)]}
    return ctx, snap, t.Store(ctx)


def update_snapshot(ctx: dict[str, Any], snap: dict[str, Any]) -> None:
    Path(ctx["snapshot_command"][-1]).write_bytes(t.canonical(snap))


def wrapped_adapter(ctx: dict[str, Any], snap: dict[str, Any], **controls: Any) -> Path:
    project = Path(ctx["project_root"])
    control = project / "adapter-control.json"
    control.write_bytes(t.canonical(controls))
    wrapper = project / "adapter-wrapper.py"
    wrapper.write_text(
        "import json,os,subprocess,sys,time\n"
        "from pathlib import Path\n"
        f"control=json.loads(Path({str(control)!r}).read_text())\n"
        "raw=sys.stdin.buffer.read()\nrequest=json.loads(raw)\n"
        "if control.get('check_env'):\n"
        "    assert os.environ.get('TRACKER_TEST_TOKEN') == 'private-token-value'\n"
        "    assert not any(k in os.environ for k in "
        "['ALLURE_TOKEN','OPENROUTER_API_KEY','HOME','PATH'])\n"
        "if request['operation']=='search':\n"
        "    if control.get('search_error'): print('not-json'); sys.exit()\n"
        "    if 'matches' in control: print(json.dumps({'api':1,'matches':control['matches']})); sys.exit()\n"
        "if request['operation'] in ['create','link'] and control.get('gate'):\n"
        "    gate=Path(control['gate']);gate.with_suffix('.started').write_text(str(os.getpid()))\n"
        "    while not gate.exists(): time.sleep(0.01)\n"
        f"result=subprocess.run([sys.executable,{str(DEMO)!r},'--db',{str(project / 'demo.db')!r}],"
        "input=raw,capture_output=True)\n"
        "if request['operation']=='prepare' and control.get('no_search'):\n"
        "    response=json.loads(result.stdout);response['capabilities']['search']=False\n"
        "    print(json.dumps(response));sys.exit()\n"
        "if request['operation'] in ['create','link','lookup'] and control.get('blank_receipt'):\n"
        "    response=json.loads(result.stdout)\n"
        "    if response.get('outcome')=='found': response['ticket'][control['blank_receipt']]=' '\n"
        "    print(json.dumps(response));sys.exit()\n"
        "sys.stdout.buffer.write(result.stdout)\nsys.exit(result.returncode)\n", encoding="utf-8")
    ctx["settings"]["adapter_command"] = [".venv/bin/python", str(wrapper)]
    update_snapshot(ctx, snap)
    return control


def make_request(ctx: dict[str, Any], store: Any, anchor: int = 2) -> dict[str, Any]:
    with store.lock():
        t.draft(ctx, anchor, store)
    records = [record for record in store.records() if record["state"] == "ready"]
    assert len(records) == 1
    record = records[0]
    t.show(record, store)
    return store.load(record["request_id"])


def invoke(ctx: dict[str, Any], action: str, *args: str) -> subprocess.CompletedProcess[str]:
    context_file = Path(ctx["out_dir"]) / "test-context.json"
    context_file.write_bytes(t.canonical(ctx))
    return subprocess.run([sys.executable, str(SCRIPTS / f"{action}.py"), *args],
                          env={**os.environ, "ALLA_CONTEXT": str(context_file)},
                          capture_output=True, text=True, timeout=30, check=False)


def test_partial_automatic_group_keeps_valid_singleton(case: Any) -> None:
    ctx, snap, store = case
    snap["groups"] = [{"numbers": [2, 5], "cause": "одна причина"}]
    snap["problems"][1]["has_fix"] = True
    result = t.assess(ctx, snap, store, automatic=True)
    assert [row["numbers"] for row in result["units"]] == [[2], [7]]
    ctx["selected_numbers"] = [2, 5]
    assert t.assess(ctx, snap, store)["units"][0]["numbers"] == [2, 5]


@pytest.mark.parametrize("saved,explicit", [([2, 5, 7], [2, 5]), ([2, 5], [5, 7])])
def test_overlapping_full_bases_refuse_without_records(case: Any, saved: list[int],
                                                       explicit: list[int]) -> None:
    ctx, snap, store = case
    snap["groups"] = [{"numbers": saved, "cause": "причина"}]
    ctx["selected_numbers"] = [2, 5, 7]
    ctx["params"] = {"group": [",".join(map(str, explicit))]}
    update_snapshot(ctx, snap)
    with pytest.raises(t.TicketError, match="Конфликт оснований"):
        t.draft(ctx, 2, store)
    assert not store.records() and not store.groups()


@pytest.mark.parametrize("selected,invalid", [([2], False), ([2, 5], True)])
def test_explicit_group_never_clips_unknown_or_rejected(case: Any, selected: list[int],
                                                      invalid: bool) -> None:
    ctx, snap, store = case
    ctx["selected_numbers"] = selected
    ctx["params"] = {"group": ["2,5"]}
    if invalid:
        snap["problems"][1]["valid"] = False
    with pytest.raises(t.TicketError, match="не обрезается"):
        t.assess(ctx, snap, store)


def test_selection_projects_report_group_without_unrequested_members(case: Any) -> None:
    ctx, snap, store = case
    snap["groups"] = [{"numbers": [2, 5, 7], "cause": "причина"}]
    ctx["selected_numbers"] = [2, 5, 9]
    result = t.assess(ctx, snap, store)
    assert [row["numbers"] for row in result["units"]] == [[2, 5]]
    assert 9 in result["rejected"]


def test_engineer_group_survives_report_removal_and_expires_on_diagnosis(case: Any) -> None:
    ctx, snap, store = case
    ctx["selected_numbers"] = [2, 5]
    ctx["params"] = {"group": ["2,5"]}
    original = make_request(ctx, store)
    old_basis = original["version"]["projection"]["basis"]
    snap["groups"] = []
    ctx["params"] = {}
    for item in snap["problems"][:2]:
        item["confirmed"] = True
    assert t.assess(ctx, snap, store)["units"][0]["numbers"] == [2, 5]
    assert len(t.assess(ctx, snap, store, automatic=True)["units"]) == 2
    snap["problems"][0]["cause"] = "новая причина"
    assert not t.active_groups(store, snap)
    ctx["params"] = {"group": ["2,5"]}
    update_snapshot(ctx, snap)
    t.draft(ctx, 2, store)
    new_record = next(row for row in store.records() if row["state"] == "ready")
    assert new_record["version"]["projection"]["basis"]["decision_id"] != old_basis["decision_id"]


def test_revoke_group_allowed_during_conflict_without_creating_ticket(case: Any) -> None:
    ctx, snap, store = case
    ctx["selected_numbers"] = [2, 5]
    ctx["params"] = {"group": ["2,5"]}
    record = make_request(ctx, store)
    decision = record["version"]["projection"]["basis"]["decision_id"]
    assert decision in (store.root / f"{record['request_id']}.review.md").read_text()
    snap["groups"] = [{"numbers": [2, 5, 7], "cause": "новая связь"}]
    ctx["params"] = {}
    with pytest.raises(t.TicketError, match=decision):
        t.assess(ctx, snap, store)
    ctx.update(selected_numbers=[], params={"revoke_group": [decision]})
    assert "отозвано" in t.revoke(ctx, store)
    assert len(store.records()) == 1
    assert store.groups()[0]["revoked"] is True


def test_new_run_does_not_inherit_group_or_request(case: Any) -> None:
    ctx, snap, store = case
    ctx["selected_numbers"] = [2, 5]
    ctx["params"] = {"group": ["2,5"]}
    original = make_request(ctx, store)
    ctx["run_uid"] = snap["run_uid"] = str(uuid.uuid4())
    ctx["params"] = {}
    update_snapshot(ctx, snap)
    assert not t.active_groups(store, snap)
    t.draft(ctx, 2, store)
    assert len(store.records()) == 2
    assert {row["run_uid"] for row in store.records()} == {original["run_uid"], snap["run_uid"]}


def test_actual_payload_matches_immutable_version_and_repeat_returns_receipt(case: Any) -> None:
    ctx, _, store = case
    record = make_request(ctx, store)
    result = t.submit(ctx, record, record["shown_hash"], store)
    assert "Создан тикет" in result
    saved = store.load(record["request_id"])
    assert saved["state"] == "created"
    connection = sqlite3.connect(Path(ctx["project_root"]) / "demo.db")
    stored = connection.execute("SELECT payload FROM requests").fetchone()[0]
    assert base64.b64decode(stored) == base64.b64decode(saved["version"]["prepared"]["payload"])
    assert "Создан тикет" in t.submit(ctx, saved, record["shown_hash"], store)
    assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
    connection.close()


def test_unrelated_problem_change_does_not_stale_version(case: Any) -> None:
    ctx, snap, store = case
    record = make_request(ctx, store)
    snap["problems"][1]["cause"] = "совсем другая причина"
    update_snapshot(ctx, snap)
    assert "Создан тикет" in t.submit(ctx, record, record["shown_hash"], store)


def test_unrelated_change_does_not_stale_grouped_ticket(case: Any) -> None:
    ctx, snap, store = case
    ctx["selected_numbers"] = [2, 5]
    snap["groups"] = [{"numbers": [2, 5], "cause": "Общий дефект валидации"}]
    update_snapshot(ctx, snap)
    record = make_request(ctx, store)
    snap["problems"][2]["cause"] = "Другая причина посторонней проблемы"
    update_snapshot(ctx, snap)
    assert "Создан тикет" in t.submit(ctx, record, record["shown_hash"], store)


@pytest.mark.parametrize("field,value", [("cause", "другая причина"), ("has_fix", True),
                                         ("rerun", {"passed": True})])
def test_related_problem_projection_change_requires_new_show(case: Any, field: str,
                                                            value: Any) -> None:
    ctx, snap, store = case
    record = make_request(ctx, store)
    snap["problems"][0][field] = value
    update_snapshot(ctx, snap)
    with pytest.raises(t.TicketError, match="Проекция"):
        t.submit(ctx, record, record["shown_hash"], store)
    assert store.load(record["request_id"])["state"] == "ready"
    assert not (Path(ctx["project_root"]) / "demo.db").exists()


def test_fabricated_source_quote_cannot_reach_adapter(case: Any) -> None:
    ctx, snap, store = case
    snap["problems"][0]["observations"][0]["quote"] = "invalid value 5"
    update_snapshot(ctx, snap)
    with pytest.raises(t.TicketError, match="Такого тикета"):
        t.draft(ctx, 2, store)
    assert not store.records()
    assert not (Path(ctx["project_root"]) / "demo.db").exists()


def test_lost_response_then_lookup_found_does_not_create_twice(case: Any) -> None:
    ctx, snap, store = case
    ctx["settings"]["adapter_command"].append("--lose-response-once")
    update_snapshot(ctx, snap)
    record = make_request(ctx, store)
    assert "итог неизвестен" in t.submit(ctx, record, record["shown_hash"], store)
    record = store.load(record["request_id"])
    assert record["state"] == "unknown"
    assert "Создан тикет" in t.reconcile(ctx, record, store)
    connection = sqlite3.connect(Path(ctx["project_root"]) / "demo.db")
    assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
    connection.close()


def test_absent_reopens_same_id_without_automatic_mutation(case: Any) -> None:
    ctx, _, store = case
    record = make_request(ctx, store)
    record["state"] = "sending"  # crash after durable mark, before adapter launch
    store.save(record)
    assert "точно не было" in t.reconcile(ctx, record, store)
    assert not (Path(ctx["project_root"]) / "demo.db").exists()
    reopened = store.load(record["request_id"])
    assert reopened["state"] == "ready"
    assert reopened["request_id"] == record["request_id"]
    assert "Создан тикет" in t.submit(ctx, reopened, reopened["shown_hash"], store)


def test_absent_without_declared_capability_keeps_lock(case: Any) -> None:
    ctx, _, store = case
    record = make_request(ctx, store)
    record["state"] = "sending"
    record["version"]["prepared"]["capabilities"]["authoritative_absence"] = False
    record["version_hash"] = t.digest(record["version"])
    store.save(record)
    assert "итог неизвестен" in t.reconcile(ctx, record, store)
    assert store.load(record["request_id"])["state"] == "unknown"
    assert len(store.records()) == 1


def test_unknown_intersection_cannot_bypass_with_other_group(case: Any) -> None:
    ctx, snap, store = case
    record = make_request(ctx, store)
    record["state"] = "unknown"
    store.save(record)
    ctx["selected_numbers"] = [2, 5]
    ctx["params"] = {"group": ["2,5"]}
    update_snapshot(ctx, snap)
    assert "новая отправка запрещена" in t.draft(ctx, 2, store)
    assert len(store.records()) == 1


def test_link_requires_search_target_and_performs_real_update(case: Any) -> None:
    ctx, snap, store = case
    record = make_request(ctx, store)
    t.submit(ctx, record, record["shown_hash"], store)
    ctx["selected_numbers"] = [5]
    ctx["params"] = {"operation_5": ["link"], "target_5": ["DEMO-999"]}
    with pytest.raises(t.TicketError, match="отсутствует"):
        t.draft(ctx, 5, store)
    assert len(store.records()) == 1
    ctx["params"]["target_5"] = ["DEMO-1"]
    update_snapshot(ctx, snap)
    t.draft(ctx, 5, store)
    linked = next(row for row in store.records() if row["numbers"] == [5])
    t.show(linked, store)
    linked = store.load(linked["request_id"])
    assert "Связь добавлена" in t.submit(ctx, linked, linked["shown_hash"], store)
    connection = sqlite3.connect(Path(ctx["project_root"]) / "demo.db")
    assert connection.execute("SELECT count(*) FROM ticket_updates").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
    connection.close()


def test_search_new_match_stales_review_before_mutation(case: Any) -> None:
    ctx, snap, store = case
    record = make_request(ctx, store)
    other_ctx = {**ctx, "selected_numbers": [5]}
    t.draft(other_ctx, 5, store)
    other = next(row for row in store.records() if row["numbers"] == [5])
    t.show(other, store)
    other = store.load(other["request_id"])
    t.submit(other_ctx, other, other["shown_hash"], store)
    update_snapshot(ctx, snap)
    with pytest.raises(t.TicketError, match="поиск изменились"):
        t.submit(ctx, record, record["shown_hash"], store)
    assert store.load(record["request_id"])["state"] == "ready"


def test_long_investigation_text_and_complete_review_preserve_bytes(case: Any) -> None:
    ctx, snap, store = case
    ctx["params"] = {"kind_2": ["investigation"]}
    question = "  Вопрос инженера\r\n" + "Строка вопроса\n" * 400 + "  "
    check = "\tПроверка\n" + "Шаг\n" * 200
    ctx["text_params"] = {"question_2": question, "check_2": check}
    update_snapshot(ctx, snap)
    record = make_request(ctx, store)
    assert record["version"]["ticket"]["question"] == question
    assert record["version"]["ticket"]["check"] == check
    review = (store.root / f"{record['request_id']}.review.md").read_bytes()
    assert len(review) > 4000
    assert question.encode() in review
    assert "Создан тикет" in t.submit(ctx, record, record["shown_hash"], store)


def test_unconfigured_adapter_allows_review_but_never_send(case: Any) -> None:
    ctx, snap, store = case
    ctx["settings"]["adapter_command"] = []
    update_snapshot(ctx, snap)
    record = make_request(ctx, store)
    assert record["version"]["search"]["status"] == "unsupported"
    with pytest.raises(t.TicketError, match="не подключён"):
        t.submit(ctx, record, record["shown_hash"], store)
    assert store.load(record["request_id"])["state"] == "ready"


def test_show_script_does_not_submit_or_claim_engineer_approval(case: Any) -> None:
    ctx, _, store = case
    record = make_request(ctx, store)
    result = invoke(ctx, "show", "--request", record["request_id"])
    assert result.returncode == 0
    assert "дождись его ответа" in result.stdout
    assert "submit не вызывай" in result.stdout
    assert store.load(record["request_id"])["state"] == "ready"


def test_multiple_processes_reuse_one_request(case: Any) -> None:
    ctx, _, store = case
    context_file = Path(ctx["out_dir"]) / "parallel-context.json"
    context_file.write_bytes(t.canonical(ctx))
    env = {**os.environ, "ALLA_CONTEXT": str(context_file)}
    children = [subprocess.Popen([sys.executable, str(SCRIPTS / "draft.py"), "--unit", "2"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(2)]
    outputs = [child.communicate(timeout=30) for child in children]
    assert all(child.returncode == 0 for child in children), outputs
    assert len(store.records()) == 1


@pytest.mark.parametrize("replacement", ["payload", "numbers", "operation"])
def test_saved_version_tampering_is_refused(case: Any, replacement: str) -> None:
    ctx, _, store = case
    record = make_request(ctx, store)
    if replacement == "payload":
        record["version"]["prepared"]["payload"] = base64.b64encode(b"other").decode()
    elif replacement == "numbers":
        record["numbers"] = [5]
    else:
        record["operation"] = "link"
    with pytest.raises(t.TicketError, match="изменил"):
        t.submit(ctx, record, record["shown_hash"], store)


def test_wrong_scope_on_reconcile_retains_unknown(case: Any) -> None:
    ctx, _, store = case
    record = make_request(ctx, store)
    record["state"] = "sending"
    store.save(record)
    ctx["settings"]["adapter_command"] += ["--project", "OTHER"]
    assert "итог неизвестен" in t.reconcile(ctx, record, store)
    assert store.load(record["request_id"])["state"] == "unknown"


def test_whitespace_and_ellipsis_quote_exports_only_actual_span(case: Any) -> None:
    ctx, snap, store = case
    source = "UNRELATED PREFIX\nERROR   Invalid  value\n detail = 'bad'\nUNRELATED SUFFIX"
    snap["problems"][0]["sources"][0]["text"] = source
    snap["problems"][0]["observations"][0]["quote"] = 'error invalid value … detail = "bad"'
    update_snapshot(ctx, snap)
    record = make_request(ctx, store)
    exported = record["version"]["ticket"]["evidence"][0]["quote"]
    assert exported == "ERROR   Invalid  value\n detail = 'bad'"
    assert exported in source
    assert "UNRELATED" not in record["version"]["ticket"]["description"]


def test_search_failure_never_reserves_request_id(case: Any) -> None:
    ctx, snap, store = case
    wrapped_adapter(ctx, snap, search_error=True)
    for _ in range(2):
        with pytest.raises(t.TicketError, match="JSON"):
            t.draft(ctx, 2, store)
        assert not store.records()


def test_search_rank_time_and_order_are_not_version_fields(case: Any) -> None:
    ctx, snap, store = case
    matches = [{"id": f"EXISTING-{i}", "url": f"https://tracker/{i}", "state": "open",
                "reason": "тот же симптом", "rank": i, "time": "earlier"} for i in (1, 2)]
    control = wrapped_adapter(ctx, snap, matches=matches)
    record = make_request(ctx, store)
    control.write_bytes(t.canonical({"matches": [dict(row, rank=99, time="later")
                                                for row in reversed(matches)]}))
    assert "Создан тикет" in t.submit(ctx, record, record["shown_hash"], store)


def test_declared_unsupported_search_allows_create_only(case: Any) -> None:
    ctx, snap, store = case
    wrapped_adapter(ctx, snap, no_search=True, search_error=True)
    record = make_request(ctx, store)
    assert record["version"]["search"] == {"status": "unsupported", "matches": []}
    assert "Создан тикет" in t.submit(ctx, record, record["shown_hash"], store)
    ctx.update(selected_numbers=[5], params={"operation_5": ["link"], "target_5": ["DEMO-1"]})
    with pytest.raises(t.TicketError, match="Без поиска"):
        t.draft(ctx, 5, store)
    assert len(store.records()) == 1


def test_adapter_receives_only_explicit_environment(case: Any, monkeypatch: Any) -> None:
    ctx, snap, store = case
    monkeypatch.setenv("ALLURE_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak-either")
    monkeypatch.setenv("TRACKER_TEST_TOKEN", "private-token-value")
    ctx["settings"]["adapter_env"] = ["TRACKER_TEST_TOKEN"]
    wrapped_adapter(ctx, snap, check_env=True)
    record = make_request(ctx, store)
    assert "Создан тикет" in t.submit(ctx, record, record["shown_hash"], store)
    assert b"private-token-value" not in t.canonical(store.records())


def test_reconcile_needs_no_run_or_original_context(case: Any) -> None:
    import shutil

    ctx, snap, store = case
    ctx["settings"]["adapter_command"].append("--lose-response-once")
    update_snapshot(ctx, snap)
    record = make_request(ctx, store)
    t.submit(ctx, record, record["shown_hash"], store)
    shutil.rmtree(ctx["run_dir"])
    recovery = {key: ctx[key] for key in ("project_root", "state_dir", "settings")}
    assert "Создан тикет" in t.reconcile(recovery, store.load(record["request_id"]), store)
    assert store.load(record["request_id"])["receipt"]["id"] == "DEMO-1"


def test_revoking_group_does_not_release_unknown_request(case: Any) -> None:
    ctx, _, store = case
    ctx.update(selected_numbers=[2, 5], params={"group": ["2,5"]})
    record = make_request(ctx, store)
    record["state"] = "unknown"
    store.save(record)
    decision = record["version"]["projection"]["basis"]["decision_id"]
    ctx.update(selected_numbers=[], params={"revoke_group": [decision]})
    t.revoke(ctx, store)
    ctx.update(selected_numbers=[2], params={})
    assert "новая отправка запрещена" in t.draft(ctx, 2, store)
    assert store.load(record["request_id"])["state"] == "unknown"


def test_model_shadow_evidence_fields_are_not_sent(case: Any) -> None:
    ctx, snap, store = case
    snap["problems"][0].update(payload={"proof": "FAKE PROOF"}, links=["https://fake.invalid"])
    update_snapshot(ctx, snap)
    record = make_request(ctx, store)
    payload = base64.b64decode(record["version"]["prepared"]["payload"])
    assert b"FAKE PROOF" not in payload and b"fake.invalid" not in payload


def test_saved_research_becomes_automatic_candidate_only_for_same_diagnosis(case: Any) -> None:
    ctx, snap, store = case
    snap["problems"][0]["category"] = "неизвестно"
    assert [row["numbers"] for row in t.assess(ctx, snap, store, automatic=True)["units"]] == [[5], [7]]
    ctx["text_params"] = {"question_2": "Почему запрос не завершился?", "check_2": "Сопоставить trace по операции."}
    update_snapshot(ctx, snap)
    make_request(ctx, store)
    assert [row["numbers"] for row in t.assess(ctx, snap, store, automatic=True)["units"]] == [[2], [5], [7]]
    snap["problems"][0]["cause"] = "Другое неизвестное поведение"
    assert [row["numbers"] for row in t.assess(ctx, snap, store, automatic=True)["units"]] == [[5], [7]]


def test_partial_completed_overlap_does_not_hide_uncovered_problem(case: Any) -> None:
    ctx, snap, store = case
    record = make_request(ctx, store)
    t.submit(ctx, record, record["shown_hash"], store)
    ctx.update(selected_numbers=[2, 5], params={"group": ["2,5"]})
    update_snapshot(ctx, snap)
    with pytest.raises(t.TicketError, match="ещё не обработаны \\[5\\]"):
        t.draft(ctx, 2, store)
    assert len(store.records()) == 1
    assert not store.groups()
    ctx.update(selected_numbers=[2], params={})
    assert "Создан тикет" in t.draft(ctx, 2, store)


def test_atomic_publication_syncs_new_directories_and_destination_parent(tmp_path: Path,
                                                                      monkeypatch: Any) -> None:
    published = tmp_path / "state" / "requests" / "request.json"
    synced = []
    original = t.sync_directory

    def sync(path: Path) -> None:
        synced.append((path, published.exists()))
        original(path)

    monkeypatch.setattr(t, "sync_directory", sync)
    t.atomic(published, {"state": "sending"})
    assert (published.parent, True) in synced
    assert (published.parent, False) in synced
    assert (published.parent.parent, False) in synced
    assert (tmp_path, False) in synced


def test_journal_projection_retains_only_cited_source_fingerprints(case: Any) -> None:
    ctx, snap, store = case
    snap["problems"][0]["sources"].append({"id": "S99", "text": "UNUSED LARGE RAW LOG"})
    update_snapshot(ctx, snap)
    record = make_request(ctx, store)
    sources = record["version"]["projection"]["problems"][0]["sources"]
    assert [source["id"] for source in sources] == ["S1"]
    assert "text" not in sources[0] and "sha256" in sources[0]
    assert b"UNUSED LARGE RAW LOG" not in t.canonical(record)
    snap["problems"][0]["sources"][-1]["text"] = "CHANGED UNUSED LOG"
    update_snapshot(ctx, snap)
    assert "Создан тикет" in t.submit(ctx, record, record["shown_hash"], store)


@pytest.mark.parametrize("field", ["id", "url", "state"])
def test_blank_found_receipt_remains_unknown(case: Any, field: str) -> None:
    ctx, snap, store = case
    wrapped_adapter(ctx, snap, blank_receipt=field)
    record = make_request(ctx, store)
    assert "итог неизвестен" in t.submit(ctx, record, record["shown_hash"], store)
    record = store.load(record["request_id"])
    assert "итог неизвестен" in t.reconcile(ctx, record, store)
    assert store.load(record["request_id"])["state"] == "unknown"


@pytest.mark.skipif(os.name != "posix", reason="descriptor lease is POSIX")
def test_orphan_before_first_database_connect_retains_lease_until_completion(case: Any) -> None:
    ctx, snap, store = case
    gate = Path(ctx["project_root"]) / "release.gate"
    wrapped_adapter(ctx, snap, gate=str(gate))
    record = make_request(ctx, store)
    context_file = Path(ctx["out_dir"]) / "orphan-context.json"
    context_file.write_bytes(t.canonical(ctx))
    env = {**os.environ, "ALLA_CONTEXT": str(context_file)}
    submitter = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "submit.py"), "--request", record["request_id"],
         "--shown", record["shown_hash"]], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    reconciler = None
    try:
        deadline = time.monotonic() + 10
        while not gate.with_suffix(".started").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gate.with_suffix(".started").exists(), submitter.poll()
        assert not (Path(ctx["project_root"]) / "demo.db").exists()
        submitter.kill()
        submitter.communicate(timeout=5)
        assert store.load(record["request_id"])["state"] == "sending"
        reconciler = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "reconcile.py"), "--request", record["request_id"]],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with pytest.raises(subprocess.TimeoutExpired):
            reconciler.wait(timeout=0.2)
        gate.write_text("release", encoding="utf-8")
        output, error = reconciler.communicate(timeout=10)
        assert reconciler.returncode == 0, error
        assert "Создан тикет" in output.decode()
        saved = store.load(record["request_id"])
        assert saved["state"] == "created"
        assert b"_lease_fd" not in t.canonical(saved)
        connection = sqlite3.connect(Path(ctx["project_root"]) / "demo.db")
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
        connection.close()
    finally:
        gate.write_text("release", encoding="utf-8")
        if submitter.poll() is None:
            submitter.kill()
            submitter.communicate(timeout=5)
        if reconciler is not None and reconciler.poll() is None:
            reconciler.kill()
            reconciler.communicate(timeout=5)
