"""Построение стабильной сигнатуры проблемы для exact feedback memory."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from alla.knowledge.feedback_models import (
    FeedbackClusterContext,
    FeedbackIssueSignature,
)
from alla.models.clustering import FailureCluster
from alla.models.testops import FailedTestSummary
from alla.utils.log_utils import parse_log_sections
from alla.utils.text_normalization import normalize_text, normalize_text_for_llm

_SHORT_MESSAGE_WORDS = 10
_SHORT_MESSAGE_CHARS = 120
_MAX_LOG_ANCHOR_LINES = 6
_MAX_TRACE_ANCHOR_LINES = 4
_MAX_AUDIT_TEXT_CHARS = 2000
_MULTI_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_$.:-]*")
_ERROR_HINT_RE = re.compile(
    r"\b(?:ERROR|FATAL|SEVERE|CRITICAL)\b"
    r"|(?:Exception|Error|Traceback|Caused by)\b"
    r"|(?:FAILED|Failed to)\b",
    re.IGNORECASE,
)
_CAUSAL_HINT_RE = re.compile(
    r"(?:Caused by|Traceback)\b"
    r"|(?:\b[\w.$]+(?:Exception|Error)\b)",
    re.IGNORECASE,
)
_STACK_FRAME_RE = re.compile(
    r"^\s*(?:at\s+\S+\(|\.\.\.\s+\d+\s+more\b|File \".+\", line \d+)",
)
_GENERIC_LOG_WORDS = frozenset(
    {
        "error",
        "fatal",
        "severe",
        "critical",
        "traceback",
        "failed",
        "failure",
        "caused",
        "by",
        "requestid",
        "correlationid",
        "traceid",
        "spanid",
        "sessionid",
        "build",
        "job",
        "task",
        "thread",
        "worker",
        "process",
        "pid",
        "tid",
        "from",
        "for",
        "the",
        "and",
        "with",
        "while",
        "during",
    }
)


def _collapse_whitespace(text: str) -> str:
    return _MULTI_WS_RE.sub(" ", text).strip()


def _normalize_signature_soft_fragment(text: str) -> str:
    return _collapse_whitespace(normalize_text(text)).casefold()


def _normalize_signature_strict_fragment(text: str) -> str:
    return _collapse_whitespace(normalize_text_for_llm(text)).casefold()


def _normalize_audit_fragment(text: str) -> str:
    return _collapse_whitespace(normalize_text_for_llm(text))


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _has_informative_signal_words(text: str) -> bool:
    words = [
        word.casefold()
        for word in _WORD_RE.findall(_normalize_signature_soft_fragment(text))
    ]
    informative = [word for word in words if word not in _GENERIC_LOG_WORDS]
    return len(informative) >= 2


@dataclass(frozen=True)
class _Anchor:
    signature_text: str
    audit_text: str


@dataclass(frozen=True)
class _AnchorLine:
    raw_text: str
    strict_signature: bool


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


def _build_anchor(lines: list[_AnchorLine]) -> _Anchor:
    if not lines:
        return _Anchor(signature_text="", audit_text="")

    ordered_raw: list[str] = []
    strict_by_raw: dict[str, bool] = {}
    for line in lines:
        if not line.raw_text:
            continue
        if line.raw_text not in strict_by_raw:
            ordered_raw.append(line.raw_text)
            strict_by_raw[line.raw_text] = line.strict_signature
        else:
            strict_by_raw[line.raw_text] = strict_by_raw[line.raw_text] or line.strict_signature

    signature_lines: list[str] = []
    for raw_text in ordered_raw:
        normalize = (
            _normalize_signature_strict_fragment
            if strict_by_raw[raw_text]
            else _normalize_signature_soft_fragment
        )
        normalized = normalize(raw_text)
        if normalized:
            signature_lines.append(normalized)

    audit_lines = _dedupe([_normalize_audit_fragment(raw_text) for raw_text in ordered_raw])
    return _Anchor(
        signature_text="\n".join(sorted(_dedupe(signature_lines))),
        audit_text="\n".join(audit_lines),
    )


def _extract_log_anchor(log_snippet: str) -> _Anchor:
    if not log_snippet.strip():
        return _Anchor(signature_text="", audit_text="")

    cause_lines: list[_AnchorLine] = []
    matched_lines: list[_AnchorLine] = []
    fallback_lines: list[_AnchorLine] = []

    for _, body in parse_log_sections(log_snippet):
        for raw_line in body.splitlines():
            stripped = raw_line.strip()
            if not stripped or _STACK_FRAME_RE.match(stripped):
                continue
            if _CAUSAL_HINT_RE.search(stripped):
                cause_lines.append(_AnchorLine(raw_text=stripped, strict_signature=True))
            elif _ERROR_HINT_RE.search(stripped) and _has_informative_signal_words(stripped):
                matched_lines.append(_AnchorLine(raw_text=stripped, strict_signature=False))
            elif len(fallback_lines) < 3:
                fallback_lines.append(_AnchorLine(raw_text=stripped, strict_signature=False))

    if cause_lines or matched_lines:
        selected: list[_AnchorLine] = []
        if cause_lines:
            selected.append(cause_lines[0])
        if matched_lines:
            selected.append(matched_lines[0])

        remaining_slots = _MAX_LOG_ANCHOR_LINES - len(selected)
        if remaining_slots > 0:
            selected.extend(cause_lines[1 : 1 + remaining_slots])
            remaining_slots = _MAX_LOG_ANCHOR_LINES - len(selected)
        if remaining_slots > 0:
            selected.extend(matched_lines[1 : 1 + remaining_slots])
        return _build_anchor(selected)
    return _build_anchor(fallback_lines[:3])


def _extract_trace_anchor(trace: str) -> _Anchor:
    if not trace.strip():
        return _Anchor(signature_text="", audit_text="")

    lines = [line.strip() for line in trace.splitlines() if line.strip()]
    if not lines:
        return _Anchor(signature_text="", audit_text="")

    first_meaningful_line: _AnchorLine | None = None
    cause_lines: list[_AnchorLine] = []

    for raw_line in lines:
        if _STACK_FRAME_RE.match(raw_line):
            continue
        if first_meaningful_line is None:
            first_meaningful_line = _AnchorLine(
                raw_text=raw_line,
                strict_signature=False,
            )
        if _CAUSAL_HINT_RE.search(raw_line):
            cause_lines.append(_AnchorLine(raw_text=raw_line, strict_signature=True))

    if first_meaningful_line or cause_lines:
        selected: list[_AnchorLine] = []
        if first_meaningful_line is not None:
            selected.append(first_meaningful_line)
        remaining_slots = _MAX_TRACE_ANCHOR_LINES - len(selected)
        if remaining_slots > 0:
            selected.extend(cause_lines[:remaining_slots])
        return _build_anchor(selected)
    return _Anchor(signature_text="", audit_text="")


def _is_short_message(message: str) -> bool:
    words = message.split()
    return len(words) <= _SHORT_MESSAGE_WORDS and len(message) <= _SHORT_MESSAGE_CHARS


def build_feedback_cluster_context(
    cluster: FailureCluster,
    test_by_id: dict[int, FailedTestSummary],
) -> FeedbackClusterContext | None:
    """Построить exact-memory context для feedback по одному кластеру."""
    message_raw, trace_raw, log_raw = get_cluster_feedback_sources(cluster, test_by_id)

    message_soft = (
        _normalize_signature_soft_fragment(message_raw) if message_raw.strip() else ""
    )
    message_strict = (
        _normalize_signature_strict_fragment(message_raw) if message_raw.strip() else ""
    )
    message_audit = _normalize_audit_fragment(message_raw) if message_raw.strip() else ""
    trace_anchor = _extract_trace_anchor(trace_raw)
    log_anchor = _extract_log_anchor(log_raw)

    signature_parts: list[str]
    basis: str
    short_message = _is_short_message(message_strict) if message_strict else False
    message_with_anchor = message_strict if short_message else message_soft

    if message_strict:
        if trace_anchor.signature_text:
            basis = "message_trace_anchor"
            signature_parts = [message_with_anchor, trace_anchor.signature_text]
        elif log_anchor.signature_text:
            basis = "message_log_anchor"
            signature_parts = [message_with_anchor, log_anchor.signature_text]
        elif short_message:
            basis = "message_exact"
            signature_parts = [message_strict]
        else:
            basis = "message_only"
            signature_parts = [message_strict]
    elif trace_anchor.signature_text:
        basis = "trace_anchor"
        signature_parts = [trace_anchor.signature_text]
    elif log_anchor.signature_text:
        basis = "log_anchor"
        signature_parts = [log_anchor.signature_text]
    else:
        return None

    signature_material = "\n---\n".join(signature_parts)
    digest_source = f"v{FeedbackIssueSignature.DEFAULT_VERSION}\n{basis}\n{signature_material}"
    signature_hash = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()

    audit_parts: list[str] = []
    if message_audit:
        audit_parts.append(f"[message]\n{message_audit}")
    if trace_anchor.audit_text:
        audit_parts.append(f"[trace]\n{trace_anchor.audit_text}")
    if log_anchor.audit_text:
        audit_parts.append(f"[log]\n{log_anchor.audit_text}")
    audit_text = "\n\n".join(audit_parts).strip()[:_MAX_AUDIT_TEXT_CHARS]

    return FeedbackClusterContext(
        audit_text=audit_text,
        issue_signature=FeedbackIssueSignature(
            signature_hash=signature_hash,
            version=FeedbackIssueSignature.DEFAULT_VERSION,
            basis=basis,
        ),
    )
