"""Построение стабильной сигнатуры проблемы для exact feedback memory."""

from __future__ import annotations

import hashlib
import re

from alla.knowledge.feedback_models import (
    FeedbackClusterContext,
    FeedbackIssueSignature,
)
from alla.models.clustering import FailureCluster
from alla.models.testops import FailedTestSummary
from alla.utils.log_utils import parse_log_sections
from alla.utils.text_normalization import normalize_text

_SHORT_MESSAGE_WORDS = 10
_SHORT_MESSAGE_CHARS = 120
_MAX_LOG_ANCHOR_LINES = 6
_MAX_TRACE_ANCHOR_LINES = 4
_MAX_AUDIT_TEXT_CHARS = 2000
_MULTI_WS_RE = re.compile(r"\s+")
_ERROR_HINT_RE = re.compile(
    r"\b(?:ERROR|FATAL|SEVERE|CRITICAL)\b"
    r"|(?:Exception|Error|Traceback|Caused by)\b"
    r"|(?:FAILED|Failed to)\b",
    re.IGNORECASE,
)
_STACK_FRAME_RE = re.compile(
    r"^\s*(?:at\s+\S+\(|\.\.\.\s+\d+\s+more\b|File \".+\", line \d+)",
)


def _collapse_whitespace(text: str) -> str:
    return _MULTI_WS_RE.sub(" ", text).strip()


def _normalize_fragment(text: str) -> str:
    return _collapse_whitespace(normalize_text(text))


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def get_cluster_feedback_sources(
    cluster: FailureCluster,
    test_by_id: dict[int, FailedTestSummary],
) -> tuple[str, str, str]:
    """Вернуть message, trace и log для representative кластера."""
    representative = (
        test_by_id.get(cluster.representative_test_id)
        if cluster.representative_test_id is not None
        else None
    )

    message = (
        representative.status_message
        if representative and representative.status_message
        else cluster.example_message
    ) or ""
    trace = (
        representative.status_trace
        if representative and representative.status_trace
        else cluster.example_trace_snippet
    ) or ""

    log_snippet = ""
    if representative and representative.log_snippet:
        log_snippet = representative.log_snippet.strip()
    if not log_snippet:
        for tid in cluster.member_test_ids:
            member = test_by_id.get(tid)
            if member and member.log_snippet and member.log_snippet.strip():
                log_snippet = member.log_snippet.strip()
                break

    return message, trace, log_snippet


def _extract_log_anchor(log_snippet: str) -> str:
    if not log_snippet.strip():
        return ""

    matched_lines: list[str] = []
    fallback_lines: list[str] = []

    for _, body in parse_log_sections(log_snippet):
        for raw_line in body.splitlines():
            stripped = raw_line.strip()
            if not stripped or _STACK_FRAME_RE.match(stripped):
                continue
            normalized = _normalize_fragment(stripped)
            if not normalized:
                continue
            if _ERROR_HINT_RE.search(stripped):
                matched_lines.append(normalized)
            elif len(fallback_lines) < 3:
                fallback_lines.append(normalized)

    lines = _dedupe(matched_lines or fallback_lines)
    return "\n".join(lines[:_MAX_LOG_ANCHOR_LINES])


def _extract_trace_anchor(trace: str) -> str:
    if not trace.strip():
        return ""

    lines = [line.strip() for line in trace.splitlines() if line.strip()]
    if not lines:
        return ""

    selected: list[str] = []
    selected.append(_normalize_fragment(lines[0]))

    for raw_line in lines[1:]:
        if _STACK_FRAME_RE.match(raw_line):
            continue
        if _ERROR_HINT_RE.search(raw_line):
            selected.append(_normalize_fragment(raw_line))

    cleaned = _dedupe(selected)
    return "\n".join(cleaned[:_MAX_TRACE_ANCHOR_LINES])


def _is_short_message(message: str) -> bool:
    words = message.split()
    return len(words) <= _SHORT_MESSAGE_WORDS and len(message) <= _SHORT_MESSAGE_CHARS


def build_feedback_cluster_context(
    cluster: FailureCluster,
    test_by_id: dict[int, FailedTestSummary],
) -> FeedbackClusterContext | None:
    """Построить exact-memory context для feedback по одному кластеру."""
    message_raw, trace_raw, log_raw = get_cluster_feedback_sources(cluster, test_by_id)

    message = _normalize_fragment(message_raw) if message_raw.strip() else ""
    trace_anchor = _extract_trace_anchor(trace_raw)
    log_anchor = _extract_log_anchor(log_raw)

    signature_parts: list[str]
    basis: str

    if message:
        if _is_short_message(message):
            basis = "message_exact"
            signature_parts = [message]
        elif log_anchor:
            basis = "message_log_anchor"
            signature_parts = [message, log_anchor]
        elif trace_anchor:
            basis = "message_trace_anchor"
            signature_parts = [message, trace_anchor]
        else:
            basis = "message_only"
            signature_parts = [message]
    elif trace_anchor and log_anchor:
        basis = "trace_log_anchor"
        signature_parts = [trace_anchor, log_anchor]
    elif trace_anchor:
        basis = "trace_anchor"
        signature_parts = [trace_anchor]
    elif log_anchor:
        basis = "log_anchor"
        signature_parts = [log_anchor]
    else:
        return None

    signature_material = "\n---\n".join(signature_parts)
    digest_source = f"v{FeedbackIssueSignature.DEFAULT_VERSION}\n{basis}\n{signature_material}"
    signature_hash = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()

    audit_parts: list[str] = []
    if message:
        audit_parts.append(f"[message]\n{message}")
    if trace_anchor:
        audit_parts.append(f"[trace]\n{trace_anchor}")
    if log_anchor:
        audit_parts.append(f"[log]\n{log_anchor}")
    audit_text = "\n\n".join(audit_parts).strip()[:_MAX_AUDIT_TEXT_CHARS]

    return FeedbackClusterContext(
        audit_text=audit_text,
        issue_signature=FeedbackIssueSignature(
            signature_hash=signature_hash,
            version=FeedbackIssueSignature.DEFAULT_VERSION,
            basis=basis,
        ),
    )
