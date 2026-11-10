"""Configuration changes require the reviewed bytes, and failures restore both sides."""

from __future__ import annotations

import json
import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from skill_fixtures import project_fixture  # noqa: F401

from alla_skill_lib import cli
from alla_skill_lib import file_transaction as ft
from alla_skill_lib import workspace as ws

SKILL = Path(__file__).resolve().parents[1]


def _preview(project: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert cli.main(["install-files", "--project-root", str(project)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("STATUS: install_files")
    assert "+++ b/.qwen/agents/alla-batch.md" in text
    match = re.search(r"--diff ([0-9a-f]{64})", text)
    assert match
    return match.group(1)


@pytest.mark.parametrize("changed", ["source", "target", "config", "project"])
def test_cli_rejects_stale_snapshot(
    project: Path, capsys: pytest.CaptureFixture[str], changed: str,
) -> None:
    shutil.copytree(SKILL / "agents", ws.SKILL_DIR / "agents")
    digest = _preview(project, capsys)
    assert not (project / ".qwen").exists()  # no provisional writes to delete on refusal
    destination = project
    if changed == "source":
        with (ws.SKILL_DIR / "agents" / "alla-batch.md").open("a") as stream:
            stream.write("unreviewed instructions\n")
    elif changed == "target":
        target = project / ".qwen" / "agents" / "alla-batch.md"
        target.parent.mkdir(parents=True)
        target.write_text("user edit\n")
    elif changed == "config":
        config = project / "alla-extensions" / "config.toml"
        config.write_text('[testops]\nallowed_origins = ["https://different.example"]\n')
    else:
        destination = project.parent / "another"
        destination.mkdir()
    assert cli.main(["install-files", "--project-root", str(destination), "--yes",
                     "--diff", digest]) == 1
    assert capsys.readouterr().out.startswith("STATUS: error")
    target = destination / ".qwen" / "agents" / "alla-batch.md"
    assert not target.exists() or target.read_text() == "user edit\n"


def test_cli_yes_requires_diff(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.copytree(SKILL / "agents", ws.SKILL_DIR / "agents")
    assert cli.main(["install-files", "--project-root", str(project), "--yes"]) == 1
    assert "--diff" in capsys.readouterr().out
    assert not (project / ".qwen").exists()


def test_partial_write_rolls_back_files_and_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = tmp_path / ".qwen" / "agents" / "demo.md"
    config = tmp_path / "alla-extensions" / "config.toml"
    agent.parent.mkdir(parents=True)
    config.parent.mkdir()
    agent.write_bytes(b"old agent")
    config.write_bytes(b'enabled = []\n[testops]\nallowed_origins = ["https://safe.example"]\n')
    original_config = config.read_bytes()
    changes = [ft.change(tmp_path, agent, b"new agent"), ft.change(tmp_path, config, b"new config")]
    write = ft._write

    def fail(target: Path, data: bytes | None) -> None:
        if target == config and data == b"new config":
            raise OSError("injected disk failure")
        write(target, data)

    monkeypatch.setattr(ft, "_write", fail)
    with pytest.raises(OSError, match="disk failure"):
        ft.apply(tmp_path, changes, ft.digest(tmp_path, changes))
    assert agent.read_bytes() == b"old agent"
    assert config.read_bytes() == original_config
    assert not (tmp_path / ft.JOURNAL).exists()


@pytest.mark.parametrize("foreign", [False, True])
def test_recovers_interrupted_transaction_without_overwriting_foreign_edit(
    tmp_path: Path, foreign: bool,
) -> None:
    target = tmp_path / ".qwen" / "agents" / "demo.md"
    target.parent.mkdir(parents=True)
    changes = [ft.Change(target, b"old", b"new")]
    target.write_bytes(b"external edit" if foreign else b"new")
    journal = tmp_path / ft.JOURNAL
    journal.write_text(json.dumps({"project_root": str(tmp_path.resolve()),
                                   "changes": ft._rows(tmp_path, changes)}))
    journal.chmod(0o600)
    if foreign:
        with pytest.raises(ValueError, match="изменён извне"):
            ft.recover(tmp_path)
        assert target.read_bytes() == b"external edit" and journal.exists()
    else:
        ft.recover(tmp_path)
        assert target.read_bytes() == b"old" and not journal.exists()


@pytest.mark.parametrize("link", ["parent", "file"])
def test_does_not_write_through_symlink(tmp_path: Path, link: str) -> None:
    other = tmp_path / "other"
    other.mkdir()
    target = tmp_path / ".qwen" / "agents" / "demo.md"
    if link == "parent":
        (tmp_path / ".qwen").symlink_to(other)
    else:
        target.parent.mkdir(parents=True)
        (other / "demo.md").write_bytes(b"outside")
        target.symlink_to(other / "demo.md")
    with pytest.raises(ValueError, match="ссылка"):
        ft.change(tmp_path, target, b"new")
    assert not (other / "agents").exists()


def test_extension_preview_includes_config_and_refusal_preserves_it(
    project: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from test_skill_extensions import make_demo

    make_demo(project)
    config = project / "alla-extensions" / "config.toml"
    before = config.read_bytes()
    args = ["extensions", "enable", "demo", "--project-root", str(project)]
    assert cli.main(args) == 0
    text = capsys.readouterr().out
    assert '+++ b/alla-extensions/config.toml' in text
    assert '+enabled = ["demo"]' in text and '+name: alla-demo' in text
    assert config.read_bytes() == before and not (project / ".qwen").exists()
    match = re.search(r"--diff ([0-9a-f]{64})", text)
    assert match
    assert cli.main([*args, "--yes", "--diff", match.group(1)]) == 0
    assert b'allowed_origins' in config.read_bytes()
    assert (project / ".qwen" / "agents" / "alla-demo.md").exists()


def test_recovery_rejects_forged_journal_outside_configuration(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    changes = [ft.Change(target, b"forged instructions", None)]
    journal = tmp_path / ft.JOURNAL
    journal.write_text(json.dumps({"project_root": str(tmp_path.resolve()),
                                  "changes": ft._rows(tmp_path, changes)}))
    journal.chmod(0o600)
    with pytest.raises(ValueError, match="недопустимый путь"):
        ft.recover(tmp_path)
    assert not target.exists()


def test_failure_after_journal_unlink_restores_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / ".qwen" / "agents" / "demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    changes = [ft.change(tmp_path, target, b"new")]
    sync = ft._sync
    calls = 0

    def fail(root: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("last fsync")
        sync(root)

    monkeypatch.setattr(ft, "_sync", fail)
    with pytest.raises(OSError, match="last fsync"):
        ft.apply(tmp_path, changes, ft.digest(tmp_path, changes))
    assert target.read_bytes() == b"old"
    assert not (tmp_path / ft.JOURNAL).exists()


def test_concurrent_apply_waits_for_lock_then_rechecks_snapshot(tmp_path: Path) -> None:
    target = tmp_path / ".qwen" / "agents" / "demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    code = """
import sys
from pathlib import Path
from alla_skill_lib import file_transaction as ft
root = Path(sys.argv[1])
target = root / '.qwen/agents/demo.md'
changes = [ft.Change(target, b'old', b'new')]
(root / 'ready').touch()
try:
    ft.apply(root, changes, ft.digest(root, changes))
except ValueError:
    sys.exit(3)
"""
    with (tmp_path / ft.LOCK).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path)], cwd=tmp_path,
            env={"PATH": os.defpath, "PYTHONPATH": str(SKILL / "scripts")},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (tmp_path / "ready").exists()
            time.sleep(0.1)
            assert process.poll() is None and target.read_bytes() == b"old"
            target.write_bytes(b"external edit")
            fcntl.flock(lock, fcntl.LOCK_UN)
            _, error = process.communicate(timeout=10)
            assert process.returncode == 3, error
            assert target.read_bytes() == b"external edit"
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
