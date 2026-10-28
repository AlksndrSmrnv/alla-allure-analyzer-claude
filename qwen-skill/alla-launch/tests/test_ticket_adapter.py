"""Реальные процессы демонстрационного адаптера: протокол, байты и восстановление."""

from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ADAPTERS = Path(__file__).resolve().parents[1] / "extensions" / "tickets" / "adapters"


def ticket(**updates: Any) -> dict[str, Any]:
    return {"kind": "bug", "team": "backend", "title": "NPE при создании заказа",
            "description": "Фактический результат: ответ 500 вместо создания заказа.",
            "problems": [2, 5], "tests": [{"name": "createOrder", "link": "https://testops/2"}],
            "evidence": [{"problem": 2, "source_id": "S1", "quote": "NullPointerException"}],
            "launch_id": 777, "run_uid": str(uuid.UUID(int=7)), **updates}


def invoke(db: Path, request: dict[str, Any], *args: str,
           adapter: str = "demo.py") -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(ADAPTERS / adapter)]
    if adapter == "demo.py":
        command += ["--db", str(db), "--project", "DEMO"]
    return subprocess.run(command + list(args), input=json.dumps(request, ensure_ascii=False),
                          capture_output=True, text=True, encoding="utf-8", timeout=10,
                          env={"PYTHONIOENCODING": "utf-8"})


def response(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert process.returncode == 0, process.stdout + process.stderr
    value: dict[str, Any] = json.loads(process.stdout)
    assert value["api"] == 1
    assert len(process.stdout.splitlines()) == 1
    return value


def prepared(db: Path, *, request_id: str | None = None, action: str = "create",
             target_id: str | None = None, **updates: Any) -> tuple[str, dict[str, Any]]:
    request_id = request_id or str(uuid.uuid4())
    result = response(invoke(db, {"api": 1, "operation": "prepare", "request_id": request_id,
                                 "action": action, "target_id": target_id,
                                 "ticket": ticket(**updates)}))
    return request_id, result


def mutation(request_id: str, draft: dict[str, Any], *, operation: str = "create") -> dict[str, Any]:
    return {"api": 1, "operation": operation, "request_id": request_id,
            "scope": draft["scope"], "payload": draft["payload"]}


def lookup(request_id: str, draft: dict[str, Any], action: str = "create") -> dict[str, Any]:
    return {"api": 1, "operation": "lookup", "request_id": request_id,
            "scope": draft["scope"], "action": action}


def test_prepare_is_pure_deterministic_and_scope_ignores_request_id(tmp_path: Path) -> None:
    db = tmp_path / "not-created" / "tracker.sqlite"
    request_id = str(uuid.uuid4())
    _, first = prepared(db, request_id=request_id)
    _, again = prepared(db, request_id=request_id)
    _, other_request = prepared(db)
    assert first == again
    assert first["scope"] == other_request["scope"]
    assert set(first["scope"]) == {"site", "project_or_queue"}
    assert first["capabilities"] == {"search": True, "authoritative_absence": True}
    assert not db.parent.exists()
    decoded = json.loads(base64.b64decode(first["payload"]))
    assert decoded["ticket"] == ticket()
    assert decoded["request_id"] == request_id


def test_read_operations_do_not_create_demo_database(tmp_path: Path) -> None:
    db = tmp_path / "absent" / "tracker.sqlite"
    request_id, draft = prepared(db)
    assert response(invoke(db, {"api": 1, "operation": "search", "request_id": request_id,
                               "scope": draft["scope"], "ticket": ticket()}))["matches"] == []
    assert response(invoke(db, lookup(request_id, draft)))["outcome"] == "absent"
    assert not db.parent.exists()


def test_create_preserves_payload_and_repeated_delivery_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "tracker.sqlite"
    request_id, draft = prepared(db)
    request = mutation(request_id, draft)
    first = response(invoke(db, request))
    second = response(invoke(db, request))
    assert first == second
    assert first["ticket"]["id"] == "DEMO-1"
    assert first["ticket"]["url"].startswith("demo://")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
        assert connection.execute("SELECT payload FROM tickets").fetchone()[0] == draft["payload"]
        assert connection.execute("SELECT payload FROM requests").fetchone()[0] == draft["payload"]


def test_lost_response_is_recovered_by_lookup_without_second_ticket(tmp_path: Path) -> None:
    db = tmp_path / "tracker.sqlite"
    request_id, draft = prepared(db)
    lost = invoke(db, mutation(request_id, draft), "--lose-response-once")
    assert lost.returncode == 75 and not lost.stdout
    found = response(invoke(db, lookup(request_id, draft)))
    assert found["outcome"] == "found"
    assert found["ticket"]["id"] == "DEMO-1"
    assert response(invoke(db, mutation(request_id, draft), "--lose-response-once")) == found
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1


def test_definite_absence_then_same_request_id_can_create(tmp_path: Path) -> None:
    db = tmp_path / "tracker.sqlite"
    request_id, draft = prepared(db)
    assert response(invoke(db, lookup(request_id, draft)))["outcome"] == "absent"
    assert response(invoke(db, mutation(request_id, draft)))["outcome"] == "found"
    assert response(invoke(db, lookup(request_id, draft)))["outcome"] == "found"


def test_external_link_records_new_evidence_and_recovers_lost_reply(tmp_path: Path) -> None:
    db = tmp_path / "tracker.sqlite"
    initial_id, initial = prepared(db)
    response(invoke(db, mutation(initial_id, initial)))
    link_id, link = prepared(db, action="link", target_id="DEMO-1",
                             description="Новые доказательства: запрос testops/5 завершился с NPE.")
    assert invoke(db, mutation(link_id, link, operation="link"),
                  "--lose-response-once").returncode == 75
    found = response(invoke(db, lookup(link_id, link, "link")))
    assert found["ticket"]["id"] == "DEMO-1"
    response(invoke(db, mutation(link_id, link, operation="link")))
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
        updates = connection.execute("SELECT request_id,payload FROM ticket_updates").fetchall()
        assert updates == [(link_id, link["payload"])]


def test_search_is_scoped_and_has_only_stable_match_fields(tmp_path: Path) -> None:
    db = tmp_path / "tracker.sqlite"
    request_id, draft = prepared(db)
    response(invoke(db, mutation(request_id, draft)))
    result = response(invoke(db, {"api": 1, "operation": "search", "request_id": request_id,
                                 "scope": draft["scope"], "ticket": ticket()}))
    assert len(result["matches"]) == 1
    assert set(result["matches"][0]) == {"id", "url", "state", "reason"}
    other_id, other = prepared(db, team="platform")
    assert response(invoke(db, {"api": 1, "operation": "search", "request_id": other_id,
                               "scope": other["scope"], "ticket": ticket()}))["matches"] == []


def test_lookup_never_claims_absence_for_same_id_in_another_queue(tmp_path: Path) -> None:
    db = tmp_path / "tracker.sqlite"
    request_id, draft = prepared(db)
    response(invoke(db, mutation(request_id, draft)))
    _, other = prepared(db, request_id=request_id, team="platform")
    assert response(invoke(db, lookup(request_id, other)))["outcome"] == "unknown"
    assert response(invoke(db, lookup(request_id, draft, "link")))["outcome"] == "unknown"


def test_same_request_id_with_changed_payload_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "tracker.sqlite"
    request_id, draft = prepared(db)
    response(invoke(db, mutation(request_id, draft)))
    _, changed = prepared(db, request_id=request_id, title="Другой заголовок")
    failure = invoke(db, mutation(request_id, changed))
    assert failure.returncode == 1 and json.loads(failure.stdout)["error"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1


@pytest.mark.parametrize("updates", [{"kind": "invalid"}, {"team": ""},
                                     {"kind": "investigation"}])
def test_invalid_preparation_creates_no_external_state(tmp_path: Path,
                                                     updates: dict[str, Any]) -> None:
    db = tmp_path / "not-created" / "tracker.sqlite"
    request = {"api": 1, "operation": "prepare", "request_id": str(uuid.uuid4()),
               "action": "create", "target_id": None, "ticket": ticket(**updates)}
    failure = invoke(db, request)
    assert failure.returncode == 1 and set(json.loads(failure.stdout)) == {"api", "error"}
    assert not db.parent.exists()


def test_template_fails_closed_for_every_operation(tmp_path: Path) -> None:
    for operation in ("prepare", "search", "create", "link", "lookup"):
        process = invoke(tmp_path / "unused", {"api": 1, "operation": operation,
                                               "request_id": str(uuid.uuid4())},
                         adapter="template.py")
        assert process.returncode == 1
        assert json.loads(process.stdout) == {"api": 1, "error": "Адаптер трекера ещё не подключён"}
        assert not process.stderr


def test_lookup_waits_for_uncommitted_mutation_before_claiming_absence(tmp_path: Path) -> None:
    db = tmp_path / "tracker.sqlite"
    initial_id, initial = prepared(db)
    response(invoke(db, mutation(initial_id, initial)))
    request_id, draft = prepared(db)
    connection = sqlite3.connect(db)
    connection.execute("BEGIN IMMEDIATE")
    number = connection.execute(
        "INSERT INTO tickets(scope,title,payload) VALUES (?,?,?)",
        (json.dumps(draft["scope"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
         "Pending", draft["payload"])).lastrowid
    connection.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                       (request_id, "create", json.dumps(draft["scope"], ensure_ascii=False,
                                                         sort_keys=True, separators=(",", ":")),
                        draft["payload"], number))
    process = subprocess.Popen([sys.executable, str(ADAPTERS / "demo.py"), "--db", str(db)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8")
    assert process.stdin is not None
    process.stdin.write(json.dumps(lookup(request_id, draft)))
    process.stdin.close()
    try:
        # A plain SELECT would finish with absent while this write is uncommitted.
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.25)
    finally:
        connection.commit()
        connection.close()
    assert process.stdout is not None
    output = process.stdout.read()
    assert process.wait(timeout=10) == 0
    assert json.loads(output)["outcome"] == "found"
