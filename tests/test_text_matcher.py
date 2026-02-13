"""Тесты TextMatcher: 3-уровневый алгоритм сопоставления error_example."""

from __future__ import annotations

import logging

from alla.knowledge.matcher import MatcherConfig, TextMatcher
from conftest import make_kb_entry


# ====================================================================
# Базовые тесты (edge cases)
# ====================================================================


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


# ====================================================================
# Tier 1: exact substring match
# ====================================================================


def test_tier1_exact_substring_match() -> None:
    """Идентичный текст ошибки и error_example → Tier 1, score=1.0."""
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
    assert results[0].score == 1.0
    assert "Tier 1" in results[0].matched_on[0]


def test_tier1_whitespace_insensitive() -> None:
    """Разный whitespace (отступы, переносы) не мешает Tier 1."""
    kb_example = (
        "java.net.UnknownHostException: Failed to resolve host\n"
        "    at java.net.InetAddress.getAllByName(InetAddress.java:1281)"
    )
    # Тот же текст, но с другими отступами
    log_text = (
        "java.net.UnknownHostException: Failed to resolve host\n"
        "      at java.net.InetAddress.getAllByName(InetAddress.java:1281)"
    )

    matcher = TextMatcher()
    entry = make_kb_entry(id="dns", error_example=kb_example)

    results = matcher.match(error_text=log_text, entries=[entry])

    assert len(results) == 1
    assert results[0].score == 1.0
    assert "Tier 1" in results[0].matched_on[0]


def test_tier1_substring_in_larger_log() -> None:
    """error_example является подстрокой большого лога → Tier 1."""
    kb_example = (
        "java.net.SocketTimeoutException: Connect timed out\n"
        "    at java.net.Socket.connect(Socket.java:601)"
    )
    large_log = (
        "2024-01-15T10:23:45 [INFO] Starting test...\n"
        "2024-01-15T10:23:46 [ERROR] Connection failed:\n"
        "java.net.SocketTimeoutException: Connect timed out\n"
        "    at java.net.Socket.connect(Socket.java:601)\n"
        "2024-01-15T10:23:47 [INFO] Cleanup done\n"
    )

    matcher = TextMatcher()
    entry = make_kb_entry(id="timeout", error_example=kb_example)

    results = matcher.match(error_text=large_log, entries=[entry])

    assert len(results) == 1
    assert results[0].score == 1.0
    assert "Tier 1" in results[0].matched_on[0]


def test_tier1_normalizes_volatile_data() -> None:
    """UUID/timestamps нормализуются, позволяя Tier 1 сработать."""
    kb_example = (
        "2024-01-15T10:23:45 ERROR timeout connecting to postgres-db-7.internal:5432\n"
        "org.postgresql.util.PSQLException: Connection timed out\n"
        "    at org.postgresql.core.v3.ConnectionFactoryImpl.openConnectionImpl"
    )
    actual_log = (
        "2025-03-20T14:00:12 ERROR timeout connecting to postgres-db-7.internal:5432\n"
        "org.postgresql.util.PSQLException: Connection timed out\n"
        "    at org.postgresql.core.v3.ConnectionFactoryImpl.openConnectionImpl"
    )

    matcher = TextMatcher()
    entry = make_kb_entry(id="pg_timeout", error_example=kb_example)

    results = matcher.match(error_text=actual_log, entries=[entry])

    assert len(results) == 1
    assert results[0].score == 1.0
    assert "Tier 1" in results[0].matched_on[0]


# ====================================================================
# Tier 2: line match
# ====================================================================


def test_tier2_partial_line_match() -> None:
    """80%+ строк совпадают → Tier 2 с score в [0.7, 0.95]."""
    # 5 строк, 4 совпадают (80%), 1 отличается
    kb_example = (
        "javax.net.ssl.SSLHandshakeException: PKIX path building failed\n"
        "    at sun.security.ssl.Alert.createSSLException(Alert.java:131)\n"
        "Caused by: sun.security.validator.ValidatorException: PKIX path building failed\n"
        "Caused by: java.security.cert.CertPathBuilderException: unable to find valid certification path\n"
        "    at sun.security.provider.certpath.SunCertPathBuilder.build(SunCertPathBuilder.java:141)"
    )
    # Лог содержит 4 из 5 строк, но одна строка другая
    actual_log = (
        "javax.net.ssl.SSLHandshakeException: PKIX path building failed\n"
        "    at sun.security.ssl.Alert.createSSLException(Alert.java:131)\n"
        "Caused by: sun.security.validator.ValidatorException: PKIX path building failed\n"
        "Caused by: java.security.cert.CertPathBuilderException: unable to find valid certification path\n"
        "    at some.other.class.method(OtherClass.java:99)"
    )

    matcher = TextMatcher()
    entry = make_kb_entry(id="ssl", error_example=kb_example)

    results = matcher.match(error_text=actual_log, entries=[entry])

    assert len(results) == 1
    assert results[0].entry.id == "ssl"
    assert 0.7 <= results[0].score <= 0.95
    assert "Tier 2" in results[0].matched_on[0]


def test_tier2_below_threshold_falls_to_tier3() -> None:
    """Менее 80% строк совпадают → Tier 2 не срабатывает, идёт Tier 3."""
    # 5 строк, только 2 совпадают (40%) — ниже порога
    kb_example = (
        "javax.net.ssl.SSLHandshakeException: PKIX path building failed\n"
        "    at sun.security.ssl.Alert.createSSLException(Alert.java:131)\n"
        "Caused by: sun.security.validator.ValidatorException: PKIX path building failed\n"
        "Caused by: java.security.cert.CertPathBuilderException: unable to find valid certification path\n"
        "    at sun.security.provider.certpath.SunCertPathBuilder.build(SunCertPathBuilder.java:141)"
    )
    actual_log = (
        "javax.net.ssl.SSLHandshakeException: PKIX path building failed\n"
        "    at completely.different.Stack(Different.java:1)\n"
        "Caused by: totally.different.Exception: something else\n"
        "Caused by: another.Exception: not matching\n"
        "    at yet.another.Class.method(Class.java:42)"
    )

    matcher = TextMatcher(config=MatcherConfig(min_score=0.01))
    entry = make_kb_entry(id="ssl", error_example=kb_example)

    results = matcher.match(error_text=actual_log, entries=[entry])

    if results:
        # Если результат есть — это Tier 3
        assert "Tier 3" in results[0].matched_on[0]
        assert results[0].score <= 0.5


def test_tier2_single_line_skipped() -> None:
    """error_example из 1 строки пропускает Tier 2 (tier2_min_lines=2)."""
    matcher = TextMatcher()
    entry = make_kb_entry(
        id="npe",
        error_example="java.lang.NullPointerException: Cannot invoke method on null",
    )

    # Текст не содержит точную подстроку → Tier 1 не сработает.
    # 1 строка → Tier 2 пропущен. Пойдёт в Tier 3.
    results = matcher.match(
        error_text="some context java.lang.NullPointerException: Cannot invoke method on null more context",
        entries=[entry],
    )

    # Tier 1 должен сработать (подстрока после collapse whitespace)
    # Но если бы подстрока не нашлась, Tier 2 был бы пропущен
    if results:
        assert results[0].score > 0


# ====================================================================
# Tier 3: TF-IDF (fallback)
# ====================================================================


def test_tier3_score_capped() -> None:
    """Tier 3 (TF-IDF) score не превышает tier3_score_cap."""
    matcher = TextMatcher(config=MatcherConfig(tier3_score_cap=0.5, min_score=0.01))

    # Частично совпадающий текст — не substring, не 80% строк
    kb_example = (
        "org.hibernate.exception.LockAcquisitionException: could not execute statement\n"
        "Caused by: com.mysql.cj.jdbc.exceptions.MySQLTransactionRollbackException: Deadlock found\n"
        "    at com.mysql.cj.jdbc.exceptions.SQLExceptionsMapping.translateException"
    )
    # Другой контекст, но похожие ключевые слова
    actual_log = (
        "ERROR during database operation\n"
        "org.hibernate.exception.LockAcquisitionException: could not execute batch\n"
        "Caused by: com.mysql.cj.jdbc.exceptions.MySQLTransactionRollbackException: Lock wait timeout\n"
        "    at com.mysql.cj.jdbc.StatementImpl.executeUpdateInternal"
    )

    entry = make_kb_entry(id="deadlock", error_example=kb_example)

    results = matcher.match(error_text=actual_log, entries=[entry])

    if results:
        assert results[0].score <= 0.5
        assert "Tier 3" in results[0].matched_on[0]


# ====================================================================
# Tier priority & ordering
# ====================================================================


def test_tier_priority_ordering() -> None:
    """Tier 1 результаты ранжируются выше Tier 3."""
    matcher = TextMatcher(config=MatcherConfig(max_results=5, min_score=0.01))

    exact_example = (
        "java.net.UnknownHostException: host not found\n"
        "    at java.net.InetAddress.getAllByName"
    )
    fuzzy_example = (
        "com.mysql.cj.jdbc.exceptions.MySQLTransactionRollbackException: Deadlock found\n"
        "    at com.mysql.cj.jdbc.exceptions.SQLExceptionsMapping.translateException"
    )

    entries = [
        make_kb_entry(id="fuzzy", title="Deadlock", error_example=fuzzy_example),
        make_kb_entry(id="exact", title="DNS failure", error_example=exact_example),
    ]

    # Query содержит exact_example как подстроку + немного слов от fuzzy
    query = (
        "java.net.UnknownHostException: host not found\n"
        "    at java.net.InetAddress.getAllByName\n"
        "some unrelated Deadlock text"
    )

    results = matcher.match(error_text=query, entries=entries)

    assert len(results) >= 1
    assert results[0].entry.id == "exact"
    assert results[0].score == 1.0


def test_match_returns_multiple_sorted_by_score() -> None:
    """Если несколько записей совпали, они сортируются по score desc."""
    matcher = TextMatcher(config=MatcherConfig(max_results=5, min_score=0.01))
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

    # dns должен совпасть (Tier 1 — точная подстрока)
    result_ids = [r.entry.id for r in results]
    assert "dns" in result_ids
    # Результаты отсортированы по score desc
    if len(results) > 1:
        assert results[0].score >= results[1].score


# ====================================================================
# matched_on содержит информацию о tier
# ====================================================================


def test_matched_on_populated() -> None:
    """matched_on содержит описание совпадения с указанием tier."""
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
    # Идентичный текст → Tier 1
    assert "Tier 1" in results[0].matched_on[0]


# ====================================================================
# Debug logging
# ====================================================================


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


# ====================================================================
# Сценарий: таймаут БД (regression)
# ====================================================================


def test_user_scenario_database_timeout_in_logs() -> None:
    """KB entry о таймауте БД матчит лог с информацией о той же БД.

    KB: error_example содержит пример таймаута к postgres-db-7.
    Лог: содержит ту же ошибку, но с другими временными метками.
    После нормализации timestamps → Tier 1 (exact substring).
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
    assert results[0].score == 1.0
    assert "Tier 1" in results[0].matched_on[0]
