"""События текстового лога: границы, уровни, склейка и совместимость со старым извлечением."""

from __future__ import annotations

import re

import pytest
import skill_fixtures  # noqa: F401  # scripts/ в sys.path

from alla_core.services.log_extraction_service import _detect_content_type, _extract_error_blocks
from alla_core.utils.log_events import error_events, parse_events
from eval import corpus_dev, corpus_holdout

FORMAT_LOGS = {
    "spring-boot": (corpus_dev.SPRING_BOOT_LOG, "merchant terminal T-77 is blocked"),
    "logback": (corpus_dev.LOGBACK_LOG, "Price list PL-77 is not published"),
    "log4j2": (corpus_dev.LOG4J2_LOG, 'unique constraint "uk_profile_email"'),
    "jul": (corpus_dev.JUL_LOG, "554 5.7.1 Relay access denied"),
    "python-root": (corpus_dev.PYTHON_ROOT_LOG, "template 'monthly.xlsx' not found"),
    "python-format": (corpus_dev.PYTHON_FORMAT_LOG, "index_not_found_exception"),
    "python-traceback": (corpus_dev.PYTHON_TRACEBACK_LOG, "TypeError: unsupported operand"),
    "logfmt": (corpus_dev.LOGFMT_LOG, "reservation already exists"),
    "nginx": (corpus_dev.NGINX_LOG, "connect() failed (111: Connection refused)"),
    "current": (corpus_dev.CURRENT_FORMAT_LOG, "customer is null"),
    "bare-exception": (corpus_dev.BARE_EXCEPTION_LOG, "No space left on device"),
    "celery": (corpus_holdout.CELERY_LOG, "KeyError: 'zone_id'"),
    "syslog": (corpus_holdout.SYSLOG_LOG, "redis connection lost"),
    "pipe-level": (corpus_holdout.PIPE_LEVEL_LOG, "has no rate for tier PLATINUM"),
}


@pytest.mark.parametrize("name", FORMAT_LOGS)
def test_each_format_yields_its_error_and_no_info(name: str) -> None:
    log, evidence = FORMAT_LOGS[name]

    errors = list(error_events(log))

    assert len(errors) == 1, [event.lines for event in errors]
    assert evidence in errors[0].text
    assert not re.search(r"\binfo\b|\[notice\]|request finished", errors[0].text, re.IGNORECASE)


@pytest.mark.parametrize("log", [log for log, _ in FORMAT_LOGS.values()])
def test_every_line_belongs_to_exactly_one_event(log: str) -> None:
    events = parse_events(log)

    assert [line for event in events for line in event.lines] == log.splitlines()
    assert events[0].first_line == 1
    for previous, event in zip(events, events[1:]):
        assert event.first_line == previous.last_line + 1


def test_neighbouring_info_is_not_swallowed_by_the_stack() -> None:
    event, = error_events(corpus_dev.TRAP_INFO_AFTER_STACK_LOG)

    assert (event.first_line, event.last_line) == (2, 4)
    assert "health check OK" not in event.text


@pytest.mark.parametrize("line", [
    "2026-10-03 10:00:00 [INFO] Audit: user typed ERROR in search field",
    "2026-10-03 10:00:00,110 INFO  [main] c.e.imp.ImportJob: Processed 0 errors",
    "2026-10-03 10:00:00,100 INFO  [main] c.e.web.ErrorController: ErrorController registered",
    "2026-10-03 10:00:00 [WARN] Retrying after error",
    "2026-10-03 10:00:00,123 WARN [main] c.e.Svc: fallback ERROR budget is 3",
    "10:00:00.123 [main] some error happened in worker",
    'time=2026-10-03T10:00:00Z level=warn msg="error budget low"',
    'time=2026-10-03T10:00:00Z msg="got level=error in payload" level=info',
])
def test_error_words_outside_the_level_position_are_not_errors(line: str) -> None:
    assert not list(error_events(line + "\n"))


@pytest.mark.parametrize("line", [
    "2026-10-03 10:00:00 [Error] mixed case in brackets",
    "2026-10-03 10:00:00 FATAL [main] c.e.Svc: down",
    "2026-10-03 10:00:00.130 | CRITICAL | svc | down",
    "[2026-10-04 08:20:01,300: ERROR/ForkPoolWorker-2] Task failed",
    "03.10.2026 10:00:00 ERROR svc down",
    "2026/10/03 10:00:00 [crit] 1#0: disk failure",
    'ts=2026-10-03T10:00:00Z level=fatal msg="down"',
    "CRITICAL:payments:gateway down",
    'time=2026-10-03T10:00:00Z msg="expected level=info in payload" level=error',
    'time=2026-10-03T10:00:00Z level="ERROR" msg="quoted level"',
])
def test_error_levels_in_the_level_position(line: str) -> None:
    assert len(list(error_events(line + "\n"))) == 1


def test_jul_header_and_severe_line_are_one_event() -> None:
    event, = error_events(corpus_dev.JUL_LOG)

    assert event.level == "SEVERE"
    assert event.lines[0].startswith("Oct 03, 2026 10:00:01 AM")
    assert event.lines[1].startswith("SEVERE: SMTP server rejected")
    assert "MessagingException" in event.lines[2]


def test_traceback_ends_at_the_exception_line() -> None:
    log = (
        "Traceback (most recent call last):\n"
        '  File "/app/a.py", line 3, in f\n'
        "    g()\n"
        "ValueError: bad value\n"
        "plain line after the traceback\n"
    )
    event, = error_events(log)

    assert event.kind == "traceback"
    assert event.lines[-1] == "ValueError: bad value"


def test_error_with_traceback_stays_one_event() -> None:
    log = (
        "2026-10-03 10:00:00,100 - svc - ERROR - request failed\n"
        "Traceback (most recent call last):\n"
        '  File "/app/a.py", line 3, in f\n'
        "KeyError: 'id'\n"
        "2026-10-03 10:00:00,200 - svc - INFO - next\n"
    )
    event, = error_events(log)

    assert event.kind == "level" and (event.first_line, event.last_line) == (1, 4)


def test_exception_starts_an_event_only_before_frames() -> None:
    with_frames = "2026-10-03 10:00:00 [INFO] start\njava.io.IOException: disk\n\tat a.B.c(B.java:1)\n"
    without = "2026-10-03 10:00:00 [INFO] start\njava.io.IOException: disk\nnext line\n"

    assert [event.kind for event in error_events(with_frames)] == ["exception"]
    assert not list(error_events(without))


# ---------------------------------------------------------------------------
# Совместимость: лог вида «<ISO-время> [ERROR] …» извлекается побайтово как раньше
# ---------------------------------------------------------------------------

_OLD_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_OLD_ERROR_RE = re.compile(r"\[error\]", re.IGNORECASE)


def _old_extract_error_blocks(log_text: str) -> str:
    """Извлечение до log_events (коммит 47215cf) — эталон совместимости."""
    blocks: list[str] = []
    current: list[str] = []
    in_block = False
    for line in log_text.splitlines():
        is_new = bool(_OLD_START_RE.match(line))
        is_error = bool(_OLD_ERROR_RE.search(line))
        if is_error and is_new:
            if current:
                blocks.append("\n".join(current))
            current = [line]
            in_block = True
        elif in_block:
            if is_new and not is_error:
                blocks.append("\n".join(current))
                current = []
                in_block = False
            else:
                current.append(line)
    if current:
        blocks.append("\n".join(current))
    return "\n\n".join(blocks)


OLD_FORMAT_LOGS = [
    corpus_dev.CURRENT_FORMAT_LOG,
    corpus_dev.HIKARI_LOG,
    corpus_dev.DISCOUNT_NPE_LOG,
    corpus_dev.SLOW_QUERY_LOG,
    corpus_dev.AUTH_DOWN_LOG,
    corpus_dev.TRAP_ERROR_WORD_LOG,
    corpus_dev.TRAP_INFO_AFTER_STACK_LOG,
    "2026-02-09 10:23:45,123 [ERROR] Exception occurred\n"
    "    at com.example.Main.run(Main.java:10)\n"
    "Caused by: java.io.IOException: Connection refused\n"
    "\t... 3 more\n"
    "\n"
    "[ERROR] continuation without time\n"
    "2026-02-09 10:23:46,200 [WARN] Recovery attempted\n",
    "2026-02-09 10:23:45,123 [ERROR] Error one\n2026-02-09 10:23:45,124 [ERROR] Error two\n"
    "    stacktrace line\n2026-02-09 10:23:46,200 [INFO] Done",
    "2026-02-09T10:23:45.123Z [error] ISO\r\n    at some.Class.method(File.java:1)\r\n"
    "2026-02-09T10:23:46.200Z [INFO] Done\r\n",
    "preamble without time\n2026-02-09 10:23:45 [ERROR] last\n    at x.Y.z(Y.java:9)",
    "",
]


@pytest.mark.parametrize("log", OLD_FORMAT_LOGS)
def test_old_format_extraction_is_byte_identical(log: str) -> None:
    assert _extract_error_blocks(log).encode() == _old_extract_error_blocks(log).encode()


@pytest.mark.parametrize(("content", "expected"), [
    (b"[2026-10-04 08:20:00,001: INFO/MainProcess] Task received\n", "text"),
    (b"[main] INFO started\n", "text"),
    (b'[{"level": "error"}]', "json"),
    (b"[1, 2, 3]", "json"),
    (b'  ["a", "b"]', "json"),
    (b"[]", "json"),
    (b'{"a": 1}', "json"),
])
def test_bracketed_text_log_is_not_mistaken_for_json(content: bytes, expected: str) -> None:
    assert _detect_content_type(content, fallback_mime="text/plain") == expected
    assert _detect_content_type(content) == expected
