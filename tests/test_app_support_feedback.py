"""Тесты gate'а интерактивных блоков HTML-отчёта.

`html_report.py` рендерит «Создать решение для кластера» и like/dislike
только когда `feedback_api_url` непустой. Логика gate'а живёт в
`app_support.get_feedback_api_url`. Эти тесты фиксируют, при каких
комбинациях `kb_active` и `feedback_server_url` интерактив включается.
"""

from __future__ import annotations

from types import SimpleNamespace

from alla.app_support import get_feedback_api_url


def test_get_feedback_api_url_returns_empty_when_kb_inactive() -> None:
    settings = SimpleNamespace(kb_active=False, feedback_server_url="https://x")
    assert get_feedback_api_url(settings) == ""  # type: ignore[arg-type]


def test_get_feedback_api_url_returns_empty_when_url_missing() -> None:
    settings = SimpleNamespace(kb_active=True, feedback_server_url="")
    assert get_feedback_api_url(settings) == ""  # type: ignore[arg-type]


def test_get_feedback_api_url_returns_url_when_both_set() -> None:
    settings = SimpleNamespace(
        kb_active=True, feedback_server_url="http://127.0.0.1:8090"
    )
    assert (
        get_feedback_api_url(settings)  # type: ignore[arg-type]
        == "http://127.0.0.1:8090"
    )
