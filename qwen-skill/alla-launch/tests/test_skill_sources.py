"""Реестр источников кластера: куски данных задания под id и откуда они."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fake_testops import FakeTestOps
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from test_skill_flow import _prepare

from alla_core.models.clustering import ClusterSignature, FailureCluster
from alla_core.models.testops import FailedTestSummary, LogAttachmentRef
from alla_core.services.prompt_builder_service import build_cluster_analysis_prompt
from alla_core.utils.log_focus import FOCUS_NOTE
from alla_skill_lib.sources import describe, registry

SNIPPET = (
    "--- [файл: app.log] ---\n"
    "[строки 2–4 · повторялось 3 раза: 2–4, 9–11, 15–17]\n"
    "2026-09-01 10:00:01 [ERROR] OrderService: failed\n"
    "java.lang.NullPointerException: customer is null\n"
    "\n"
    "[… пропущено блоков: 2, строк: 7 …]\n"
    "\n"
    "--- [HTTP: response.json] ---\n"
    "HTTP status: 500"
)


def _cluster(message: str | None = "expected: <200> but was: <500>") -> FailureCluster:
    return FailureCluster(cluster_id="c", label="x", signature=ClusterSignature(),
                          member_test_ids=[1, 2], member_count=2, representative_test_id=1,
                          example_message=message)


def test_every_piece_of_data_has_an_id_and_exact_text() -> None:
    prompt = build_cluster_analysis_prompt(
        _cluster(), SNIPPET, "java.lang.AssertionError: boom\n\tat a.B.c(B.java:1)",
        normalize_evidence=False, source_ids=True, message_test="createOrder", log_test="member")

    assert [(s.id, s.kind) for s in prompt.sources] == [
        ("S1", "message"), ("S2", "trace"), ("S3", "log"), ("S4", "log")]
    text = prompt.user_prompt
    assert "--- [S1 · сообщение об ошибке · тест createOrder] ---\nexpected: <200>" in text
    assert "--- [S2 · стек-трейс · тест createOrder] ---\njava.lang.AssertionError" in text
    assert ("--- [S3 · лог app.log · строки 2–4 · повторялось 3 раза: 2–4, 9–11, 15–17 · "
            "тест member] ---\n2026-09-01 10:00:01 [ERROR] OrderService: failed") in text
    assert "--- [S4 · HTTP response.json · тест member] ---\nHTTP status: 500" in text
    # Пометка пропуска — не источник, но остаётся в данных; пометка строк ушла в заголовок.
    assert "\n[… пропущено блоков: 2, строк: 7 …]\n" in text
    assert "\n[строки 2–4" not in text
    for source in prompt.sources:
        assert f"{source.header()}\n{source.text}" in text
    assert prompt.sources[2].text == (
        "2026-09-01 10:00:01 [ERROR] OrderService: failed\n"
        "java.lang.NullPointerException: customer is null")


def test_selection_notes_and_unmarked_logs() -> None:
    snippet = f"{FOCUS_NOTE}\n\nERROR plain log without sections"
    prompt = build_cluster_analysis_prompt(_cluster(None), snippet, source_ids=True,
                                           normalize_evidence=False)

    assert [(s.id, s.kind, s.attachment, s.lines) for s in prompt.sources] == [
        ("S1", "log", None, None)]
    assert f"\n{FOCUS_NOTE}\n" in prompt.user_prompt
    assert "--- [S1 · лог] ---\nERROR plain log without sections" in prompt.user_prompt


def test_without_source_ids_the_data_block_is_unchanged() -> None:
    prompt = build_cluster_analysis_prompt(_cluster(), SNIPPET, "trace")

    assert prompt.sources == ()
    assert "--- Сообщение об ошибке ---" in prompt.user_prompt
    assert "--- Фрагмент лога ---" in prompt.user_prompt


def test_registry_names_test_attachment_and_lines() -> None:
    prompt = build_cluster_analysis_prompt(
        _cluster(), SNIPPET, "trace", normalize_evidence=False, source_ids=True,
        message_test="createOrder", log_test="member")
    representative = FailedTestSummary(test_result_id=1, name="createOrder", status="failed")
    member = FailedTestSummary(test_result_id=2, name="member", status="failed",
                               log_attachments=[LogAttachmentRef(id=9001, name="app.log")])

    records = registry(prompt.sources, representative, member)

    assert records["S1"]["test_result_id"] == 1 and records["S1"]["text"].startswith("expected")
    assert records["S3"] == {
        "kind": "log", "test_result_id": 2, "test_name": "member", "section": "лог",
        "attachment": "app.log", "attachment_id": 9001,
        "lines": "строки 2–4 · повторялось 3 раза: 2–4, 9–11, 15–17",
        "text": prompt.sources[2].text,
    }
    assert records["S4"]["attachment_id"] is None  # вложение без секции ошибок в списке нет
    assert describe(records["S3"]) == "лог app.log, строки 2–4, тест member"
    assert describe(records["S1"]) == "сообщение об ошибке, тест createOrder"


def test_prepare_writes_a_registry_per_cluster(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]

    records = json.loads((run_dir / "evidence" / f"{order}.sources.json").read_text("utf-8"))
    task = (run_dir / "clusters" / f"{order}.md").read_text(encoding="utf-8")
    assert list(records) == ["S1", "S2", "S3"]
    assert records["S3"]["attachment_id"] == 9001 and records["S3"]["lines"] == "строки 2–4"
    for source_id, record in records.items():
        assert f"--- [{source_id} · " in task and record["text"] in task
    login_records = json.loads((run_dir / "evidence" / f"{login}.sources.json").read_text("utf-8"))
    assert [record["kind"] for record in login_records.values()] == ["message", "trace"]
    auto = next(entry["file_id"] for entry in run["clusters"] if entry["auto"])
    assert not (run_dir / "evidence" / f"{auto}.sources.json").exists()
