"""Модульные тесты памяти скилла: база знаний проекта, история, правки автотестов."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fixtures import without_libmagic  # noqa: F401

from alla_core.knowledge.models import KBEntry
from alla_core.models.clustering import ClusterSignature, FailureCluster
from alla_core.models.common import TestStatus as Status
from alla_core.models.testops import FailedTestSummary
from alla_skill_lib.history import (
    append_run,
    load_history,
    loose_key,
    recurrence,
    render_recurrence,
)
from alla_skill_lib.kb import (
    KBRecord,
    ProjectKB,
    cluster_signature,
    default_fingerprint,
    fingerprint_hits,
    make_entry_id,
    match_cluster,
    missing_fingerprint_lines,
    secret_lines,
)
from alla_skill_lib.proposals import apply_proposal, is_applied, parse_proposal, validate_proposal

MESSAGE = "Order 0f8a1c2e-1b2c-4d5e-8f90-123456789abc not found: status 404"
TRACE = "java.lang.AssertionError: Order not found\n\tat ru.company.OrderTest.check(OrderTest.java:12)"
LOG = (
    "2026-09-01 10:00:01 [ERROR] OrderService: order lookup failed\n"
    "java.lang.IllegalStateException: missing order"
)


def _cluster(message: str, trace: str, log: str, test_id: int = 1) -> tuple[FailureCluster, dict]:
    test = FailedTestSummary(
        test_result_id=test_id, name="t", status=Status.FAILED,
        status_message=message, status_trace=trace, log_snippet=log,
    )
    cluster = FailureCluster(
        cluster_id=f"c{test_id}", label="l", signature=ClusterSignature(),
        member_test_ids=[test_id], member_count=1, representative_test_id=test_id,
        example_message=message,
    )
    return cluster, {test_id: test}


# --- сигнатура и признак -----------------------------------------------------


def test_signature_is_stable_across_runs_and_pinned() -> None:
    first = cluster_signature(*_cluster(MESSAGE, TRACE, LOG))
    other_run = cluster_signature(*_cluster(
        MESSAGE.replace("0f8a1c2e-1b2c-4d5e-8f90-123456789abc", "11111111-2222-3333-4444-555555555555"),
        TRACE,
        LOG.replace("2026-09-01 10:00:01", "2026-10-05 23:59:59"),
        test_id=987654,
    ))
    different = cluster_signature(*_cluster("Payment declined", TRACE, LOG))

    assert first == other_run
    assert first != different
    # Если хэш изменился после синхронизации ядра — старые записи alla-kb перестанут
    # узнаваться точно: это нужно осознанно учесть, а не пропустить.
    assert first == "v5:f6a84ac7ede67c5fb4228e1dc50d566ce1090a99a1fce340426a75a36643fdbb"


def test_fingerprint_matching_is_number_and_id_agnostic() -> None:
    fingerprint = 'Order "84736251" not found\nERROR] OrderService: lookup failed for 0f8a1c2e-1b2c-4d5e-8f90-123456789abc'
    text = (
        'Order "11112222" not found\r\n'
        "2026-10-05 23:59:59 [ERROR] OrderService: lookup failed for "
        "11111111-2222-3333-4444-555555555555"
    )
    assert fingerprint_hits(fingerprint, text)
    assert missing_fingerprint_lines(fingerprint + "\nPayment declined", text) == ["Payment declined"]
    assert fingerprint_hits("Order not found…", "Order not found in database")
    assert not fingerprint_hits("", text)


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: Bearer abc",
        "password=qwerty",
        '{"password": "hunter2"}',
        '"access_token": "abc123"',
        "'refresh_token': 'x'",
        "client_secret=x",
        "X-Api-Key: 123",
        "Set-Cookie: SESSION=abc",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
    ],
)
def test_secret_lines_are_detected(line: str) -> None:
    assert secret_lines(f"Order not found\n{line}") == [line]


@pytest.mark.parametrize(
    "line",
    ["Order not found", "Authorization failed for user", "token expired", "tokenId=5 is invalid"],
)
def test_ordinary_error_text_is_not_a_secret(line: str) -> None:
    assert secret_lines(line) == []


def test_default_fingerprint_matches_own_evidence() -> None:
    specific = default_fingerprint(MESSAGE, TRACE, LOG)
    assert specific == MESSAGE  # сообщение конкретное — одной строки достаточно

    generic_message = "expected: <200> but was: <500>"
    generic = default_fingerprint(generic_message, TRACE, LOG)
    assert generic.splitlines() == [generic_message, "ERROR] OrderService: order lookup failed"]
    assert fingerprint_hits(generic, "\n".join([generic_message, TRACE, LOG]))

    long_message = "word " * 60 + "0f8a1c2e-1b2c-4d5e-8f90-123456789abc"
    cut = default_fingerprint(long_message, "", "")
    assert len(cut) <= 160 and fingerprint_hits(cut, long_message)

    no_message = default_fingerprint("", TRACE, "")
    assert no_message == "java.lang.AssertionError: Order not found"


# --- хранилище базы знаний ---------------------------------------------------


def _record(entry_id: str = "npe_order_12345678", **overrides: object) -> KBRecord:
    data = {
        "id": entry_id,
        "title": "NPE в OrderService",
        "category": "service",
        "description": "customer не передаётся",
        "resolution_steps": ["Проверять customer"],
        "error_example": "customer is null",
    }
    data.update(overrides)
    return KBRecord.from_json(data)


def test_kb_save_is_stable_and_server_compatible(tmp_path: Path) -> None:
    kb = ProjectKB(tmp_path / "alla-kb")
    record = _record(confirmed_signatures=["v5:b", "v5:a"])
    path = kb.save(record)
    first = path.read_bytes()
    kb.save(kb.get(record.id))  # type: ignore[arg-type]

    assert path.read_bytes() == first
    assert first.endswith(b"\n")
    data = json.loads(first)
    assert data["confirmed_signatures"] == ["v5:a", "v5:b"]
    KBEntry.model_validate(data)
    assert (tmp_path / "alla-kb" / "README.md").is_file()


def test_kb_loader_skips_broken_files(tmp_path: Path) -> None:
    kb = ProjectKB(tmp_path)
    kb.save(_record())
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "conflict.json").write_text("<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> b\n", encoding="utf-8")
    (tmp_path / "renamed.json").write_text(
        json.dumps(_record("other_id").to_json()), encoding="utf-8"
    )
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    (tmp_path / "null.json").write_text("null", encoding="utf-8")
    wrong_type = _record("wrong_type").to_json() | {"resolution_steps": "одной строкой"}
    (tmp_path / "wrong_type.json").write_text(json.dumps(wrong_type), encoding="utf-8")
    no_title = {key: value for key, value in _record("no_title").to_json().items() if key != "title"}
    (tmp_path / "no_title.json").write_text(json.dumps(no_title), encoding="utf-8")

    records, warnings = kb.load()

    assert [record.id for record in records] == ["npe_order_12345678"]
    assert len(warnings) == 7 and all("пропущен" in warning for warning in warnings)
    with pytest.raises(ValueError, match="JSON-объектом"):
        kb.get("list")


def test_kb_ids_and_signature_invariants(tmp_path: Path) -> None:
    kb = ProjectKB(tmp_path)
    with pytest.raises(ValueError):
        kb.path_for("../escape")
    assert make_entry_id("NPE в OrderService", "customer is null").startswith("npe_v_orderservice_")

    record = _record()
    record.confirm("v5:x")
    record.reject("v5:x")
    assert record.rejected_signatures == ["v5:x"] and record.confirmed_signatures == []
    record.confirm("v5:x")
    assert record.confirmed_signatures == ["v5:x"] and record.rejected_signatures == []


def test_match_cluster_orders_exact_first_and_drops_rejected() -> None:
    evidence = "java.lang.NullPointerException: customer is null"
    exact = _record("exact_1", confirmed_signatures=["v5:sig"], error_example="customer is null")
    by_fp = _record("by_fp_1", error_example="NullPointerException")
    rejected = _record("rejected_1", rejected_signatures=["v5:sig"], error_example="customer is null")
    unrelated = _record("unrelated_1", error_example="Payment declined")
    extra = [_record(f"extra_{i}", error_example="customer") for i in range(3)]

    matches = match_cluster([by_fp, rejected, unrelated, exact, *extra], "v5:sig", evidence)

    assert [m["id"] for m in matches] == ["exact_1", "by_fp_1", "extra_0"]
    assert [m["origin"] for m in matches] == ["exact", "fingerprint", "fingerprint"]
    assert matches[0]["category"] == "приложение"


# --- история ---------------------------------------------------------------------


def test_history_recurrence(tmp_path: Path) -> None:
    key = loose_key(MESSAGE, TRACE)
    assert key == loose_key(MESSAGE.replace("404", "500").replace("0f8a", "aaaa"), "")
    append_run(tmp_path, [
        {"date": "2026-09-20", "launch_id": 1, "signature": "v5:a", "loose_key": "k1",
         "category": "приложение", "cause": "старая причина", "kb_entry": None},
        {"date": "2026-09-25", "launch_id": 2, "signature": "v5:other", "loose_key": "k1",
         "category": "окружение", "cause": "последняя причина", "kb_entry": None},
        {"date": "2026-09-26", "launch_id": 2, "signature": "v5:a", "loose_key": "k1",
         "category": "окружение", "cause": "дубль прогона 2", "kb_entry": None},
        {"date": "2026-09-27", "launch_id": 9, "signature": "v5:a", "loose_key": "k1",
         "category": "тест", "cause": "тот же прогон", "kb_entry": None},
        {"date": "2026-09-28", "launch_id": 3, "signature": "v5:z", "loose_key": "kz",
         "category": "данные", "cause": "по записи", "kb_entry": "kb_1"},
    ])
    with (tmp_path / "history.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{broken\n")

    history = load_history(tmp_path)
    assert len(history) == 5

    info = recurrence(history, launch_id=9, signature="v5:a", loose="k1", kb_ids=set())
    assert info is not None
    assert info["launches"] == 2 and info["first_date"] == "2026-09-20"
    assert info["last"]["cause"] == "дубль прогона 2"
    assert recurrence(history, launch_id=9, signature="v5:q", loose="kq", kb_ids={"kb_1"})["launches"] == 1
    assert recurrence(history, launch_id=9, signature="v5:q", loose="kq", kb_ids=set()) is None

    unconfirmed = "\n".join(render_recurrence(info, has_exact_kb=False))
    assert "20.09.2026" in unconfirmed and "не подтверждён" in unconfirmed
    confirmed = "\n".join(render_recurrence(info, has_exact_kb=True))
    assert "дубль" not in confirmed and "базе знаний" in confirmed


# --- правки автотестов ---------------------------------------------------------


JAVA = (
    "class OrderTest {\n"
    "    @Test\n"
    "    void createOrder() {\n"
    '        page.click("#submit-old");\n'
    "        assertEquals(200, api.create().status());\n"
    "    }\n"
    "}\n"
)


def _proposal(before: str, after: str, line: int = 4, why: str = "локатор устарел") -> str:
    return (
        f"**РЕШЕНИЕ:** исправить\nФАЙЛ: src/OrderTest.java:{line}\nБЫЛО:\n```java\n{before}\n```\n"
        f"СТАЛО:\n```java\n{after}\n```\nПОЧЕМУ: {why}\n"
    )


@pytest.fixture
def java_project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "OrderTest.java").write_text(JAVA, encoding="utf-8")
    return tmp_path


def test_proposal_validation(java_project: Path) -> None:
    good = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))
    assert good.is_fix and validate_proposal(good, java_project) == []

    skip = parse_proposal("РЕШЕНИЕ: не трогать\nПОЧЕМУ: в логе приложения NPE")
    assert not skip.is_fix and validate_proposal(skip, java_project) == []

    wrong = parse_proposal(_proposal('page.click("#nope");', 'page.click("#submit");'))
    errors = validate_proposal(wrong, java_project)
    assert "не найдены" in errors[0] and '4:         page.click("#submit-old");' in errors[0]

    far = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#x");', line=90))
    assert validate_proposal(far, java_project)

    weakening = {
        "убирает проверки": "        // assertEquals(200, api.create().status());",
        "отключает тест": "        @Disabled assertEquals(200, api.create().status());",
        "добавляет sleep": "        Thread.sleep(5000); assertEquals(200, api.create().status());",
    }
    for message, after in weakening.items():
        proposal = parse_proposal(_proposal("        assertEquals(200, api.create().status());", after, line=5))
        assert any(message in error for error in validate_proposal(proposal, java_project)), message

    (java_project / "alla-kb").mkdir()
    (java_project / "alla-kb" / "x.json").write_text("{}", encoding="utf-8")
    foreign = parse_proposal(_proposal("{}", '{"a": 1}').replace("src/OrderTest.java", "alla-kb/x.json"))
    assert "не относится к коду автотестов" in validate_proposal(foreign, java_project)[0]


def test_timeout_increase_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "t.py").write_text("wait_for(ready, timeout=5)\n", encoding="utf-8")
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: t.py:1\nБЫЛО:\nwait_for(ready, timeout=5)\n"
        "СТАЛО:\nwait_for(ready, timeout=60)\nПОЧЕМУ: долго"
    )
    assert any("таймаут" in error for error in validate_proposal(proposal, tmp_path))


def test_apply_shows_diff_then_applies_once(java_project: Path) -> None:
    target = java_project / "src" / "OrderTest.java"
    target.write_text(JAVA.replace("\n", "\r\n"), encoding="utf-8", newline="")
    proposal = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))

    status, diff = apply_proposal(proposal, java_project, confirm=False)
    assert status == "diff" and '+        page.click("#submit");' in diff
    assert "#submit-old" in target.read_text(encoding="utf-8")

    status, _ = apply_proposal(proposal, java_project, confirm=True)
    content = target.read_bytes().decode("utf-8")
    assert status == "applied"
    assert '        page.click("#submit");\r\n' in content and "#submit-old" not in content
    assert content.endswith("}\r\n") and not content.endswith("\r\n\r\n")
    assert is_applied(proposal, java_project)

    status, message = apply_proposal(proposal, java_project, confirm=True)
    assert status == "applied" and "уже применена" in message


def test_apply_refuses_when_code_changed_or_ambiguous(java_project: Path) -> None:
    target = java_project / "src" / "OrderTest.java"
    proposal = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))

    target.write_text(JAVA.replace("#submit-old", "#changed"), encoding="utf-8")
    assert apply_proposal(proposal, java_project, confirm=True)[0] == "error"

    doubled = JAVA.replace(
        '        page.click("#submit-old");\n',
        '        page.click("#submit-old");\n        page.click("#submit-old");\n',
    )
    target.write_text(doubled, encoding="utf-8")
    status, message = apply_proposal(proposal, java_project, confirm=True)
    assert status == "error" and "2 раз" in message
