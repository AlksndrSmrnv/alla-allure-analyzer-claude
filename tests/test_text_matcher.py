"""Тесты TextMatcher: подстрочное сопоставление error_pattern."""

from __future__ import annotations

import logging

from alla.knowledge.matcher import TextMatcher
from conftest import make_kb_entry


def test_match_empty_entries_returns_empty() -> None:
    """Пустой список entries даёт пустой результат."""
    matcher = TextMatcher()
    results = matcher.match(
        error_text="java.net.UnknownHostException: host not found",
        entries=[],
    )

    assert results == []


def test_match_blank_error_text_returns_empty() -> None:
    """Пустой текст ошибки не матчит ничего."""
    matcher = TextMatcher()
    results = matcher.match(
        error_text="   ",
        entries=[make_kb_entry(id="e1", error_pattern="UnknownHostException")],
    )

    assert results == []


def test_match_finds_entry_case_insensitive() -> None:
    """Матчинг по error_pattern регистронезависимый."""
    matcher = TextMatcher()
    entry = make_kb_entry(
        id="dns",
        title="DNS failure",
        error_pattern="UnknownHostException",
    )

    results = matcher.match(
        error_text="java.net.unknownhostexception: dns resolution failed",
        entries=[entry],
    )

    assert len(results) == 1
    assert results[0].entry.id == "dns"
    assert results[0].score == 1.0


def test_match_returns_multiple_matches() -> None:
    """Если в тексте есть несколько паттернов, возвращаются все совпадения."""
    matcher = TextMatcher()
    entries = [
        make_kb_entry(id="dns", error_pattern="UnknownHostException"),
        make_kb_entry(id="timeout", error_pattern="SocketTimeoutException"),
        make_kb_entry(id="irrelevant", error_pattern="DeadlockException"),
    ]

    results = matcher.match(
        error_text=(
            "java.net.UnknownHostException: host not found\n"
            "Caused by: java.net.SocketTimeoutException: read timed out"
        ),
        entries=entries,
    )

    result_ids = {r.entry.id for r in results}
    assert result_ids == {"dns", "timeout"}
    assert all(r.score == 1.0 for r in results)


def test_no_matches_log_contains_head_and_tail(caplog) -> None:
    """В debug-логе no-match видны и начало, и конец запроса."""
    matcher = TextMatcher()
    entries = [make_kb_entry(id="dns", error_pattern="UnknownHostException")]
    error_text = "ALLURE_HEAD " + ("x" * 400) + " LOG_TAIL RootCauseError"

    with caplog.at_level(logging.DEBUG):
        results = matcher.match(error_text, entries, query_label="cluster-1")

    assert results == []
    assert "KB: нет совпадений [cluster-1]" in caplog.text
    assert "ALLURE_HEAD" in caplog.text
    assert "LOG_TAIL RootCauseError" in caplog.text
