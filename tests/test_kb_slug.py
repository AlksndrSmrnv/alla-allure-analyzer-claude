"""Tests for shared KB slug generation."""

from __future__ import annotations

import hashlib

import pytest

from alla.knowledge.slug import make_kb_slug


def _suffix(material: str) -> str:
    return hashlib.sha256(material.encode()).hexdigest()[:8]


_STEP_LOGIN_SUBMIT = "same error\n---\nlogin \u2192 submit"
_STEP_LOGIN_SUBMIT_CONFIRM = "same error\n---\nlogin \u2192 submit \u2192 confirm"
_STEP_DATES = "same error\n---\nbuild <ts> \u2192 submit"


@pytest.mark.parametrize(
    ("title", "error_example", "step_path", "expected"),
    [
        (
            "Connection timeout",
            "socket.timeout: 30s",
            "Login -> Submit",
            "connection_timeout_241f9620",
        ),
        ("Gateway timeout!", "gateway 504", None, f"gateway_timeout_{_suffix('gateway 504')}"),
        (" Gateway   Timeout ", "gateway 504", "", f"gateway_timeout_{_suffix('gateway 504')}"),
        ("", "empty title", None, f"kb_entry_{_suffix('empty title')}"),
        ("!!!", "punct title", None, f"kb_entry_{_suffix('punct title')}"),
        ("Ошибка подключения", "unicode title", None, f"kb_entry_{_suffix('unicode title')}"),
        ("HTTP 500 on /api/v1/order", "boom", None, f"http_500_on_api_v1_order_{_suffix('boom')}"),
        (
            "A" * 80,
            "long title",
            None,
            f"{'a' * 50}_{_suffix('long title')}",
        ),
        (
            "Step aware",
            "same error",
            "Login -> Submit",
            f"step_aware_{_suffix(_STEP_LOGIN_SUBMIT)}",
        ),
        (
            "Step aware",
            "same error",
            "  Login   ->   Submit  ",
            f"step_aware_{_suffix(_STEP_LOGIN_SUBMIT)}",
        ),
        (
            "Step aware",
            "same error",
            "Login \u2192 Submit",
            f"step_aware_{_suffix(_STEP_LOGIN_SUBMIT)}",
        ),
        (
            "Step aware",
            "same error",
            "Login -> Submit -> Confirm",
            f"step_aware_{_suffix(_STEP_LOGIN_SUBMIT_CONFIRM)}",
        ),
        (
            "Dates in step",
            "same error",
            "Build 2026-02-10 -> Submit",
            f"dates_in_step_{_suffix(_STEP_DATES)}",
        ),
        (
            "Numbers",
            "Order 123456 failed",
            None,
            f"numbers_{_suffix('Order 123456 failed')}",
        ),
        (
            "Canonical input",
            "Order <NUM> failed",
            None,
            f"canonical_input_{_suffix('Order <NUM> failed')}",
        ),
        (
            "Spaces and CAPS",
            "caps",
            None,
            f"spaces_and_caps_{_suffix('caps')}",
        ),
    ],
)
def test_make_kb_slug_is_stable(
    title: str,
    error_example: str,
    step_path: str | None,
    expected: str,
) -> None:
    assert make_kb_slug(title, error_example, step_path) == expected


def test_make_kb_slug_step_path_changes_signature_only() -> None:
    without_step = make_kb_slug("Connection timeout", "socket.timeout: 30s")
    with_step = make_kb_slug("Connection timeout", "socket.timeout: 30s", "Login -> Submit")

    assert without_step.startswith("connection_timeout_")
    assert with_step.startswith("connection_timeout_")
    assert without_step != with_step
