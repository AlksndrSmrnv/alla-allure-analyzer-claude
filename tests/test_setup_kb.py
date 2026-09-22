"""Тесты bootstrap-скрипта KB."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_setup_kb_module():
    path = Path(__file__).resolve().parents[1] / "sql" / "setup_kb.py"
    spec = importlib.util.spec_from_file_location("setup_kb_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_kb_defaults_to_schema_only_in_dry_run(monkeypatch, capsys) -> None:
    """Без флагов bootstrap не печатает starter pack в dry-run."""
    module = _load_setup_kb_module()
    monkeypatch.setattr(sys, "argv", ["setup_kb.py", "--dry-run"])

    module.main()

    output = capsys.readouterr().out
    assert "--- SCHEMA SQL ---" in output
    assert "--- STARTER PACK SQL ---" not in output


def test_setup_kb_with_starter_pack_prints_seed_in_dry_run(monkeypatch, capsys) -> None:
    """Флаг --with-starter-pack явно включает starter pack."""
    module = _load_setup_kb_module()
    monkeypatch.setattr(
        sys,
        "argv",
        ["setup_kb.py", "--dry-run", "--with-starter-pack"],
    )

    module.main()

    output = capsys.readouterr().out
    assert "--- STARTER PACK SQL ---" in output


def test_bootstrap_includes_retained_skill_api_schema(monkeypatch, capsys):
    module = _load_setup_kb_module()
    monkeypatch.setattr(sys, "argv", ["setup_kb.py", "--dry-run"])
    module.main()
    output = capsys.readouterr().out
    assert "CREATE TABLE IF NOT EXISTS alla.skill_run" in output
    assert "agent_analysis_json JSONB" in " ".join(output.split())
    assert "CREATE INDEX IF NOT EXISTS idx_skill_run_launch" in output
    assert "CREATE TRIGGER skill_run_updated_at" in output


def test_bootstrap_executes_skill_schema(monkeypatch):
    from unittest.mock import MagicMock
    import psycopg

    module = _load_setup_kb_module()
    connection = MagicMock()
    monkeypatch.setattr(psycopg, "connect", lambda dsn: connection)
    module.run("unused")
    sql = connection.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
    assert "CREATE TABLE IF NOT EXISTS alla.skill_run" in sql
    assert "CREATE TRIGGER skill_run_updated_at" in sql
    connection.close.assert_called_once()
