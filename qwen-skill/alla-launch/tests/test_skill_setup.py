"""Setup and bootstrap inspect the environment without installing or writing files."""
from __future__ import annotations

import hashlib
import importlib.metadata
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import skill_fixtures  # noqa: F401

import alla_skill


def configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("httpx==" + importlib.metadata.version("httpx") + " --hash=sha256:" + "0" * 64 + "\n")
    monkeypatch.setattr(alla_skill, "REQUIREMENTS", requirements)
    monkeypatch.setattr(alla_skill, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(alla_skill, "VENV_DIR", tmp_path / ".venv")
    monkeypatch.setattr(alla_skill, "SKILL_DIR", tmp_path / "skill")
    monkeypatch.setattr(alla_skill, "venv_python", lambda: Path(sys.executable))
    (tmp_path / ".venv").mkdir()
    return requirements


def test_existing_environment_validation_never_calls_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(tmp_path, monkeypatch)
    calls = []
    original = alla_skill.subprocess.call

    def call(command, **kwargs):
        calls.append(command)
        assert "pip" not in command and "ensurepip" not in command
        return original(command, **kwargs)

    monkeypatch.setattr(alla_skill.subprocess, "call", call)
    assert alla_skill.setup([]) == 0
    assert calls
    assert not (tmp_path / ".venv/.alla-setup-complete").exists()


def test_validation_requires_every_applicable_locked_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requirements = configure(tmp_path, monkeypatch)
    requirements.write_text(requirements.read_text() + "missing-alla-distribution==1.0 --hash=sha256:" + "0" * 64 + "\n")
    marker = tmp_path / ".venv/.alla-setup-complete"
    marker.write_text(hashlib.sha256(requirements.read_bytes()).hexdigest())
    before = marker.read_bytes()
    assert alla_skill.setup([]) == 0
    assert marker.read_bytes() == before


@pytest.mark.parametrize("line", [
    "httpx>=1", "httpx @ https://forbidden.invalid/x.whl", "-r other.txt", "--find-links https://forbidden.invalid", "-e .", "httpx==1 --hash=sha256:abc",
])
def test_lock_rejects_external_sources_and_unhashed_requirements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str) -> None:
    requirements = configure(tmp_path, monkeypatch)
    requirements.write_text(line + "\n")
    with pytest.raises(ValueError):
        alla_skill.locked_requirements()


def test_python_markers_use_target_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(tmp_path, monkeypatch)
    entries = [("missing-alla-distribution", "1", "python_full_version < '3.0'")]
    assert alla_skill.validate_dependencies(Path(sys.executable), entries)
    entries.append(("missing-alla-distribution", "1", "python_full_version >= '3.0'"))
    assert not alla_skill.validate_dependencies(Path(sys.executable), entries)


def _snapshot(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    return {
        str(path.relative_to(root)): (path.lstat().st_mode, path.lstat().st_mtime_ns,
                                     path.read_bytes() if path.is_file() else None)
        for path in [root, *sorted(root.rglob("*"))]
    }


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "QA's project"
    skill = project / ".qwen" / "skills" / "alla-launch"
    shutil.copytree(Path(alla_skill.__file__).parent, skill / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (skill / "requirements.txt").write_text(
        "httpx==" + importlib.metadata.version("httpx") + " --hash=sha256:" + "0" * 64 + "\n")
    (skill / ".env.example").write_text("ALLURE_TOKEN=\n")
    return project, skill / "scripts" / "alla_skill.py"


def _run(entry: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != alla_skill.CHILD_MARKER}
    # Prove suppression comes from bootstrap, not the developer's environment.
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run([sys.executable, str(entry), *args], capture_output=True,
                          text=True, env=env, timeout=30, check=False)


@pytest.mark.skipif(os.name == "nt", reason="POSIX interpreter wrapper")
@pytest.mark.parametrize("state", ["missing", "ready", "missing_package", "wrong_version", "broken", "old_python"])
@pytest.mark.parametrize("old_marker", [False, True])
@pytest.mark.parametrize("env_exists", [False, True])
def test_setup_real_process_is_read_only(tmp_path: Path, state: str, old_marker: bool, env_exists: bool) -> None:
    project, entry = _project(tmp_path)
    if state != "missing" or old_marker:
        (project / ".venv/bin").mkdir(parents=True)
    if state != "missing":
        python = project / ".venv/bin/python"
        body = {"broken": "exit 1", "old_python": "echo 3.9"}.get(
            state, "exec " + shlex.quote(sys.executable) + ' "$@"')
        python.write_text("#!/bin/sh\n" + body + "\n")
        python.chmod(0o755)
    requirements = entry.parents[1] / "requirements.txt"
    if state == "missing_package":
        requirements.write_text("alla-missing-test-package==1 --hash=sha256:" + "0" * 64 + "\n")
    elif state == "wrong_version":
        requirements.write_text("httpx==0.0.0 --hash=sha256:" + "0" * 64 + "\n")
    if old_marker:
        (project / ".venv/.alla-setup-complete").write_text(hashlib.sha256(requirements.read_bytes()).hexdigest())
    # Existing settings and their restrictive permissions must also remain unchanged.
    if env_exists:
        (entry.parents[1] / ".env").write_text("synthetic settings\n")
        (entry.parents[1] / ".env").chmod(0o600)
    before = _snapshot(project)
    for _ in range(2):
        result = _run(entry, "setup")
        if state == "ready":
            expected = "ready"
        elif state in ("broken", "old_python") or (state == "missing" and old_marker):
            expected = "error"
        else:
            expected = "setup_required"
        assert result.stdout.startswith("STATUS: " + expected + "\n"), result
        assert result.returncode == (2 if expected == "error" else 0), result
        assert "инженер" in result.stdout and "install-files" in result.stdout
        assert _snapshot(project) == before


@pytest.mark.parametrize("args", [("--wheelhouse", "/local/wheels"), ("--python", "python3.11"), ("--yes",), ("--diff", "0" * 64), ("--help",)])
def test_setup_rejects_old_flags_without_writing(tmp_path: Path, args: tuple[str, ...]) -> None:
    project, entry = _project(tmp_path)
    before = _snapshot(project)
    result = _run(entry, "setup", *args)
    assert result.returncode == 2
    assert result.stdout.startswith("STATUS: error\n")
    assert _snapshot(project) == before
    assert not (entry.parents[1] / ".env").exists()


def test_setup_missing_environment_prints_manual_steps_without_creating_env(tmp_path: Path) -> None:
    project, entry = _project(tmp_path)
    before = _snapshot(project)
    result = _run(entry, "setup")
    assert result.stdout.startswith("STATUS: setup_required\n")
    assert "--require-hashes" in result.stdout and ".env.example" in result.stdout
    assert _snapshot(project) == before
    assert not (entry.parents[1] / ".env").exists()
    assert not (project / ".venv").exists()


def test_setup_uses_real_venv_metadata_without_pip_or_marker(tmp_path: Path) -> None:
    """A real pyvenv.cfg isolates target metadata from the invoking interpreter."""
    import json

    project, entry = _project(tmp_path)
    venv = project / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    paths = json.loads(subprocess.check_output(
        [str(python), "-I", "-B", "-c", "import json, sysconfig; print(json.dumps(sysconfig.get_paths()))"], text=True))
    metadata = Path(paths["purelib"]) / "alla_synthetic_probe-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: alla-synthetic-probe\nVersion: 1.0\n")
    requirements = entry.parent.parent / "requirements.txt"
    requirements.write_text("alla-synthetic-probe==1.0 --hash=sha256:" + "0" * 64 + "\n")
    before = _snapshot(project)
    result = _run(entry, "setup")
    assert result.returncode == 0 and result.stdout.startswith("STATUS: ready"), result.stdout
    assert _snapshot(project) == before
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: alla-synthetic-probe\nVersion: 2.0\n")
    before = _snapshot(project)
    result = _run(entry, "setup")
    assert result.returncode == 0 and result.stdout.startswith("STATUS: setup_required"), result.stdout
    assert _snapshot(project) == before
