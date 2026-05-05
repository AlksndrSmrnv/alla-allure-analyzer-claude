"""Smoke-тесты лёгких функций скилл-скриптов.

Сами скрипты — тонкие orchestration-обёртки, и большая часть логики
живёт в `alla.services.*`. Здесь покрываем:

* `_interactive_disabled_reasons` из `generate_report.py` (gate
  интерактивных блоков HTML);
* `serve.py._build_parser()` (правильные дефолты host/port).

Запуск uvicorn / реальной БД из тестов не выполняется — только
логика, не требующая внешних ресурсов.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SKILL_SCRIPTS = (
    Path(__file__).resolve().parent.parent / "alla-skill" / "scripts"
)


@pytest.fixture(autouse=True)
def _add_skill_scripts_to_path() -> None:
    """Скилл-скрипты импортируют `_common` относительно своего каталога."""
    sys.path.insert(0, str(SKILL_SCRIPTS))
    yield
    try:
        sys.path.remove(str(SKILL_SCRIPTS))
    except ValueError:
        pass


def test_interactive_disabled_reasons_empty_when_feedback_url_set() -> None:
    import generate_report  # noqa: WPS433 — runtime import after sys.path tweak

    settings = SimpleNamespace(
        kb_active=True, feedback_server_url="http://127.0.0.1:8090"
    )
    assert generate_report._interactive_disabled_reasons(settings) == []


def test_interactive_disabled_reasons_flags_empty_url() -> None:
    import generate_report

    settings = SimpleNamespace(kb_active=True, feedback_server_url="")
    assert generate_report._interactive_disabled_reasons(settings) == [
        "feedback_server_url_empty"
    ]


def test_interactive_disabled_reasons_flags_inactive_kb() -> None:
    import generate_report

    settings = SimpleNamespace(kb_active=False, feedback_server_url="https://x")
    assert generate_report._interactive_disabled_reasons(settings) == ["kb_inactive"]


def test_serve_parser_defaults_to_loopback_and_8090() -> None:
    import serve

    args = serve._build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8090
    assert args.log_level == "info"


def test_serve_parser_accepts_overrides() -> None:
    import serve

    args = serve._build_parser().parse_args(
        ["--host", "0.0.0.0", "--port", "9000", "--log-level", "debug"]
    )
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.log_level == "debug"
