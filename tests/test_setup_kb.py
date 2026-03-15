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


def test_setup_kb_dry_run_contains_feedback_column_migration(monkeypatch, capsys) -> None:
    """Dry-run схемы должен включать миграцию новых колонок kb_feedback."""
    module = _load_setup_kb_module()
    monkeypatch.setattr(sys, "argv", ["setup_kb.py", "--dry-run"])

    module.main()

    output = capsys.readouterr().out
    assert "ADD COLUMN IF NOT EXISTS issue_signature_hash TEXT" in output
    assert "ADD COLUMN IF NOT EXISTS issue_signature_version INTEGER NOT NULL DEFAULT 1" in output
    assert "ADD COLUMN IF NOT EXISTS issue_signature_payload JSONB" in output
