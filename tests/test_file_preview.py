"""Installation commands only print instructions, including on errors and repeat calls."""
from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest
import skill_fixtures  # noqa: F401
from test_skill_setup import _project, _run, _snapshot
from test_skill_extensions import make_demo

from alla_skill_lib import file_preview as fp

EXT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX interpreter wrapper")
@pytest.mark.parametrize("command", ["enable", "disable"])
@pytest.mark.parametrize("state", ["absent", "existing", "modified", "journal", "bad_config", "bad_manifest"])
def test_installation_cli_real_process_never_writes(tmp_path: Path, command: str, state: str) -> None:
    project, entry = _project(tmp_path)
    ext_dir = entry.parent.parent
    for folder in ("agents", "commands"):
        shutil.copytree(EXT_ROOT / folder, ext_dir / folder)
    make_demo(project)
    config = project / "alla-extensions/config.toml"
    config.write_text('enabled = ["demo"]\n' if command == "disable" else 'enabled = []\n')
    (project / ".venv/bin").mkdir(parents=True)
    python = project / ".venv/bin/python"
    python.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    python.chmod(0o755)
    agent = project / ".qwen/agents/alla-demo.md"
    if state in ("existing", "modified", "journal"):
        agent.parent.mkdir(parents=True)
        agent.write_text((project / "alla-extensions/demo/agent.md").read_text() +
                         ("engineer edit\n" if state == "modified" else ""))
    if state == "journal":
        (project / fp.JOURNAL).write_text('{"unfinished": true}\n')
    elif state == "bad_config":
        config.write_text('enabled = [\n')
    elif state == "bad_manifest":
        (project / "alla-extensions/demo/extension.toml").write_text('api = 99\n')
    before = _snapshot(tmp_path)
    args = ["extensions", "--project-root", str(project), command, "demo"]
    for _ in range(2):
        result = _run(entry, *args, cwd=project)
        assert _snapshot(tmp_path) == before, result.stdout + result.stderr
        assert result.stdout.startswith("STATUS:"), result.stderr
        if state == "journal":
            assert result.returncode == 1 and "автоматического восстановления нет" in result.stdout
        elif state == "bad_config":
            assert result.returncode == 1
        elif state == "bad_manifest" and command == "enable":
            assert result.returncode == 1
        else:
            assert result.returncode == 0, result.stdout + result.stderr
        if state in ("absent", "existing", "modified"):
            assert "enable demo --yes" not in result.stdout
            assert "Ничего не записано" in result.stdout or "Изменений нет" in result.stdout
    for flags in (["--yes"], ["--diff", "0" * 64], ["--yes", "--diff", "0" * 64]):
        result = _run(entry, *args, *flags, cwd=project)
        assert result.returncode != 0 and result.stdout.startswith("STATUS: error"), result.stdout
        assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("link", ["parent", "file", "broken"])
def test_preview_rejects_symlink_without_reading_target(tmp_path: Path, link: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "project"
    root.mkdir()
    target = root / ".qwen/agents/demo.md"
    if link == "parent":
        (root / ".qwen").symlink_to(outside)
    else:
        target.parent.mkdir(parents=True)
        (outside / "demo.md").write_text("outside")
        target.symlink_to(outside / ("missing.md" if link == "broken" else "demo.md"))
    with pytest.raises(ValueError, match="ссылка"):
        fp.change(root, target, b"ready")
    assert not (outside / "agents").exists()


def test_ready_content_preserves_trailing_blank_lines_and_binary_diff(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_bytes(b"\xffold")
    binary_result = fp.preview(tmp_path, [fp.change(tmp_path, target, b"ready\n\n\n")])
    assert "Binary target.md" in binary_result
    assert target.read_bytes() == b"\xffold"

    text_target = tmp_path / "plain.md"
    text_target.write_text("old\n", encoding="utf-8")
    text_result = fp.preview(tmp_path, [fp.change(tmp_path, text_target, b"ready\n\n\n")])
    assert "+ready\n+\n+" in text_result


@pytest.mark.parametrize("args", [
    ["extensions", "enable", "demo", "--yes"],
    ["extensions", "disable", "demo", "--diff", "abc"],
    ["extensions", "--project-root", "/missing", "enable", "demo", "--yes"],
    ["extensions", "--reports-dir=/missing", "disable", "demo", "--diff=abc"],
])
def test_removed_flags_fail_before_environment_check(tmp_path: Path, args: list[str]) -> None:
    project, entry = _project(tmp_path)
    before = _snapshot(tmp_path)
    result = _run(entry, *args, cwd=project)
    assert result.returncode == 2 and result.stdout.startswith("STATUS: error")
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("args", [
    ["extensions", "--yes", "enable", "demo"],
    ["extensions", "--diff", "abc", "enable", "demo"],
    ["extensions", "--diff", "enable", "demo"],
    ["extensions", "--diff=enable", "demo"],
    ["extensions", "--diff"],
    ["extensions", "--diff=abc", "--reports-dir", "/missing", "disable", "demo"],
])
def test_removed_flags_before_subcommand_fail_without_venv(tmp_path: Path, args: list[str]) -> None:
    project, entry = _project(tmp_path)
    before = _snapshot(tmp_path)
    result = _run(entry, *args, cwd=project)
    assert result.returncode == 2 and "--yes и --diff удалены" in result.stdout
    assert _snapshot(tmp_path) == before
