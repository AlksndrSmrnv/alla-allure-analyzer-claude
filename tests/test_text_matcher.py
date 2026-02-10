"""Тесты TextMatcher: TF-IDF cosine similarity сопоставление error_example."""

from __future__ import annotations

import logging

from alla.knowledge.matcher import MatcherConfig, TextMatcher
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
        entries=[make_kb_entry(id="e1", error_example="UnknownHostException: host not found")],
    )

    assert results == []


def test_match_high_similarity_when_same_text() -> None:
    """Идентичный текст ошибки и error_example даёт высокий score."""
    error_example = (
        "java.net.UnknownHostException: Failed to resolve 'api-gateway.staging.internal'\n"
        "    at java.net.InetAddress.getAllByName(InetAddress.java:1281)"
    )
    matcher = TextMatcher()
    entry = make_kb_entry(
        id="dns",
        title="DNS failure",
        error_example=error_example,
    )

    results = matcher.match(
        error_text=error_example,
        entries=[entry],
    )

    assert len(results) == 1
    assert results[0].entry.id == "dns"
    assert results[0].score > 0.5


def test_match_fuzzy_similar_errors() -> None:
    """Похожий, но не идентичный текст (разные даты/ID) всё равно матчится."""
    kb_example = (
        "2024-01-15T10:23:45 ERROR timeout connecting to postgres-db-7.internal:5432\n"
        "java.net.SocketTimeoutException: Connect timed out\n"
        "    at java.net.Socket.connect(Socket.java:601)"
    )
    actual_error = (
        "2025-03-20T14:00:12 ERROR timeout connecting to postgres-db-7.internal:5432\n"
        "java.net.SocketTimeoutException: Connect timed out\n"
        "    at java.net.Socket.connect(Socket.java:601)"
    )

    matcher = TextMatcher()
    entry = make_kb_entry(
        id="db_timeout",
        title="Таймаут подключения к БД",
        description="Timeout connecting to database server",
        error_example=kb_example,
    )

    results = matcher.match(
        error_text=actual_error,
        entries=[entry],
    )

    assert len(results) == 1
    assert results[0].entry.id == "db_timeout"
    assert results[0].score > 0.3


def test_match_returns_multiple_sorted_by_score() -> None:
    """Если несколько записей совпали, они сортируются по score desc."""
    matcher = TextMatcher()
    entries = [
        make_kb_entry(
            id="dns",
            title="DNS failure",
            error_example="java.net.UnknownHostException: host not found\n    at java.net.InetAddress.getAllByName",
        ),
        make_kb_entry(
            id="timeout",
            title="Connection timeout",
            error_example="java.net.SocketTimeoutException: read timed out\n    at java.net.Socket.connect",
        ),
        make_kb_entry(
            id="irrelevant",
            title="Deadlock",
            error_example="com.mysql.cj.jdbc.exceptions.MySQLTransactionRollbackException: Deadlock found",
        ),
    ]

    results = matcher.match(
        error_text=(
            "java.net.UnknownHostException: host not found\n"
            "    at java.net.InetAddress.getAllByName\n"
            "Caused by: java.net.SocketTimeoutException: read timed out"
        ),
        entries=entries,
    )

    # dns и timeout должны совпасть, irrelevant не должен (или с низким score)
    result_ids = [r.entry.id for r in results]
    assert "dns" in result_ids
    # Результаты отсортированы по score desc
    if len(results) > 1:
        assert results[0].score >= results[1].score


def test_irrelevant_text_below_min_score() -> None:
    """Совсем нерелевантный текст не попадает в результаты."""
    matcher = TextMatcher(config=MatcherConfig(min_score=0.15))
    entry = make_kb_entry(
        id="dns",
        title="DNS failure",
        error_example="java.net.UnknownHostException: Failed to resolve host\n    at java.net.InetAddress",
    )

    results = matcher.match(
        error_text="Login button color is incorrect on mobile viewport",
        entries=[entry],
    )

    assert results == []


def test_min_score_filtering() -> None:
    """min_score фильтрация отсекает слабые совпадения."""
    matcher = TextMatcher(config=MatcherConfig(min_score=0.99))
    entry = make_kb_entry(
        id="dns",
        title="DNS failure",
        error_example="java.net.UnknownHostException: host not found",
    )

    results = matcher.match(
        error_text="partially related UnknownHostException in different context",
        entries=[entry],
    )

    # С min_score=0.99 почти ничего не пройдёт
    assert len(results) == 0


def test_max_results_limit() -> None:
    """max_results ограничивает количество результатов."""
    matcher = TextMatcher(config=MatcherConfig(max_results=1, min_score=0.01))

    # Создаём 3 похожие записи
    entries = [
        make_kb_entry(
            id=f"entry_{i}",
            title=f"Error type {i}",
            error_example=f"java.lang.RuntimeException: test failure variant {i}\n    at com.company.Test.run",
        )
        for i in range(3)
    ]

    results = matcher.match(
        error_text="java.lang.RuntimeException: test failure variant\n    at com.company.Test.run",
        entries=entries,
    )

    assert len(results) <= 1


def test_matched_on_populated() -> None:
    """matched_on содержит описание совпадения."""
    matcher = TextMatcher()
    entry = make_kb_entry(
        id="dns",
        title="DNS failure",
        error_example="java.net.UnknownHostException: host not found\n    at java.net.InetAddress",
    )

    results = matcher.match(
        error_text="java.net.UnknownHostException: host not found\n    at java.net.InetAddress",
        entries=[entry],
    )

    assert len(results) == 1
    assert len(results[0].matched_on) > 0
    assert "TF-IDF" in results[0].matched_on[0]


def test_no_matches_log_contains_head_and_tail(caplog) -> None:
    """В debug-логе no-match видны и начало, и конец запроса."""
    matcher = TextMatcher()
    entries = [make_kb_entry(
        id="dns",
        error_example="completely unrelated pattern about network issues",
    )]
    error_text = "ALLURE_HEAD " + ("x" * 400) + " LOG_TAIL RootCauseError"

    with caplog.at_level(logging.DEBUG):
        results = matcher.match(error_text, entries, query_label="cluster-1")

    # Результаты могут быть пустыми (нерелевантный текст)
    # Главное — проверяем что логирование работает
    if not results:
        assert "KB: нет совпадений [cluster-1]" in caplog.text
        assert "ALLURE_HEAD" in caplog.text
        assert "LOG_TAIL RootCauseError" in caplog.text


def test_user_scenario_database_timeout_in_logs() -> None:
    """Главный сценарий: KB entry о таймауте БД матчит лог с информацией о той же БД.

    KB: error_example содержит пример таймаута к postgres-db-7.
    Лог: содержит ту же ошибку, но с другими временными метками.
    """
    kb_example = (
        "2024-05-10T08:12:33 [ERROR] Connection to postgres-db-7.internal:5432 "
        "timed out after 30000ms\n"
        "org.postgresql.util.PSQLException: Connection timed out\n"
        "    at org.postgresql.core.v3.ConnectionFactoryImpl.openConnectionImpl"
    )
    actual_log = (
        "2025-11-20T22:45:01 [ERROR] Connection to postgres-db-7.internal:5432 "
        "timed out after 30000ms\n"
        "org.postgresql.util.PSQLException: Connection timed out\n"
        "    at org.postgresql.core.v3.ConnectionFactoryImpl.openConnectionImpl"
    )

    matcher = TextMatcher()
    entry = make_kb_entry(
        id="postgres_timeout",
        title="Таймаут подключения к PostgreSQL",
        description="База данных postgres-db-7 не отвечает, нужна перезагрузка",
        error_example=kb_example,
    )

    results = matcher.match(error_text=actual_log, entries=[entry])

    assert len(results) == 1
    assert results[0].entry.id == "postgres_timeout"
    assert results[0].score > 0.3
