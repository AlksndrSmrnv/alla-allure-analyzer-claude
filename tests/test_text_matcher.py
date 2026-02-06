"""Тесты двухэтапного алгоритма сопоставления TextMatcher (keyword + TF-IDF)."""

from __future__ import annotations

from alla.knowledge.matcher import MatcherConfig, TextMatcher
from alla.knowledge.models import KBEntryMatchCriteria
from conftest import make_kb_entry


# ---------------------------------------------------------------------------
# _keyword_match (этап 1 — детерминистический)
# ---------------------------------------------------------------------------

def test_keyword_match_exception_type_in_message() -> None:
    """exception_types в message даёт score > 0 и explanation в matched_on."""
    entry = make_kb_entry(
        match_criteria=KBEntryMatchCriteria(
            exception_types=["NullPointerException"],
            keywords=[], message_patterns=[], trace_patterns=[], categories=[],
        ),
    )
    matcher = TextMatcher()
    score, matched_on = matcher._keyword_match(
        message="java.lang.NullPointerException: value is null",
        trace=None,
        category=None,
        entry=entry,
    )

    assert score > 0
    assert any("exception_type" in m for m in matched_on)


def test_keyword_match_message_pattern() -> None:
    """message_patterns в message увеличивает score."""
    entry = make_kb_entry(
        match_criteria=KBEntryMatchCriteria(
            message_patterns=["Connection timed out"],
            keywords=[], exception_types=[], trace_patterns=[], categories=[],
        ),
    )
    matcher = TextMatcher()
    score, matched_on = matcher._keyword_match(
        message="Connection timed out after 30000ms",
        trace=None,
        category=None,
        entry=entry,
    )

    assert score > 0
    assert any("message_pattern" in m for m in matched_on)


def test_keyword_match_no_criteria_returns_zero() -> None:
    """Пустые criteria → score == 0."""
    entry = make_kb_entry()  # все criteria пустые по умолчанию
    matcher = TextMatcher()
    score, matched_on = matcher._keyword_match(
        message="some error",
        trace="some trace",
        category="Product defects",
        entry=entry,
    )

    assert score == 0.0
    assert matched_on == []


# ---------------------------------------------------------------------------
# match() — полный двухэтапный pipeline
# ---------------------------------------------------------------------------

def test_match_empty_entries_returns_empty() -> None:
    """Пустой список entries → пустой результат."""
    matcher = TextMatcher()
    results = matcher.match(
        query_message="some error",
        query_trace=None,
        query_category=None,
        entries=[],
    )

    assert results == []


def test_match_blank_query_returns_empty() -> None:
    """Все query-параметры None → пустой результат."""
    entry = make_kb_entry(id="e1")
    matcher = TextMatcher()
    results = matcher.match(
        query_message=None,
        query_trace=None,
        query_category=None,
        entries=[entry],
    )

    assert results == []


def test_match_blends_keyword_and_tfidf() -> None:
    """Полный pipeline: keyword + TF-IDF → score ∈ (0, 1], matched_on заполнен."""
    entry = make_kb_entry(
        id="npe",
        title="NullPointerException",
        description="Null pointer dereference in application code",
        match_criteria=KBEntryMatchCriteria(
            exception_types=["NullPointerException"],
            keywords=["null", "npe", "pointer"],
            message_patterns=["NullPointerException"],
            trace_patterns=[],
            categories=[],
        ),
    )
    matcher = TextMatcher()
    results = matcher.match(
        query_message="java.lang.NullPointerException: cannot invoke method on null",
        query_trace="at com.acme.UserService.getUser(UserService.java:42)",
        query_category=None,
        entries=[entry],
    )

    assert len(results) == 1
    assert 0 < results[0].score <= 1.0
    assert len(results[0].matched_on) > 0


def test_match_respects_min_score_and_max_results() -> None:
    """Фильтрация по min_score и ограничение max_results."""
    entries = [
        make_kb_entry(
            id="relevant",
            title="DNS Resolution Failure",
            description="DNS resolution failure in test environment",
            match_criteria=KBEntryMatchCriteria(
                exception_types=["UnknownHostException"],
                keywords=["dns", "resolve"],
                message_patterns=["UnknownHostException"],
                trace_patterns=[], categories=[],
            ),
        ),
        make_kb_entry(
            id="irrelevant",
            title="Database Deadlock",
            description="Database deadlock in transaction",
            match_criteria=KBEntryMatchCriteria(
                keywords=["deadlock", "database"],
                exception_types=["DeadlockException"],
                message_patterns=[], trace_patterns=[], categories=[],
            ),
        ),
        make_kb_entry(
            id="also_relevant",
            title="Connection Timeout",
            description="Connection timeout to external service",
            match_criteria=KBEntryMatchCriteria(
                keywords=["connection", "timeout", "dns"],
                exception_types=["UnknownHostException"],
                message_patterns=[], trace_patterns=[], categories=[],
            ),
        ),
    ]
    config = MatcherConfig(min_score=0.1, max_results=2)
    matcher = TextMatcher(config)
    results = matcher.match(
        query_message="java.net.UnknownHostException: dns resolution failed",
        query_trace=None,
        query_category=None,
        entries=entries,
    )

    assert len(results) <= 2
    assert all(r.score >= 0.1 for r in results)
