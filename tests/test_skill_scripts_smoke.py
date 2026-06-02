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


def test_manage_kb_does_not_define_local_slugify() -> None:
    import manage_kb

    assert not hasattr(manage_kb, "_slugify")


def test_serve_mask_dsn_strips_credentials() -> None:
    import serve

    # Нормальный DSN с user:pass@ — credentials должны исчезнуть из вывода.
    masked = serve._mask_dsn("postgresql://alla_user:secretpass@db.local:5432/kb")
    assert "secretpass" not in masked
    assert "alla_user" not in masked
    assert "db.local" in masked
    assert "5432" in masked
    assert "kb" in masked


def test_serve_mask_dsn_handles_edge_cases() -> None:
    import serve

    assert serve._mask_dsn("") == "<empty>"
    # DSN без хоста / db: не падать, не возвращать сырую строку.
    out = serve._mask_dsn("postgresql:///")
    assert "secret" not in out


def test_serve_propagate_env_loads_skill_env_into_environ(
    tmp_path, monkeypatch
) -> None:
    """`_propagate_env_to_server` кладёт переменные skill `.env` в os.environ.

    Без этого `alla.server._lifespan` зовёт `Settings()` от CWD-дефолта и
    может прочитать другой DSN/URL, чем `_common.load_settings`.
    """
    import serve

    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "ALLURE_KB_POSTGRES_DSN=postgresql://u:p@host/db\n"
        "ALLURE_FEEDBACK_SERVER_URL=http://127.0.0.1:8090\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(serve, "ENV_PATH", fake_env)
    monkeypatch.delenv("ALLURE_KB_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("ALLURE_FEEDBACK_SERVER_URL", raising=False)

    serve._propagate_env_to_server()

    import os

    assert os.environ["ALLURE_KB_POSTGRES_DSN"] == "postgresql://u:p@host/db"
    assert os.environ["ALLURE_FEEDBACK_SERVER_URL"] == "http://127.0.0.1:8090"


def test_serve_propagate_env_does_not_override_existing(
    tmp_path, monkeypatch
) -> None:
    """Уже выставленные env vars (shell export) имеют приоритет над `.env`."""
    import serve

    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "ALLURE_KB_POSTGRES_DSN=from-file\n", encoding="utf-8"
    )
    monkeypatch.setattr(serve, "ENV_PATH", fake_env)
    monkeypatch.setenv("ALLURE_KB_POSTGRES_DSN", "from-shell")

    serve._propagate_env_to_server()

    import os

    assert os.environ["ALLURE_KB_POSTGRES_DSN"] == "from-shell"


class _FakeAllaClient:
    """Контекст-менеджер-заглушка вместо AllaApiClient."""

    def __init__(self, response: dict) -> None:
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def generate_skill_report(self, run_id: int) -> dict:
        return self._response


def test_generate_report_hard_errors_when_nowhere_saved(monkeypatch) -> None:
    """Нет saved_to_db, нет --out/reports_dir, пустой report_url → EXIT_ERROR."""
    import generate_report

    settings = SimpleNamespace(kb_active=True, feedback_server_url="http://x", reports_dir="")
    monkeypatch.setattr(generate_report, "load_settings", lambda **k: settings)
    monkeypatch.setattr(
        generate_report,
        "build_alla_client",
        lambda s: _FakeAllaClient(
            {"html": "<html>", "report_filename": "r.html", "saved_to_db": False, "report_url": ""}
        ),
    )

    with pytest.raises(SystemExit) as exc:
        generate_report.main(["--run-id", "42"])
    assert exc.value.code == generate_report.EXIT_ERROR


def test_generate_report_ok_when_saved_to_db(monkeypatch, capsys) -> None:
    """saved_to_db=true — шаг успешен, без hard error."""
    import generate_report

    settings = SimpleNamespace(kb_active=True, feedback_server_url="http://x", reports_dir="")
    monkeypatch.setattr(generate_report, "load_settings", lambda **k: settings)
    monkeypatch.setattr(
        generate_report,
        "build_alla_client",
        lambda s: _FakeAllaClient(
            {
                "html": "<html>",
                "report_filename": "r.html",
                "saved_to_db": True,
                "report_url": "http://x/reports/r.html",
                "interactive_disabled_reasons": [],
            }
        ),
    )

    generate_report.main(["--run-id", "42"])
    out = capsys.readouterr().out
    assert '"saved_to_db": true' in out
    assert '"ok": true' in out
