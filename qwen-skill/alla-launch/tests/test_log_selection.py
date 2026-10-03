"""Регрессии общего бюджета логов и временного контекста отбора."""

import asyncio
import json

import pytest

from skill_fixtures import without_libmagic  # noqa: F401
from alla_core.models.clustering import ClusterSignature, FailureCluster
from alla_core.models.testops import AttachmentMeta, FailedTestSummary, TriageReport
from alla_core.services.attachment_handlers import HandlerResult
from alla_core.services.log_extraction_service import LogExtractionConfig, LogExtractionService
from alla_skill_lib.kb import cluster_signature
from alla_skill_lib.report import load_models


class _Provider:
    def __init__(self, sections):
        self.sections = sections

    async def get_attachments_for_test_result(self, _test_id):
        return [AttachmentMeta(id=i + 1, name=f"source-{i}.log", type="text/plain")
                for i in range(len(self.sections))]

    async def get_attachment_content(self, attachment_id):
        return self.sections[attachment_id - 1].encode()


class _WholeText:
    name = "whole-text"
    priority = 1

    def handle(self, ctx):
        return HandlerResult(section=ctx.decoded_text or "", label="файл")


def _summary(**kwargs):
    return FailedTestSummary(test_result_id=1, name="failure", status="failed", **kwargs)


def _enrich(sections, budget=1000, **kwargs):
    summary = _summary(**kwargs)
    service = LogExtractionService(_Provider(sections), LogExtractionConfig(max_snippet_chars=budget),
                                   handlers=[_WholeText()])
    asyncio.run(service.enrich_with_logs([summary]))
    return summary


def _cluster():
    return FailureCluster(cluster_id="c1", label="failure", signature=ClusterSignature(),
                          representative_test_id=1, member_test_ids=[1], member_count=1,
                          example_message="request failed")


def test_late_related_error_replaces_earlier_noise_within_total_budget():
    summary = _enrich(["\n\n".join(f"heartbeat {i} " + "x" * 180 for i in range(30)),
                       "ERROR OrderService request 123456789 failed"],
                      status_message="OrderService 123456789")
    assert "OrderService request 123456789" in summary.log_snippet
    assert len(summary.log_snippet) <= 1000
    assert summary.log_selection_truncated is True
    assert summary.log_snippet.count("[... обрезано:") == 1


def test_short_sections_keep_exact_bytes_and_exclude_selection_fields():
    sections = ["backend pool exhausted\r\n", "raw [... обрезано: application data ...]\n"]
    summary = _enrich(sections, status_message="request failed", status_trace="trace line")
    expected = "\n\n".join(f"--- [файл: source-{i}.log] ---\n{text}"
                           for i, text in enumerate(sections))
    assert summary.log_snippet.encode() == expected.encode()
    assert summary.log_selection_truncated is False
    assert summary.log_selection_error == "request failed\ntrace line"
    assert not any(key.startswith("log_selection_") for key in summary.model_dump())


def test_first_overflow_rescores_all_previously_literal_sections():
    from alla_core.utils.log_focus import StreamingLogSelector
    selector = StreamingLogSelector("OrderService", 700)
    first = "ERROR OrderService early cause\n" + "x" * 250
    selector.add_section("--- [файл: first.log] ---", first)
    assert selector.render() == f"--- [файл: first.log] ---\n{first}"
    selector.add_section("--- [файл: later.log] ---", "heartbeat\n" + "y" * 700)
    assert selector.truncated
    assert "OrderService early cause" in selector.render()
    assert len(selector.render()) <= 700


def test_streaming_state_is_bounded_and_order_ties_stay_earliest():
    from alla_core.utils.log_focus import StreamingLogSelector
    selector = StreamingLogSelector("unrelated", 1000)
    for i in range(1000):
        selector.add_section(f"--- [файл: source-{i}.log] ---", f"heartbeat {i}\n" + "x" * 100)
        assert selector.retained_chars <= 1000
        assert all(len(block.text) <= 500 and not hasattr(block, "original") for block in selector._blocks.values())
    rendered = selector.render()
    assert "heartbeat 0" in rendered
    assert "heartbeat 999" not in rendered
    assert len(rendered) <= 1000


def test_core_http_sections_keep_their_headers_when_truncated():
    from alla_core.utils.log_focus import StreamingLogSelector
    from alla_core.utils.log_utils import parse_log_sections
    selector = StreamingLogSelector("request", 500)
    selector.add_section("--- [HTTP: response.json] ---", "request failed\n" + "x" * 3000)
    snippet = selector.render()
    assert snippet.startswith("--- [HTTP: response.json] ---\n")
    assert parse_log_sections(snippet, include_http=False) == []


def test_prompt_many_core_gaps_keeps_content_and_single_footer():
    from alla_core.utils.log_focus import focus_log
    footer = "[... обрезано: было 9000 символов, оставлено 5000 ...]"
    snippet = "\n\n".join(f"--- [файл: file-{i}.log] ---\n[…]\n\nERROR OrderService cause {i}\n\n[…]"
                           for i in range(30)) + "\n\n" + footer
    focused = focus_log(snippet, "OrderService", 450, log_selection_truncated=True)
    assert "OrderService cause" in focused
    assert focused.count(footer) == 1
    assert "[… пропущено блоков:" in focused or "[…]" not in focused
    assert len(focused) <= 450


@pytest.mark.parametrize("literal", [
    "[... обрезано: " + "noise " * 80 + "]",
    "[... обрезано: было 9000 символов, оставлено 5000 ...]",
])
def test_marker_like_untruncated_text_is_not_pinned(literal):
    from alla_core.utils.log_focus import focus_log
    focused = focus_log("--- [файл: app.log] ---\nheartbeat " + "x" * 2000
                        + f"\n\nERROR OrderService cause\n\n{literal}",
                        "OrderService", 200)
    assert "OrderService cause" in focused
    assert "[... обрезано:" not in focused


def test_generated_markers_do_not_change_feedback_anchor_but_raw_markers_do():
    body = "--- [файл: app.log] ---\nbackend pool exhausted"
    baseline = cluster_signature(_cluster(), {1: _summary(status_message="request failed", log_snippet=body)})
    marked = _summary(status_message="request failed", log_snippet=body + "\n\n[…]\n\n[... обрезано: было 9000 символов, оставлено 1000 ...]",
                      log_selection_truncated=True)
    assert cluster_signature(_cluster(), {1: marked}) == baseline
    raw = _summary(status_message="request failed", log_snippet=body + "\n[... обрезано: literal application data ...]")
    assert cluster_signature(_cluster(), {1: raw}) != baseline


def test_untruncated_full_model_json_roundtrip_preserves_v5_signature():
    summary = _enrich(["backend pool exhausted"], status_message="request failed")
    cluster = _cluster()
    triage = TriageReport(launch_id=1, total_results=1, failed_count=1, broken_count=0,
                          failed_tests=[summary])
    run = json.loads(json.dumps({"triage": triage.model_dump(mode="json")}))
    restored, _ = load_models(run)
    assert cluster_signature(cluster, {1: summary}) == cluster_signature(cluster, {1: restored.failed_tests[0]})
    assert not any(key.startswith("log_selection_") for key in run["triage"]["failed_tests"][0])


def test_selection_context_freezes_first_twenty_trace_lines_and_known_hint():
    class LateHint(_WholeText):
        def handle(self, ctx):
            return HandlerResult(section=ctx.decoded_text or "", label="файл", correlation_hint="late hint")

    trace = "\n".join(f"trace {i}" for i in range(21))
    summary = _summary(status_message="symptom", status_trace=trace, correlation_hint="known hint")
    service = LogExtractionService(_Provider(["short log"]), handlers=[LateHint()])
    asyncio.run(service.enrich_with_logs([summary]))
    assert summary.log_selection_error == "symptom\n" + "\n".join(trace.splitlines()[:20]) + "\nknown hint"
    assert "trace 20" not in summary.log_selection_error
    assert "late hint" not in summary.log_selection_error
    assert summary.correlation_hint == "late hint"


def test_first_correlation_hint_survives_eviction_and_empty_section():
    class EmptyHint(_WholeText):
        def handle(self, ctx):
            if ctx.att.id == 1:
                return HandlerResult(section="", label="HTTP", correlation_hint="first hint")
            return HandlerResult(section=ctx.decoded_text or "", label="файл", correlation_hint="later hint")

    summary = _summary(status_message="OrderService")
    service = LogExtractionService(_Provider(["empty", "heartbeat\n" + "x" * 3000,
                                             "ERROR OrderService cause"]),
                                   LogExtractionConfig(max_snippet_chars=500), handlers=[EmptyHint()])
    asyncio.run(service.enrich_with_logs([summary]))
    assert summary.correlation_hint == "first hint"
    assert "OrderService cause" in summary.log_snippet


def test_prompt_uses_actual_member_frozen_context_and_flag(monkeypatch):
    from alla_core.config import Settings
    from alla_skill_lib import cluster_task
    cluster = _cluster()
    cluster.member_test_ids = [1, 2]
    source = FailedTestSummary(test_result_id=2, name="member", status="failed", log_snippet="selected log",
                               log_selection_error="frozen member context", log_selection_truncated=True)
    seen = []

    def focus(snippet, error, budget, *, log_selection_truncated=False):
        seen.append((snippet, error, log_selection_truncated))
        return snippet

    monkeypatch.setattr(cluster_task, "focus_log", focus)
    cluster_task.build_cluster_task(cluster=cluster, position=1, total=1, launch_id=1,
                                   answer_path="/tmp/answer.md", next_command="next", tests_by_id={1: _summary(), 2: source},
                                   log_snippet="selected log", full_trace="representative trace", frames=[], hints=[],
                                   settings=Settings(endpoint="https://example.test", token="test"))
    assert seen == [("selected log", "frozen member context", True)]
    cluster_task.build_cluster_task(cluster=cluster, position=1, total=1, launch_id=1,
                                   answer_path="/tmp/answer.md", next_command="next", tests_by_id={1: _summary(), 2: source},
                                   log_snippet="custom log", full_trace="representative trace", frames=[], hints=[],
                                   settings=Settings())
    assert seen[-1] == ("custom log", "request failed\nrepresentative trace", False)


def test_streaming_no_headerless_fallback_when_source_metadata_exceeds_budget():
    from alla_core.utils.log_focus import StreamingLogSelector
    selector = StreamingLogSelector("OrderService", 500)
    selector.add_section("--- [HTTP: " + "x" * 600 + "] ---", "ERROR OrderService cause")
    assert selector.truncated is True
    assert selector.render() == ""


def test_streaming_accounting_matches_render_across_insertions_and_evictions():
    from alla_core.utils.log_focus import StreamingLogSelector
    selector = StreamingLogSelector("OrderService 123456789", 1000)
    for section in range(40):
        body = "\n\n".join(
            ("ERROR OrderService 123456789" if (section + block) % 5 == 0 else "heartbeat")
            + " x" * (5 + (section * 7 + block * 13) % 80)
            for block in range(15)
        )
        selector.add_section(f"--- [файл: source-{section}.log] ---", body)
        assert selector._render_size() == len(selector.render())
        assert len(selector.render()) <= 1000


def test_short_block_stream_has_no_per_candidate_full_render(monkeypatch):
    from alla_core.utils.log_focus import StreamingLogSelector
    selector = StreamingLogSelector("unrelated", 65536)
    renders = []
    original = selector._render_retained

    def render():
        renders.append(True)
        return original()

    monkeypatch.setattr(selector, "_render_retained", render)
    selector.add_section("--- [файл: tiny.log] ---", "\n\n".join("x" for _ in range(23000)))
    assert renders == []
    assert len(selector.render()) <= 65536
    assert len(renders) == 1


def test_huge_unrelated_first_line_does_not_displace_late_matching_line():
    summary = _enrich(["INFO payload=" + "x" * 9000 + "\nERROR OrderService cause"],
                      status_message="OrderService")
    assert "ERROR OrderService cause" in summary.log_snippet
    assert len(summary.log_snippet) <= 1000


@pytest.mark.parametrize("level", ["ERROR", "INFO"])
@pytest.mark.parametrize("context", ["", "\ncontext " + "y" * 22])
def test_huge_first_anchor_preserves_later_signal_line(level, context):
    from alla_core.utils.log_focus import StreamingLogSelector
    selector = StreamingLogSelector("OrderService", 1000)
    selector.add_section("--- [файл: app.log] ---", f"{level} OrderService payload=" + "x" * 9000
                         + context + "\nERROR OrderService root cause pool exhausted")
    result = selector.render()
    assert f"{level} OrderService payload=" in result
    assert "ERROR OrderService root cause pool exhausted" in result
    assert len(result) <= 1000


def test_only_last_extraction_footer_is_metadata_and_earlier_raw_footer_is_evidence():
    from alla_core.utils.log_focus import focus_log, strip_log_selection_metadata
    raw = "[... обрезано: было 42 символов, оставлено 24 ...]"
    generated = "[... обрезано: было 9000 символов, оставлено 5000 ...]"
    snippet = "--- [файл: app.log] ---\nbackend pool exhausted\n\n" + raw + "\n\n[…]\n\n" + generated
    stripped = strip_log_selection_metadata(snippet)
    assert raw in stripped
    assert generated not in stripped
    assert "[…]" not in stripped
    baseline = _summary(status_message="request failed", log_snippet="--- [файл: app.log] ---\nbackend pool exhausted\n\n" + raw)
    marked = _summary(status_message="request failed", log_snippet=snippet, log_selection_truncated=True)
    assert cluster_signature(_cluster(), {1: marked}) == cluster_signature(_cluster(), {1: baseline})
    focused = focus_log(snippet + "\n", "символов оставлено", 170, log_selection_truncated=True)
    assert raw in focused
    assert focused.count(generated) == 1


@pytest.mark.parametrize("budget", [100, 150])
def test_prompt_tiny_fallback_drops_footer_before_meaningful_content(budget):
    from alla_core.utils.log_focus import focus_log
    footer = "[... обрезано: было 9000 символов, оставлено 5000 ...]"
    snippet = "--- [файл: app.log] ---\nERROR OrderService cause " + "x" * 3000 + "\n\n" + footer
    result = focus_log(snippet, "OrderService", budget, log_selection_truncated=True)
    assert "OrderService cause" in result
    assert len(result) <= budget


@pytest.mark.parametrize("frames", [[], ["src/test/java/ru/company/OrderTest.java:6 — кадр стека"]])
def test_task_without_code_hints_says_code_not_found_and_forbids_searching(frames):
    # «Правила» велят начинать с раздела «Где искать код автотеста»; без него субагенты
    # искали код сами через ls/find (стенд Qwen, P04). Кадры стека упоминаются, только если
    # раздел есть, — иначе модель сообщала о противоречии.
    from alla_core.config import Settings
    from alla_skill_lib import cluster_task
    task = cluster_task.build_cluster_task(
        cluster=_cluster(), position=1, total=1, launch_id=1, answer_path="/tmp/answer.md",
        next_command="next", tests_by_id={1: _summary()}, log_snippet=None,
        full_trace=None, frames=frames, hints=[], settings=Settings())
    section = task.split("--- Где искать код автотеста (пути от корня проекта) ---\n")[1]
    assert section.startswith("- не найден:") and "Сам код не ищи" in section
    # подсказок нет и при нераспознанном или неоднозначном имени теста — исходник может быть
    assert "не удалось сопоставить" in section and "файлов с этими тестами" not in section
    if frames:
        assert "Кадры стека из кода проекта" in task.split("--- Где искать")[0]
        assert cluster_task.CODE_NOT_FOUND_WITH_FRAMES in section
    else:
        assert "Кадр" not in task.split("## Задание")[0].split("--- Где искать")[1]
        assert cluster_task.CODE_NOT_FOUND_NO_FRAMES in section
