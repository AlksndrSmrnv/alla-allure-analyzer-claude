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
    store_fingerprint,
)
from alla_skill_lib.proposals import (
    ProposalFiles,
    apply_proposal,
    applied_state,
    is_applied,
    parse_proposal,
    revert_proposal,
    validate_proposal,
    weakening_errors,
    weakening_warnings,
)

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
        "jdbc:postgresql://app:pw@db.internal/orders",
        "GET https://user:pass@host/path failed",
        "key AKIAIOSFODNN7EXAMPLE rejected",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "-----BEGIN RSA PRIVATE KEY-----",
        "пароль: hunter2",
        "session_id: 12ab34",
    ],
)
def test_secret_lines_are_detected(line: str) -> None:
    assert secret_lines(f"Order not found\n{line}") == [line]


@pytest.mark.parametrize(
    "line",
    [
        "Order not found", "Authorization failed for user", "token expired", "tokenId=5 is invalid",
        "Invalid token: expired", "Session id: missing in request",
        "Failed to reach http://host:8080/orders",
    ],
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


def test_kb_keeps_hand_added_fields_and_writes_atomically(tmp_path: Path) -> None:
    kb = ProjectKB(tmp_path / "alla-kb")
    record = _record()
    path = kb.save(record)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update({"jira": "QA-123", "owner": "team-orders", "tags": ["api", "orders"]})
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = kb.get(record.id)
    assert loaded is not None
    loaded.confirm("v5:new")  # remember/reject перезаписывают файл целиком
    kb.save(loaded)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert (saved["jira"], saved["owner"], saved["tags"]) == ("QA-123", "team-orders", ["api", "orders"])
    assert saved["confirmed_signatures"] == ["v5:new"]
    assert "step_path" not in saved
    assert not list((tmp_path / "alla-kb").glob("*.tmp"))
    assert b"\r\n" not in path.read_bytes()


def test_kb_get_reports_conflict_markers(tmp_path: Path) -> None:
    kb = ProjectKB(tmp_path)
    (tmp_path / "npe_order_12345678.json").write_text(
        "<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> b\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="конфликт"):
        kb.get("npe_order_12345678")


def test_stored_fingerprint_is_normalized_and_still_matches() -> None:
    raw = (
        "Order 0f8a1c2e-1b2c-4d5e-8f90-123456789abc not found for anna@company.ru\n"
        "2026-09-01 10:00:01 timeout after 1500 ms"
    )
    stored = store_fingerprint(raw)
    assert "0f8a1c2e" not in stored and "anna@company.ru" not in stored
    assert "<ID>" in stored and "<EMAIL>" in stored
    evidence = (
        "prefix\nOrder aaaaaaaa-1111-2222-3333-444444444444 not found for bob@other.org\n"
        "2026-10-02 11:22:33 timeout after 2500 ms\n"
    )
    assert fingerprint_hits(stored, evidence)
    assert not fingerprint_hits(stored, "Order not found for nobody")


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
    append_run(tmp_path, [
        {"date": "2026-09-20", "launch_id": 1, "signature": "v5:a",
         "category": "приложение", "cause": "старая причина", "kb_entry": None},
        {"date": "2026-09-25", "launch_id": 2, "signature": "v5:other",
         "category": "окружение", "cause": "другая ошибка", "kb_entry": None},
        {"date": "2026-09-26", "launch_id": 2, "signature": "v5:a",
         "category": "окружение", "cause": "дубль прогона 2", "kb_entry": None},
        {"date": "2026-09-27", "launch_id": 9, "signature": "v5:a",
         "category": "тест", "cause": "тот же прогон", "kb_entry": None},
        {"date": "2026-09-28", "launch_id": 3, "signature": "v5:z",
         "category": "данные", "cause": "по записи", "kb_entry": "kb_1"},
    ])
    with (tmp_path / "history.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{broken")  # оборванная строка без перевода строки

    append_run(tmp_path, [
        {"date": "2026-09-29", "launch_id": 4, "signature": "v5:tail", "kb_entry": None},
    ])  # не должна склеиться с оборванной строкой
    history = load_history(tmp_path)
    assert len(history) == 6 and history[-1]["launch_id"] == 4

    info = recurrence(history, launch_id=9, signature="v5:a", kb_ids=set())
    assert info == {"launches": 2, "first_date": "2026-09-20", "last_date": "2026-09-26"}
    assert recurrence(history, launch_id=9, signature="v5:q", kb_ids={"kb_1"})["launches"] == 1
    assert recurrence(history, launch_id=9, signature="v5:q", kb_ids=set()) is None
    # Похожая по первой строке, но другая ошибка (другая сигнатура) повтором не считается.
    assert recurrence(history, launch_id=9, signature="v5:similar", kb_ids=set()) is None

    text = "\n".join(render_recurrence(info, has_exact_kb=False))
    assert "20.09.2026" in text and "2 других прогонах" in text
    # Прошлые выводы модели в задание не попадают — они не подтверждены.
    assert "дубль" not in text and "причина" not in text.lower().replace("подтверждённая", "")
    confirmed = "\n".join(render_recurrence(info, has_exact_kb=True))
    assert "базе знаний" in confirmed


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


def _files(tmp_path: Path, name: str = "01") -> ProposalFiles:
    folder = tmp_path / "proposals"
    return ProposalFiles(
        record=folder / f"{name}.applied.json",
        backup=folder / f"{name}.orig",
        patch=folder / f"{name}.patch",
    )


def _apply(proposal, root: Path, files: ProposalFiles | None = None):
    """Показать diff, затем применить ровно его — как это делает агент."""
    shown = apply_proposal(proposal, root, files=files)
    assert shown.status == "diff", shown.text
    return shown, apply_proposal(
        proposal, root, confirm=True, diff_hash=shown.diff_hash, files=files
    )


def test_timeout_increase_is_a_warning_not_a_rejection(tmp_path: Path) -> None:
    (tmp_path / "t.py").write_text("wait_for(ready, timeout=5)\n", encoding="utf-8")
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: t.py:1\nБЫЛО:\nwait_for(ready, timeout=5)\n"
        "СТАЛО:\nwait_for(ready, timeout=60)\nПОЧЕМУ: долго"
    )
    assert validate_proposal(proposal, tmp_path) == []
    shown = apply_proposal(proposal, tmp_path)
    assert shown.status == "diff" and "Проверь: увеличено значение ожидания" in shown.text


def test_apply_shows_diff_then_applies_once(java_project: Path, tmp_path: Path) -> None:
    target = java_project / "src" / "OrderTest.java"
    target.write_text(JAVA.replace("\n", "\r\n"), encoding="utf-8", newline="")
    files = _files(tmp_path)
    proposal = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))

    shown = apply_proposal(proposal, java_project, files=files)
    assert shown.status == "diff" and '+        page.click("#submit");' in shown.text
    assert "#submit-old" in target.read_text(encoding="utf-8")
    assert files.patch.read_text(encoding="utf-8") == shown.text  # тот же diff сохранён в NN.patch

    result = apply_proposal(proposal, java_project, confirm=True, diff_hash=shown.diff_hash, files=files)
    content = target.read_bytes().decode("utf-8")
    assert result.status == "applied" and result.changed
    assert '        page.click("#submit");\r\n' in content and "#submit-old" not in content
    assert content.endswith("}\r\n") and not content.endswith("\r\n\r\n")
    assert is_applied(proposal, java_project, files)

    again = apply_proposal(proposal, java_project, confirm=True, diff_hash=shown.diff_hash, files=files)
    assert again.status == "applied" and not again.changed and "уже применена" in again.text


def test_yes_without_matching_diff_hash_shows_diff_again(java_project: Path) -> None:
    target = java_project / "src" / "OrderTest.java"
    proposal = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))

    for wrong in (None, "deadbeef"):
        result = apply_proposal(proposal, java_project, confirm=True, diff_hash=wrong)
        assert result.status == "diff" and "Хэш --diff не совпал" in result.text
        assert result.diff_hash and "#submit-old" in target.read_text(encoding="utf-8")


def test_apply_refuses_when_code_changed_or_ambiguous(java_project: Path) -> None:
    target = java_project / "src" / "OrderTest.java"
    proposal = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))

    target.write_text(JAVA.replace("#submit-old", "#changed"), encoding="utf-8")
    assert apply_proposal(proposal, java_project, confirm=True).status == "error"

    # Два одинаковых места на равном расстоянии от указанной строки — неоднозначно.
    around = JAVA.replace(
        '        page.click("#submit-old");\n',
        '        page.click("#submit-old");\n        page.log();\n        page.click("#submit-old");\n',
    )
    target.write_text(around, encoding="utf-8")
    equidistant = parse_proposal(
        _proposal('        page.click("#submit-old");', '        page.click("#submit");', line=5)
    )
    result = apply_proposal(equidistant, java_project, confirm=True)
    assert result.status == "error" and "несколько одинаковых мест" in result.text
    assert target.read_text(encoding="utf-8") == around


def test_apply_touches_only_the_stated_place(tmp_path: Path) -> None:
    """Правка у строки 10 уже стоит — совпадение в другом тесте (строка 100) не трогается."""
    body = (
        ["class T {"] + ["    // filler"] * 7
        + ["    void a() {", "        page.waitUntilReady();", "        page.click();", "    }"]
        + ["    // filler"] * 86
        + ["    void b() {", "        page.click();", "    }", "}"]
    )
    target = tmp_path / "T.java"
    target.write_text("\n".join(body) + "\n", encoding="utf-8")
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:10\nБЫЛО:\n        page.click();\n"
        "СТАЛО:\n        page.waitUntilReady();\n        page.click();\nПОЧЕМУ: нет ожидания"
    )
    before = target.read_text(encoding="utf-8")

    assert is_applied(proposal, tmp_path)
    result = apply_proposal(proposal, tmp_path, confirm=True)
    assert result.status == "applied" and "уже применена" in result.text
    assert target.read_text(encoding="utf-8") == before

    # Та же правка, указанная на строку 100, меняет только b().
    at_b = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:100\nБЫЛО:\n        page.click();\n"
        "СТАЛО:\n        page.waitUntilReady();\n        page.click();\nПОЧЕМУ: нет ожидания"
    )
    assert _apply(at_b, tmp_path)[1].status == "applied"
    lines = target.read_text(encoding="utf-8").split("\n")
    assert [i + 1 for i, line in enumerate(lines) if "waitUntilReady" in line] == [10, 100]


def test_apply_when_after_contains_before_is_not_repeated(tmp_path: Path) -> None:
    target = tmp_path / "T.java"
    target.write_text("class T {\n    void t() {\n        page.click();\n    }\n}\n", encoding="utf-8")
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:3\nБЫЛО:\n        page.click();\n"
        "СТАЛО:\n        page.waitUntilReady();\n        page.click();\nПОЧЕМУ: нет ожидания"
    )

    assert not is_applied(proposal, tmp_path)
    assert _apply(proposal, tmp_path)[1].status == "applied"
    assert is_applied(proposal, tmp_path)  # БЫЛО внутри вставленного СТАЛО — не новое место
    result = apply_proposal(proposal, tmp_path, confirm=True)
    assert result.status == "applied" and "уже применена" in result.text
    assert target.read_text(encoding="utf-8").count("waitUntilReady") == 1
    assert validate_proposal(proposal, tmp_path) == []


@pytest.mark.parametrize(
    ("before", "after", "warned"),
    [
        ("wait(Duration.ofMillis(500));", "wait(Duration.ofSeconds(30));", True),
        ("setTimeout(60000); poll(timeout=1000)", "setTimeout(60000); poll(timeout=5000)", True),
        ("wait.withTimeout(30, TimeUnit.SECONDS)", "wait.withTimeout(2, TimeUnit.MINUTES)", True),
        ("timeout: 30s", "timeout: 2min", True),
        ("wait_for(ready, timeout=5)", "wait_for(ready, timeout=60)", True),
        ("deadline = timedelta(seconds=30)", "deadline = timedelta(minutes=5)", True),
        ("wait(Duration.ofSeconds(30));", "wait(Duration.ofMillis(500));", False),
        ('waitForElement("#item-3", 10)', 'waitForElement("#item-4", 10)', False),
        ("timeout(60, SECONDS)", "timeout(60, SECONDS)  // тот же", False),
        # уменьшение одного ожидания не скрывает увеличение другого
        ("a(Duration.ofSeconds(60)); b(Duration.ofSeconds(1));",
         "a(Duration.ofSeconds(30)); b(Duration.ofSeconds(5));", True),
        ("a(Duration.ofSeconds(60)); b(Duration.ofSeconds(10));",
         "a(Duration.ofSeconds(30)); b(Duration.ofSeconds(5));", False),
        # перенос значения из одного вызова в другой — увеличение ожидания b
        ("a(Duration.ofSeconds(60)); b(Duration.ofSeconds(1));",
         "a(Duration.ofSeconds(1)); b(Duration.ofSeconds(60));", True),
        ("a(timeout=5); b(timeout=60)", "a(timeout=60); b(timeout=5)", True),
        # само ожидание wait выросло, check(1s) — новое
        ("wait(Duration.ofSeconds(1));",
         "wait(Duration.ofSeconds(5)); check(Duration.ofSeconds(1));", True),
    ],
)
def test_timeout_growth_warning_respects_units(before: str, after: str, warned: bool) -> None:
    assert weakening_errors([before], [after]) == []  # предупреждение, не отказ
    assert any("ожидания" in w for w in weakening_warnings([before], [after])) is warned


def test_new_explicit_wait_is_not_a_timeout_increase() -> None:
    before = ["page.click();"]
    after = ["wait.until(visible(el), Duration.ofSeconds(10));", "page.click();"]
    assert weakening_errors(before, after) == [] and weakening_warnings(before, after) == []


def test_reordered_waits_are_not_an_increase() -> None:
    before = ["a(Duration.ofSeconds(60));", "b(Duration.ofSeconds(1));"]
    after = ["b(Duration.ofSeconds(1));", "a(Duration.ofSeconds(60));"]
    assert weakening_warnings(before, after) == []
    same_line = ["a(Duration.ofSeconds(60)); b(Duration.ofSeconds(1));"]
    swapped_calls = ["b(Duration.ofSeconds(1)); a(Duration.ofSeconds(60));"]
    assert weakening_warnings(same_line, swapped_calls) == []


def test_changed_expected_value_is_a_warning() -> None:
    warnings = weakening_warnings(
        ["        assertEquals(200, api.create().status());"],
        ["        assertEquals(201, api.create().status());"],
    )
    assert len(warnings) == 1 and "200 → 201" in warnings[0]
    assert weakening_warnings(["assertEquals(200, x);"], ["assertEquals(200, y);"]) == []


@pytest.mark.parametrize(
    ("before", "after", "reported"),
    [
        # переименование аргумента проверок не убирает
        ("assertEquals(expectedCode, api.status());", "assertEquals(201, api.status());", False),
        ("assert x == expected_status", "assert x == 201", False),
        # «#» и «//» внутри строк — не комментарий
        ('$("#total").shouldHave(text("5"));', '$("#total").click();', True),
        ('open("http://app"); assertEquals(1, x);', 'open("http://app");', True),
        ('open("http://app");', 'open("http://app"); // assertEquals(1, x);', False),
        ("assertEquals(1, x); // проверка", "// assertEquals(1, x);", True),
        # RestAssured, Gherkin, JS-проверки
        ("given().get(u).then().statusCode(200).body(\"a\", eq(1));", "given().get(u).then();", True),
        ("Then the status is 200", "And the status is 200", True),
        ("expect(res.status).toBe(200);", "await res;", True),
        # отключение и глушение
        ("[Fact]\nvoid T() {}", "[Fact(Skip = \"flaky\")]\nvoid T() {}", True),
        ("[Test]", "[Test][Ignore]", True),
        ("void t() {}", "@Retry(3)\nvoid t() {}", True),
        ("await page.click();", "await page.click().catch(() => {});", True),
        ("try { go(); } catch (Exception e) { log(e); }", "try { go(); } catch (Exception e) {}", True),
        ("go();", "try { go(); } catch {}", True),
    ],
)
def test_weakening_detection(before: str, after: str, reported: bool) -> None:
    assert bool(weakening_errors(before.split("\n"), after.split("\n"))) is reported


def test_removing_duplicate_line_is_located_and_applied_once(tmp_path: Path) -> None:
    """СТАЛО внутри ещё целого БЫЛО (убрать двойной клик) — не признак применения."""
    target = tmp_path / "T.java"
    target.write_text(
        "class T {\n    void t() {\n        page.click();\n        page.click();\n    }\n}\n",
        encoding="utf-8",
    )
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:3\nБЫЛО:\n        page.click();\n        page.click();\n"
        "СТАЛО:\n        page.click();\nПОЧЕМУ: двойной клик отправляет форму дважды"
    )

    assert validate_proposal(proposal, tmp_path) == []
    assert not is_applied(proposal, tmp_path)
    assert _apply(proposal, tmp_path)[1].status == "applied"
    assert target.read_text(encoding="utf-8").count("page.click();") == 1
    assert is_applied(proposal, tmp_path)
    result = apply_proposal(proposal, tmp_path, confirm=True)
    assert result.status == "applied" and "уже применена" in result.text
    assert target.read_text(encoding="utf-8").count("page.click();") == 1


THREE_CLICKS = (
    "class T {\n    void t() {\n        page.click();\n        page.click();\n"
    "        page.click();\n    }\n}\n"
)
REMOVE_ONE_CLICK = (
    "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:3\nБЫЛО:\n        page.click();\n        page.click();\n"
    "СТАЛО:\n        page.click();\nПОЧЕМУ: двойной клик отправляет форму дважды"
)


def test_repeated_apply_with_overlapping_matches_uses_record(tmp_path: Path) -> None:
    """Три click() подряд, правка убирает один: повтор не должен удалять ещё."""
    target = tmp_path / "T.java"
    target.write_text(THREE_CLICKS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(REMOVE_ONE_CLICK)

    shown = apply_proposal(proposal, tmp_path, files=files)
    assert shown.status == "diff" and not files.record.exists()  # показ ничего не фиксирует

    result = apply_proposal(proposal, tmp_path, confirm=True, diff_hash=shown.diff_hash, files=files)
    assert result.status == "applied"
    assert target.read_text(encoding="utf-8").count("page.click();") == 2
    assert json.loads(files.record.read_text(encoding="utf-8"))["line"] == 3
    assert is_applied(proposal, tmp_path, files)

    for _ in range(2):
        again = apply_proposal(proposal, tmp_path, confirm=True, diff_hash=shown.diff_hash, files=files)
        assert again.status == "applied" and "уже применена" in again.text
    assert target.read_text(encoding="utf-8").count("page.click();") == 2


def test_revert_restores_original_and_refuses_after_later_edits(java_project: Path, tmp_path: Path) -> None:
    target = java_project / "src" / "OrderTest.java"
    files = _files(tmp_path)
    proposal = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))
    original = target.read_bytes()

    assert revert_proposal(java_project, files).status == "error"  # ещё не применяли

    assert _apply(proposal, java_project, files)[1].status == "applied"
    assert target.read_bytes() != original and files.backup.read_bytes() == original
    reverted = revert_proposal(java_project, files)
    assert reverted.status == "reverted" and target.read_bytes() == original
    assert not files.record.exists() and not is_applied(proposal, java_project, files)

    # После отката правку можно применить снова, а если файл потом правили руками — откат не затирает.
    assert _apply(proposal, java_project, files)[1].status == "applied"
    target.write_text(target.read_text(encoding="utf-8") + "// manual\n", encoding="utf-8")
    refused = revert_proposal(java_project, files)
    assert refused.status == "error" and "изменён после apply" in refused.text
    assert "// manual" in target.read_text(encoding="utf-8")


def test_revert_refuses_a_corrupted_or_replaced_backup(java_project: Path, tmp_path: Path) -> None:
    target = java_project / "src" / "OrderTest.java"
    files = _files(tmp_path)
    proposal = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))
    original = target.read_bytes()
    assert _apply(proposal, java_project, files)[1].status == "applied"
    applied = target.read_bytes()
    record = files.record.read_text(encoding="utf-8")

    for bad in (b"", original[:-5], b"\xff\xfe not a source file", original + b"// extra\n"):
        files.backup.write_bytes(bad)
        refused = revert_proposal(java_project, files)
        assert refused.status == "error" and "не совпадает с версией файла до правки" in refused.text
        assert target.read_bytes() == applied  # исправный файл не затёрт
        assert files.record.read_text(encoding="utf-8") == record  # отметка применения на месте
        assert is_applied(proposal, java_project, files)

    # Настоящая копия по-прежнему откатывает.
    files.backup.write_bytes(original)
    assert revert_proposal(java_project, files).status == "reverted"
    assert target.read_bytes() == original and not files.record.exists()


def test_revert_refuses_a_record_without_backup_hash(java_project: Path, tmp_path: Path) -> None:
    target = java_project / "src" / "OrderTest.java"
    files = _files(tmp_path)
    proposal = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))
    assert _apply(proposal, java_project, files)[1].status == "applied"
    applied = target.read_bytes()
    data = json.loads(files.record.read_text(encoding="utf-8"))
    del data["sha_before"]
    files.record.write_text(json.dumps(data), encoding="utf-8")

    refused = revert_proposal(java_project, files)
    assert refused.status == "error" and "нет хэша резервной копии" in refused.text
    assert target.read_bytes() == applied and files.record.exists()


def test_record_is_ignored_after_revert_or_rewrite(java_project: Path, tmp_path: Path) -> None:
    target = java_project / "src" / "OrderTest.java"
    files = _files(tmp_path)
    proposal = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#submit");'))
    assert _apply(proposal, java_project, files)[1].status == "applied"

    # Правку откатили вручную — отметка устарела, применить снова можно.
    target.write_text(JAVA, encoding="utf-8")
    assert not is_applied(proposal, java_project, files)
    assert _apply(proposal, java_project, files)[1].status == "applied"
    assert '"#submit"' in target.read_text(encoding="utf-8")

    # Предложение переписали — старая отметка к нему не относится.
    target.write_text(JAVA, encoding="utf-8")
    rewritten = parse_proposal(_proposal('        page.click("#submit-old");', '        page.click("#send");'))
    assert not is_applied(rewritten, java_project, files)


def test_line_endings_are_preserved_per_line(tmp_path: Path) -> None:
    target = tmp_path / "T.java"
    target.write_bytes(b"a();\r\nb();\nc();\r\nd();")  # смесь окончаний, без перевода в конце
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:3\nБЫЛО:\nc();\nСТАЛО:\nc1();\nc2();\nПОЧЕМУ: x"
    )
    assert _apply(proposal, tmp_path)[1].status == "applied"
    assert target.read_bytes() == b"a();\r\nb();\nc1();\r\nc2();\r\nd();"

    tail = tmp_path / "U.java"
    tail.write_bytes(b"x();\ny();")
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: U.java:2\nБЫЛО:\ny();\nСТАЛО:\ny1();\ny2();\nПОЧЕМУ: x"
    )
    assert _apply(proposal, tmp_path)[1].status == "applied"
    assert tail.read_bytes() == b"x();\ny1();\ny2();"


def test_bom_is_kept_and_non_utf8_is_refused(tmp_path: Path) -> None:
    bom = tmp_path / "B.java"
    bom.write_bytes(b"\xef\xbb\xbfa();\nb();\n")
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: B.java:2\nБЫЛО:\nb();\nСТАЛО:\nc();\nПОЧЕМУ: x"
    )
    assert _apply(proposal, tmp_path)[1].status == "applied"
    assert bom.read_bytes() == b"\xef\xbb\xbfa();\nc();\n"

    legacy = tmp_path / "Old.java"
    legacy.write_bytes("// тест\nb();\n".encode("cp1251"))
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: Old.java:2\nБЫЛО:\nb();\nСТАЛО:\nc();\nПОЧЕМУ: x"
    )
    errors = validate_proposal(proposal, tmp_path)  # раньше здесь был UnicodeDecodeError
    assert errors and "не в кодировке UTF-8" in errors[0]
    assert apply_proposal(proposal, tmp_path, confirm=True).status == "error"
    assert not is_applied(proposal, tmp_path)


@pytest.mark.parametrize(
    "path",
    ["pom.xml", "config/.env", ".github/workflows/ci.py", "node_modules/x/index.js", "notes.md"],
)
def test_only_test_sources_can_be_edited(tmp_path: Path, path: str) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("a\n", encoding="utf-8")
    proposal = parse_proposal(f"РЕШЕНИЕ: исправить\nФАЙЛ: {path}:1\nБЫЛО:\na\nСТАЛО:\nb\nПОЧЕМУ: x")
    errors = validate_proposal(proposal, tmp_path)
    assert errors and "не относится к коду автотестов" in errors[0]


def test_proposal_parser_handles_bold_ticks_and_spaces(tmp_path: Path) -> None:
    text = (
        "**РЕШЕНИЕ:** исправить\n"
        "**ФАЙЛ:** `src/My Tests/OrderTest.java:6` — локатор\n"
        "**БЫЛО:** `page.click(\"#a\");`\n"
        "**СТАЛО:**\n```java\npage.click(\"#b\");\nx = a ** b;\n```\n"
        "**ПОЧЕМУ:** локатор устарел\n"
    )
    proposal = parse_proposal(text)
    assert proposal.file == "src/My Tests/OrderTest.java" and proposal.line == 6
    assert proposal.before == ['page.click("#a");']
    assert proposal.after == ['page.click("#b");', "x = a ** b;"]  # «**» в коде не потерян
    assert proposal.why == "локатор устарел"


def test_inline_before_after_in_single_backticks_keep_the_code(tmp_path: Path) -> None:
    """``БЫЛО: `код` `` без жирного: апострофы — оформление, а не часть кода."""
    (tmp_path / "T.java").write_text("a\n    page.click();\nb\n", encoding="utf-8")
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:2\n"
        "БЫЛО: `    page.click();`\nСТАЛО: `    page.waitUntilReady(); page.click();`\n"
        "ПОЧЕМУ: нет ожидания\n"
    )
    assert proposal.before == ["    page.click();"]
    assert proposal.after == ["    page.waitUntilReady(); page.click();"]
    assert validate_proposal(proposal, tmp_path) == []


def test_fence_opened_on_the_header_line_is_not_code() -> None:
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:2\nБЫЛО: ```java\nx();\n```\n"
        "СТАЛО: ```java\ny();\n```\nПОЧЕМУ: z\n"
    )
    assert proposal.before == ["x();"] and proposal.after == ["y();"]


@pytest.mark.parametrize("before_header", ["`БЫЛО:`", "**`БЫЛО:`**", "### `БЫЛО:`", "БЫЛО:"])
def test_decorated_header_lines_add_no_code_lines(before_header: str) -> None:
    """Заголовок в обратных кавычках на своей строке: закрывающая «`» — не строка кода."""
    after_header = before_header.replace("БЫЛО", "СТАЛО")
    proposal = parse_proposal(
        f"РЕШЕНИЕ: исправить\nФАЙЛ: T.java:2\n{before_header}\n        x();\n"
        f"{after_header}\n        y();\nПОЧЕМУ: z\n"
    )
    assert proposal.before == ["        x();"] and proposal.after == ["        y();"]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # код в обратных кавычках: отступы сохраняются
        ("`БЫЛО:` `    x();`", "    x();"),
        ("**БЫЛО:** `    x();`", "    x();"),
        ("- **`БЫЛО:`** `    x();`", "    x();"),
        ("### `БЫЛО:` `    x();`", "    x();"),
        # маркер списка («*», «-») — не оформление заголовка и закрытия не требует
        ("* `БЫЛО:` `    x();`", "    x();"),
        ("*   `БЫЛО:` `    x();`", "    x();"),
        ("- `БЫЛО:` `    x();`", "    x();"),
        ("* **БЫЛО:** `    x();`", "    x();"),
        ("+ `БЫЛО:` `    x();`", "    x();"),
        ("• `БЫЛО:` `    x();`", "    x();"),
        ("+ **БЫЛО:** `    x();`", "    x();"),
        ("• БЫЛО: x();", "x();"),
        ("+ БЫЛО: x();", "x();"),
        ("* `БЫЛО:` x();", "x();"),
        ("* *БЫЛО:* x();", "x();"),
        ("*БЫЛО:* x();", "x();"),
        ("`БЫЛО`: `    x();`", "    x();"),
        ("БЫЛО: `    x();`", "    x();"),
        # обычный код на строке заголовка: ведущие пробелы после «:» не сохраняются
        ("`БЫЛО:`    x();", "x();"),
        ("**БЫЛО:** x();", "x();"),
        ("**БЫЛО**: x();", "x();"),
        ("БЫЛО: x();", "x();"),
        # код, начинающийся со «*» или «_», разметкой не считается
        ("БЫЛО: *ptr = 1;", "*ptr = 1;"),
        ("**БЫЛО:** _tmp = 1;", "_tmp = 1;"),
    ],
)
def test_header_decoration_is_separated_from_code_on_the_same_line(header: str, expected: str) -> None:
    """Оформление заголовка («`», «**») не должно попадать в код БЫЛО/СТАЛО."""
    after_header = header.replace("БЫЛО", "СТАЛО").replace("x();", "y();").replace("= 1;", "= 2;")
    proposal = parse_proposal(
        f"РЕШЕНИЕ: исправить\nФАЙЛ: T.java:2\n{header}\n{after_header}\nПОЧЕМУ: z\n"
    )
    assert proposal.before == [expected]
    assert proposal.after == [expected.replace("x();", "y();").replace("= 1;", "= 2;")]


@pytest.mark.parametrize("bullet", ["-", "*", "+", "•"])
def test_every_header_may_carry_a_list_marker(bullet: str) -> None:
    """Все заголовки, а не только БЫЛО/СТАЛО, допускают маркер списка перед названием."""
    proposal = parse_proposal(
        f"{bullet} РЕШЕНИЕ: исправить\n{bullet} ФАЙЛ: `src/T.java:2`\n"
        f"{bullet} `БЫЛО:`\n    x();\n{bullet} `СТАЛО:`\n    y();\n{bullet} ПОЧЕМУ: z\n"
    )
    assert proposal.decision == "fix" and proposal.file == "src/T.java" and proposal.line == 2
    assert proposal.before == ["    x();"] and proposal.after == ["    y();"]
    assert proposal.why == "z"


@pytest.mark.parametrize("decision", ["исправить | не трогать", "не трогать | исправить"])
def test_copied_decision_template_is_not_a_decision(tmp_path: Path, decision: str) -> None:
    """Шаблон «исправить | не трогать», скопированный дословно, молча читался как «не трогать»."""
    proposal = parse_proposal(f"РЕШЕНИЕ: {decision}\nПОЧЕМУ: потому что\n")
    assert proposal.decision == "?"
    assert "«РЕШЕНИЕ:» должно быть «исправить» или «не трогать»" in validate_proposal(proposal, tmp_path)[0]


def test_applied_place_nearer_to_the_line_wins_over_a_neighbouring_before(tmp_path: Path) -> None:
    """СТАЛО уже стоит у указанной строки, а такое же БЫЛО — в 10 строках дальше: соседний участок не трогать."""
    body = (
        ["class T {"] + ["    // f"] * 8 + ["    void a() {"]
        + ["        page.waitUntilReady();", "        page.click();", "    }"]
        + ["    // f"] * 6 + ["    void b() {", "        page.click();", "    }", "}"]
    )
    target = tmp_path / "T.java"
    target.write_text("\n".join(body) + "\n", encoding="utf-8")
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:11\nБЫЛО:\n        page.click();\n"
        "СТАЛО:\n        page.waitUntilReady();\n        page.click();\nПОЧЕМУ: нет ожидания"
    )
    before = target.read_text(encoding="utf-8")

    assert is_applied(proposal, tmp_path)  # без отметки: по содержимому, ближайшее — СТАЛО
    result = apply_proposal(proposal, tmp_path, confirm=True)
    assert result.status == "applied" and not result.changed and "уже применена" in result.text
    assert target.read_text(encoding="utf-8") == before


def test_applied_mark_survives_unrelated_edits_and_old_records(tmp_path: Path) -> None:
    target = tmp_path / "T.java"
    target.write_text(THREE_CLICKS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(REMOVE_ONE_CLICK)
    assert _apply(proposal, tmp_path, files)[1].status == "applied"
    backup = files.backup.read_bytes()
    assert target.read_text(encoding="utf-8").count("page.click();") == 2

    # Другая правка того же файла меняет хэш. Оставшиеся два click() совпадают с БЫЛО, но правку
    # это не откатывает: повтор запрещён, бэкап не тронут.
    target.write_text(target.read_text(encoding="utf-8") + "// другая правка\n", encoding="utf-8")
    assert not is_applied(proposal, tmp_path, files)
    refused = apply_proposal(proposal, tmp_path, confirm=True, files=files)
    assert refused.status == "error" and not refused.changed
    assert target.read_text(encoding="utf-8").count("page.click();") == 2
    assert files.backup.read_bytes() == backup

    # Файл вернули к версии до правки (побайтно) — правка снова не применена.
    target.write_bytes(backup)
    assert applied_state(proposal, tmp_path, files) == "not_applied"

    # Отметка старого формата (только proposal/file/line) учитывается по СТАЛО у записанной строки.
    _apply(proposal, tmp_path, files)
    files.record.write_text(json.dumps({
        "proposal": json.loads(files.record.read_text(encoding="utf-8"))["proposal"],
        "file": "T.java", "line": 3,
    }), encoding="utf-8")
    target.write_text(target.read_text(encoding="utf-8") + "// ещё правка\n", encoding="utf-8")
    assert is_applied(proposal, tmp_path, files)
    assert apply_proposal(proposal, tmp_path, confirm=True, files=files).status == "applied"
    assert target.read_text(encoding="utf-8").count("page.click();") == 2


def test_revert_uses_the_file_from_the_apply_record(tmp_path: Path) -> None:
    """Предложение переписали (другой ФАЙЛ) — откат всё равно возвращает файл, который правил apply."""
    (tmp_path / "A.java").write_text("a();\nb();\n", encoding="utf-8")
    (tmp_path / "B.java").write_text("a();\nc();\n", encoding="utf-8")  # совпадёт с A после правки
    files = _files(tmp_path)
    for_a = parse_proposal("РЕШЕНИЕ: исправить\nФАЙЛ: A.java:2\nБЫЛО:\nb();\nСТАЛО:\nc();\nПОЧЕМУ: x")
    assert _apply(for_a, tmp_path, files)[1].status == "applied"
    assert (tmp_path / "A.java").read_text(encoding="utf-8") == "a();\nc();\n"

    # B.java не менялся и не должен пострадать, какое бы предложение сейчас ни лежало в папке.
    reverted = revert_proposal(tmp_path, files)
    assert reverted.status == "reverted" and "A.java" in reverted.text
    assert (tmp_path / "A.java").read_text(encoding="utf-8") == "a();\nb();\n"
    assert (tmp_path / "B.java").read_text(encoding="utf-8") == "a();\nc();\n"

    # Отметка старого формата: откатить нечем, файл не трогается.
    files.record.write_text(json.dumps({"proposal": "x", "file": "A.java", "line": 2}), encoding="utf-8")
    old = revert_proposal(tmp_path, files)
    assert old.status == "error" and "старого формата" in old.text


TWO_METHODS = (
    "class T {\n    void a() {\n        page.click();\n    }\n"
    + "    // filler\n" * 5
    + "    void b() {\n        page.waitUntilReady();\n        page.click();\n    }\n}\n"
)
WAIT_BEFORE_CLICK = (
    "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:3\nБЫЛО:\n        page.click();\n"
    "СТАЛО:\n        page.waitUntilReady();\n        page.click();\nПОЧЕМУ: нет ожидания"
)


def test_neighbouring_after_does_not_confirm_a_manually_reverted_place(tmp_path: Path) -> None:
    """Участок у строки 3 откатили вручную (другая правка в файле осталась), а такое же СТАЛО
    есть в соседнем методе: ни «применено», ни автоматического повтора; повтор — по --repeat."""
    target = tmp_path / "T.java"
    target.write_text(TWO_METHODS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(WAIT_BEFORE_CLICK)
    assert _apply(proposal, tmp_path, files)[1].changed
    assert target.read_text(encoding="utf-8").count("waitUntilReady") == 2

    reverted = target.read_text(encoding="utf-8").replace(
        "        page.waitUntilReady();\n        page.click();\n    }\n    // filler",
        "        page.click();\n    }\n    // filler", 1,
    )
    target.write_text(reverted + "// другая правка\n", encoding="utf-8")
    assert target.read_text(encoding="utf-8").count("waitUntilReady") == 1

    assert not is_applied(proposal, tmp_path, files)
    assert applied_state(proposal, tmp_path, files) == "unknown"
    assert apply_proposal(proposal, tmp_path, files=files).status == "error"

    shown = apply_proposal(proposal, tmp_path, files=files, repeat=True)
    assert shown.status == "diff" and "пользователь разрешил его флагом --repeat" in shown.text
    result = apply_proposal(
        proposal, tmp_path, confirm=True, diff_hash=shown.diff_hash, files=files, repeat=True
    )
    assert result.changed
    fixed = target.read_text(encoding="utf-8").split("\n")
    assert fixed[2].strip() == "page.waitUntilReady();" and fixed[3].strip() == "page.click();"
    assert target.read_text(encoding="utf-8").count("waitUntilReady") == 2
    assert target.read_text(encoding="utf-8").endswith("// другая правка\n")


def test_applied_mark_follows_the_place_when_lines_shift(tmp_path: Path) -> None:
    """Правка выше по файлу сдвинула номера строк — место узнаётся по окружению, а не по номеру."""
    target = tmp_path / "T.java"
    target.write_text(TWO_METHODS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(WAIT_BEFORE_CLICK)
    assert _apply(proposal, tmp_path, files)[1].changed

    shifted = target.read_text(encoding="utf-8").replace(
        "class T {\n", "// заголовок\n// ещё строка\n// и ещё\nclass T {\n", 1
    )
    target.write_text(shifted, encoding="utf-8")
    assert is_applied(proposal, tmp_path, files)
    again = apply_proposal(proposal, tmp_path, confirm=True, files=files)
    assert again.status == "applied" and not again.changed
    assert target.read_text(encoding="utf-8") == shifted


def test_identical_methods_are_confirmed_only_at_the_recorded_line(tmp_path: Path) -> None:
    """Два одинаковых метода: копия СТАЛО с тем же окружением не доказывает применение."""
    method = "    void {n}() {{\n        page.click();\n    }}\n"
    source = "class T {\n" + method.format(n="a") + "    // between\n" + method.format(n="a") + "}\n"
    target = tmp_path / "T.java"
    target.write_text(source, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:3\nБЫЛО:\n        page.click();\n"
        "СТАЛО:\n        page.waitUntilReady();\n        page.click();\nПОЧЕМУ: нет ожидания"
    )
    assert _apply(proposal, tmp_path, files)[1].changed
    # Второй метод правили так же руками, а участок у строки 3 откатили.
    both = target.read_text(encoding="utf-8")
    manual = both.replace("        page.waitUntilReady();\n", "", 1)  # снимает СТАЛО у строки 3
    manual = manual.replace(
        "    // between\n    void a() {\n        page.click();",
        "    // between\n    void a() {\n        page.waitUntilReady();\n        page.click();",
    ) + "// правка\n"
    target.write_text(manual, encoding="utf-8")
    assert not is_applied(proposal, tmp_path, files)


def test_old_format_mark_confirms_only_the_exact_recorded_line(tmp_path: Path) -> None:
    target = tmp_path / "T.java"
    target.write_text(TWO_METHODS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(WAIT_BEFORE_CLICK)
    assert _apply(proposal, tmp_path, files)[1].changed
    recorded = json.loads(files.record.read_text(encoding="utf-8"))
    files.record.write_text(json.dumps({
        "proposal": recorded["proposal"], "file": "T.java", "line": recorded["line"],
    }), encoding="utf-8")

    target.write_text(target.read_text(encoding="utf-8") + "// правка\n", encoding="utf-8")
    assert is_applied(proposal, tmp_path, files)  # СТАЛО ровно на записанной строке

    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "        page.waitUntilReady();\n        page.click();\n    }\n    // filler",
            "        page.click();\n    }\n    // filler", 1,
        ),
        encoding="utf-8",
    )
    assert not is_applied(proposal, tmp_path, files)  # соседнее СТАЛО в b() не в счёт


ASSERTED_CLICKS = (
    "class T {\n    void t() {\n        assertEquals(1, a());\n        page.click();\n"
    "        page.click();\n        page.click();\n        assertEquals(2, b());\n    }\n}\n"
)
REMOVE_DOUBLE_CLICK = (
    "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:4\nБЫЛО:\n        page.click();\n        page.click();\n"
    "СТАЛО:\n        page.click();\nПОЧЕМУ: двойной клик отправляет форму дважды"
)


@pytest.mark.parametrize(
    "edits",
    [
        [("assertEquals(2, b())", "assertEquals(9, b())")],  # соседняя строка ниже
        [("assertEquals(1, a())", "assertEquals(7, a())")],  # соседняя строка выше
        [("assertEquals(1, a())", "assertEquals(7, a())"), ("assertEquals(2, b())", "assertEquals(9, b())")],
        [("void t() {", "// заметка\n    void t() {")],  # строка вставлена вплотную к правке
    ],
)
def test_edits_next_to_a_removal_never_reopen_it(tmp_path: Path, edits: list[tuple[str, str]]) -> None:
    """Правка-удаление: оставшиеся два click() совпадают с БЫЛО. После чужих правок рядом состояние
    неизвестно (не «применено» по догадке), но повтора без явного разрешения нет."""
    target = tmp_path / "T.java"
    target.write_text(ASSERTED_CLICKS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(REMOVE_DOUBLE_CLICK)
    assert _apply(proposal, tmp_path, files)[1].changed
    backup = files.backup.read_bytes()

    for old, new in edits:
        target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    assert applied_state(proposal, tmp_path, files) == "unknown"
    again = apply_proposal(proposal, tmp_path, confirm=True, files=files)
    assert again.status == "error" and not again.changed
    assert target.read_text(encoding="utf-8").count("page.click();") == 2
    assert files.backup.read_bytes() == backup  # исходный бэкап не перезаписан


def test_added_action_after_a_removal_is_not_a_revert(tmp_path: Path) -> None:
    """Из трёх click() убран один; потом после оставшихся двух добавили focus() и click().

    От исходного файла это одна вставленная строка, от файла после apply — две, но откатом
    это не является: близость к исходнику не повод повторять правку и затирать бэкап.
    """
    target = tmp_path / "T.java"
    target.write_text(THREE_CLICKS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(REMOVE_ONE_CLICK)
    assert _apply(proposal, tmp_path, files)[1].changed
    backup = files.backup.read_bytes()

    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "        page.click();\n        page.click();\n    }",
            '        page.click();\n        page.click();\n        page.focus("#next");\n'
            "        page.click();\n    }",
        ),
        encoding="utf-8",
    )
    edited = target.read_text(encoding="utf-8")

    assert applied_state(proposal, tmp_path, files) == "unknown"
    for confirm in (False, True):
        result = apply_proposal(proposal, tmp_path, confirm=confirm, diff_hash="x", files=files)
        assert result.status == "error" and "Не удалось определить" in result.text
    assert target.read_text(encoding="utf-8") == edited and files.backup.read_bytes() == backup

    # Явное разрешение пользователя: показ diff с предупреждением, затем обычное подтверждение;
    # версия до первого apply остаётся в NN.orig.1.
    shown = apply_proposal(proposal, tmp_path, files=files, repeat=True)
    assert shown.status == "diff" and "может задвоить правку" in shown.text
    done = apply_proposal(proposal, tmp_path, confirm=True, diff_hash=shown.diff_hash, files=files, repeat=True)
    assert done.changed
    assert files.backup.with_name(files.backup.name + ".1").read_bytes() == backup
    assert files.backup.read_bytes() == edited.encode("utf-8")


@pytest.mark.parametrize(
    "edits",
    [
        [("assertEquals(200, api.status())", "assertEquals(202, api.status())")],  # другой тест ниже
        [("void a() {", "void a() { // комментарий")],
        [("class T {", "// шапка\nclass T {")],  # сдвиг всех номеров строк
    ],
)
def test_edits_elsewhere_keep_a_replacement_applied(tmp_path: Path, edits: list[tuple[str, str]]) -> None:
    """Правка-замена (БЫЛО и СТАЛО не пересекаются): чужие правки в других строках её не затрагивают."""
    target = tmp_path / "T.java"
    target.write_text(
        "class T {\n    void a() {\n        page.click(\"#old\");\n    }\n"
        "    void b() {\n        assertEquals(200, api.status());\n    }\n}\n",
        encoding="utf-8",
    )
    files = _files(tmp_path)
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:3\nБЫЛО:\n        page.click(\"#old\");\n"
        "СТАЛО:\n        page.click(\"#new\");\nПОЧЕМУ: локатор устарел"
    )
    assert _apply(proposal, tmp_path, files)[1].changed
    for old, new in edits:
        target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    assert applied_state(proposal, tmp_path, files) == "applied"
    again = apply_proposal(proposal, tmp_path, confirm=True, files=files)
    assert again.status == "applied" and not again.changed


def _copies_of_one_block() -> str:
    block = ["// x1", "// x2", "// x3", "page.click();", "// y1", "// y2", "// y3"]
    return "\n".join(["class T {", *block, "// sep", *block, "}"]) + "\n"


def test_identical_copy_elsewhere_does_not_confirm_a_reverted_place(tmp_path: Path) -> None:
    """Два участка с одинаковыми тремя строками до и после: откатили первый, у второго такое же СТАЛО."""
    target = tmp_path / "T.java"
    target.write_text(_copies_of_one_block(), encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(
        "РЕШЕНИЕ: исправить\nФАЙЛ: T.java:5\nБЫЛО:\npage.click();\n"
        "СТАЛО:\npage.wait();\npage.click();\nПОЧЕМУ: нет ожидания"
    )
    assert _apply(proposal, tmp_path, files)[1].changed

    text = target.read_text(encoding="utf-8").replace("page.wait();\npage.click();", "page.click();", 1)
    text = text.replace(
        "// sep\n// x1\n// x2\n// x3\npage.click();",
        "// sep\n// x1\n// x2\n// x3\npage.wait();\npage.click();",
    )
    target.write_text(text + "// другая правка\n", encoding="utf-8")

    # Копия СТАЛО в другом методе применение первого участка не подтверждает; автоматически
    # повторять нельзя, а по явной просьбе (--repeat) правится именно первый участок.
    assert applied_state(proposal, tmp_path, files) == "unknown"
    assert apply_proposal(proposal, tmp_path, files=files).status == "error"
    shown = apply_proposal(proposal, tmp_path, files=files, repeat=True)
    result = apply_proposal(
        proposal, tmp_path, confirm=True, diff_hash=shown.diff_hash, files=files, repeat=True
    )
    assert result.changed
    fixed = target.read_text(encoding="utf-8").split("\n")
    assert fixed[4:6] == ["page.wait();", "page.click();"]  # снова исправлен первый участок
    assert target.read_text(encoding="utf-8").count("page.wait();") == 2


def test_customised_fix_is_unknown_and_is_never_repeated(tmp_path: Path) -> None:
    """Строку правки изменили иначе, чем откатом: неизвестно, стоит ли она; повтор запрещён."""
    target = tmp_path / "T.java"
    target.write_text(TWO_METHODS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(WAIT_BEFORE_CLICK)
    assert _apply(proposal, tmp_path, files)[1].changed
    backup = files.backup.read_bytes()

    customised = target.read_text(encoding="utf-8").replace(
        "        page.waitUntilReady();\n        page.click();\n    }\n    // filler",
        "        page.waitForLongLoad();\n        page.click();\n    }\n    // filler", 1,
    )
    target.write_text(customised, encoding="utf-8")

    assert applied_state(proposal, tmp_path, files) == "unknown"
    assert not is_applied(proposal, tmp_path, files)
    for confirm in (False, True):
        result = apply_proposal(proposal, tmp_path, confirm=confirm, diff_hash="x", files=files)
        assert result.status == "error" and "Не удалось определить" in result.text
    assert target.read_text(encoding="utf-8") == customised and files.backup.read_bytes() == backup

    # Явное разрешение пользователя (--repeat) — единственный путь повторить правку.
    assert apply_proposal(proposal, tmp_path, files=files, repeat=True).status == "diff"


def test_unreadable_snapshot_falls_back_to_the_recorded_line(tmp_path: Path) -> None:
    target = tmp_path / "T.java"
    target.write_text(TWO_METHODS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(WAIT_BEFORE_CLICK)
    assert _apply(proposal, tmp_path, files)[1].changed
    target.write_text(target.read_text(encoding="utf-8") + "// правка\n", encoding="utf-8")
    files.backup.write_bytes(b"\xff\xfe not the original")  # копия повреждена или подменена

    # Без надёжной копии верим только СТАЛО ровно на записанной строке.
    assert applied_state(proposal, tmp_path, files) == "applied"
    target.write_text(target.read_text(encoding="utf-8").replace("        page.waitUntilReady();\n", "", 1), encoding="utf-8")
    assert applied_state(proposal, tmp_path, files) == "unknown"


def test_repeated_applies_never_overwrite_earlier_backups(tmp_path: Path) -> None:
    """Три применения подряд (два повтора): каждая версия до apply лежит в своём файле, первая — в NN.orig.1."""
    target = tmp_path / "T.java"
    target.write_text(TWO_METHODS, encoding="utf-8")
    files = _files(tmp_path)
    proposal = parse_proposal(WAIT_BEFORE_CLICK)

    before_each: list[bytes] = []
    for round_number in range(3):
        repeat = round_number > 0
        if repeat:
            assert applied_state(proposal, tmp_path, files) == "unknown"
        before_each.append(target.read_bytes())
        shown = apply_proposal(proposal, tmp_path, files=files, repeat=repeat)
        assert shown.status == "diff"
        assert apply_proposal(
            proposal, tmp_path, confirm=True, diff_hash=shown.diff_hash, files=files, repeat=repeat
        ).changed
        # Строку правки меняют вручную: состояние неизвестно, дальнейший повтор — только с --repeat.
        lines = target.read_text(encoding="utf-8").split("\n")
        index = next(i for i, line in enumerate(lines) if "page.waitUntilReady();" in line)
        lines[index] = f"        page.waitCustom{round_number}();"
        target.write_text("\n".join(lines), encoding="utf-8")

    folder = files.backup.parent
    assert files.backup.with_name(files.backup.name + ".1").read_bytes() == before_each[0]  # самая первая
    assert files.backup.with_name(files.backup.name + ".2").read_bytes() == before_each[1]
    assert files.backup.read_bytes() == before_each[2]  # последняя — её вернёт revert
    assert sorted(path.name for path in folder.glob("01.orig*")) == ["01.orig", "01.orig.1", "01.orig.2"]
    assert len({before_each[0], before_each[1], before_each[2]}) == 3  # версии действительно разные
