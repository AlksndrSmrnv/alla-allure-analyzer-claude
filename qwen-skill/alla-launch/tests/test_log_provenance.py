"""Происхождение фрагментов лога, свёртка повторов и влияние на сигнатуру."""

from __future__ import annotations

import asyncio
import re

import pytest
from skill_fixtures import without_libmagic  # noqa: F401
from test_core_log_events import OLD_FORMAT_LOGS, _old_extract_error_blocks

from alla_core.knowledge.feedback_signature import (
    build_feedback_cluster_context,
    get_cluster_feedback_sources,
)
from alla_core.models.clustering import ClusterSignature, FailureCluster
from alla_core.models.testops import AttachmentMeta, FailedTestSummary
from alla_core.services.clustering_service import _build_log_document
from alla_core.services.log_extraction_service import (
    LogExtractionConfig,
    LogExtractionService,
    _extract_error_blocks,
)
from alla_core.utils.log_events import (
    SOURCE_MARK_RE,
    render_error_blocks,
    source_mark,
    strip_source_marks,
)
from alla_core.utils.log_focus import focus_log
from alla_core.utils.log_utils import parse_log_sections
from eval import corpus_dev

REPEATED = "".join(
    f"2026-10-03 10:00:{i:02d} [ERROR] OrderService: payment gateway timeout\n"
    f"java.net.SocketTimeoutException: Read timed out\n"
    "\tat ru.company.Gateway.call(Gateway.java:40)\n"
    f"2026-10-03 10:00:{i:02d} [INFO] retry scheduled\n"
    for i in range(8)
) + "2026-10-03 10:01:00 [ERROR] OrderService: giving up on order 5512\n"


class _Provider:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = list(files.items())

    async def get_attachments_for_test_result(self, _test_id: int) -> list[AttachmentMeta]:
        return [AttachmentMeta(id=100 + i, name=name, type="text/plain")
                for i, (name, _text) in enumerate(self.files)]

    async def get_attachment_content(self, attachment_id: int) -> bytes:
        return self.files[attachment_id - 100][1].encode()


def _enrich(files: dict[str, str], budget: int = 65536, **fields: str) -> FailedTestSummary:
    summary = FailedTestSummary(test_result_id=1, name="t", status="failed", **fields)
    service = LogExtractionService(_Provider(files), LogExtractionConfig(max_snippet_chars=budget))
    asyncio.run(service.enrich_with_logs([summary]))
    return summary


def _cluster() -> FailureCluster:
    return FailureCluster(cluster_id="c", label="x", signature=ClusterSignature(),
                          member_test_ids=[1], member_count=1, representative_test_id=1)


def _signature(summary: FailedTestSummary) -> tuple[str, str]:
    context = build_feedback_cluster_context(_cluster(), {1: summary})
    assert context is not None
    return context.base_issue_signature.signature_hash, context.audit_text


# ---------------------------------------------------------------------------
# Пометки строк и свёртка повторов
# ---------------------------------------------------------------------------


def test_each_block_starts_with_its_source_lines() -> None:
    summary = _enrich({"payment.log": corpus_dev.SPRING_BOOT_LOG, "app.log": corpus_dev.HIKARI_LOG})

    assert summary.log_snippet == (
        "--- [файл: payment.log] ---\n[строки 2–5]\n"
        + "\n".join(corpus_dev.SPRING_BOOT_LOG.splitlines()[1:5])
        + "\n\n--- [файл: app.log] ---\n[строки 2–5]\n"
        + "\n".join(corpus_dev.HIKARI_LOG.splitlines()[1:5])
    )
    assert [(ref.id, ref.name) for ref in summary.log_attachments] == [
        (100, "payment.log"), (101, "app.log")]
    assert "log_attachments" not in summary.model_dump()


def test_exact_repeats_fold_into_the_first_occurrence() -> None:
    blocks = render_error_blocks(REPEATED)

    first, last = blocks.split("\n\n")
    assert first.splitlines()[0] == (
        "[строки 1–3 · повторялось 8 раз: 1–3, 5–7, 9–11, 13–15, 17–19 и ещё 3]")
    assert first.count("Gateway.java:40") == 1 and blocks.count("Read timed out") == 1
    assert last.splitlines()[0] == "[строка 33]"


@pytest.mark.parametrize(("count", "word"), [(2, "раза"), (4, "раза"), (5, "раз"), (11, "раз"),
                                             (12, "раз"), (21, "раз"), (22, "раза")])
def test_repeat_count_is_spelled_in_russian(count: int, word: str) -> None:
    assert f"повторялось {count} {word}:" in source_mark([(i, i) for i in range(count)])


def test_distinct_errors_are_not_folded() -> None:
    log = ("2026-10-03 10:00:00 [ERROR] HTTP 500 from billing\n"
           "2026-10-03 10:00:01 [ERROR] HTTP 502 from billing\n")

    assert render_error_blocks(log).count("[строка") == 2


@pytest.mark.parametrize("log", OLD_FORMAT_LOGS)
def test_without_marks_blocks_are_the_old_extraction(log: str) -> None:
    if len({line for line in log.splitlines()}) != len(log.splitlines()):
        pytest.skip("в логе есть повторы")
    assert strip_source_marks(render_error_blocks(log)) == _extract_error_blocks(log)


def test_strip_keeps_application_lines_that_only_look_alike() -> None:
    text = "[строки 1–2]\nERROR boom\n[строки разбора: 3]\n[строка 7] и текст"

    assert strip_source_marks(text) == "ERROR boom\n[строки разбора: 3]\n[строка 7] и текст"


# ---------------------------------------------------------------------------
# Пометки переживают отбор и не попадают в признаки
# ---------------------------------------------------------------------------


def test_marks_survive_selection_and_prompt_focus() -> None:
    noise = "".join(f"2026-10-03 10:{i // 60:02d}:{i % 60:02d} [ERROR] Worker: heartbeat {i} "
                    + "x" * 120 + "\n" for i in range(300))
    summary = _enrich({"noise.log": noise, "app.log": corpus_dev.HIKARI_LOG}, budget=4000,
                      status_message="HikariPool connection is not available")

    assert summary.log_selection_truncated
    assert "[строки 2–5]\n2026-10-03 10:00:30 [ERROR] OrderRepository" in summary.log_snippet
    focused = focus_log(summary.log_snippet, "HikariPool connection", 1500,
                        log_selection_truncated=True)
    assert "[строки 2–5]\n2026-10-03 10:00:30 [ERROR] OrderRepository" in focused
    for _label, body in parse_log_sections(summary.log_snippet):
        for block in re.split(r"\n\s*\n", body):
            first = block.splitlines()[0]
            assert SOURCE_MARK_RE.fullmatch(first) or first.startswith(("[…", "[... обрезано"))


def test_marks_stay_out_of_signature_evidence_and_clustering() -> None:
    summary = _enrich({"app.log": REPEATED})
    stripped = summary.model_copy(update={"log_snippet": strip_source_marks(summary.log_snippet)})

    assert "[строк" in summary.log_snippet
    assert _signature(summary) == _signature(stripped)
    _message, _trace, evidence = get_cluster_feedback_sources(_cluster(), {1: summary})
    assert "[строк" not in evidence and "giving up on order" in evidence
    document = _build_log_document(summary, head_lines=50, tail_lines=50)
    assert document == _build_log_document(stripped, head_lines=50, tail_lines=50)
    assert "строк" not in document


# ---------------------------------------------------------------------------
# Сигнатура (v6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("log", [log for log in OLD_FORMAT_LOGS if "[ERROR]" in log.upper()])
def test_old_format_log_keeps_signature_material(log: str) -> None:
    """Лог «<ISO-время> [ERROR]» без повторов: тот же материал, что у прежнего извлечения."""
    new = _enrich({"app.log": log}, status_message="request failed")
    old = FailedTestSummary(test_result_id=1, name="t", status="failed",
                            status_message="request failed",
                            log_snippet=f"--- [файл: app.log] ---\n{_old_extract_error_blocks(log)}")

    assert _signature(new) == _signature(old)


def test_signature_does_not_depend_on_repeat_count() -> None:
    twice = _enrich({"app.log": "".join(REPEATED.splitlines(keepends=True)[:8])
                     + REPEATED.splitlines(keepends=True)[-1]})
    eight = _enrich({"app.log": REPEATED})

    assert _signature(twice) == _signature(eight)


@pytest.mark.parametrize(("log", "line"), [
    (corpus_dev.SPRING_BOOT_LOG, "merchant terminal T-77 is blocked"),
    (corpus_dev.LOGFMT_LOG, "reservation already exists"),
    (corpus_dev.NGINX_LOG, "connect() failed (111: Connection refused)"),
    (corpus_dev.JUL_LOG, "Relay access denied"),
])
def test_new_formats_sign_by_the_found_error(log: str, line: str) -> None:
    summary = _enrich({"app.log": log}, status_message="request failed")
    context = build_feedback_cluster_context(_cluster(), {1: summary})

    assert context is not None
    assert context.base_issue_signature.basis == "message_log_anchor"
    assert line in context.audit_text.split("[log]", 1)[1]
