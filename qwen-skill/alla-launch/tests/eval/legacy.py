"""Фикстура старого разбора ``tests/fixtures/legacy_run_v1``: восстановление в проект.

Папку создала версия скилла до изменений первой очереди точности (см. README фикстуры);
абсолютные пути в ней заменены метками ``@PROJECT_ROOT@``, ``@SKILL_DIR@``,
``@SKILL_ENTRY@``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_run_v1"


def restore_legacy_run(project_root: Path, skill_dir: Path, entrypoint: Path) -> Path:
    """Скопировать разбор в ``<project_root>/alla-reports/`` с настоящими путями."""
    source = next(path for path in FIXTURE.iterdir() if path.is_dir())
    target = project_root / "alla-reports" / source.name
    shutil.copytree(source, target)
    marks = {
        "@SKILL_ENTRY@": str(entrypoint),
        "@SKILL_DIR@": str(skill_dir),
        "@PROJECT_ROOT@": str(project_root),
    }
    for path in target.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            for mark, value in marks.items():
                text = text.replace(mark, value)
            path.write_text(text, encoding="utf-8")
    return target
