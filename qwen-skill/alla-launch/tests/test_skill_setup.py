"""Setup validates installed distributions, and installs only an explicit wheelhouse."""
from __future__ import annotations

import importlib.metadata
import os
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
    monkeypatch.setattr(alla_skill, "SETUP_MARKER", tmp_path / ".venv/.alla-setup-complete")
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
    assert alla_skill.SETUP_MARKER.read_text().strip() == alla_skill.requirements_digest()


def test_validation_requires_every_applicable_locked_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requirements = configure(tmp_path, monkeypatch)
    requirements.write_text(requirements.read_text() + "missing-alla-distribution==1.0 --hash=sha256:" + "0" * 64 + "\n")
    alla_skill.SETUP_MARKER.write_text(alla_skill.requirements_digest())
    assert alla_skill.setup([]) == 0
    assert not alla_skill.SETUP_MARKER.exists()


def test_offline_install_has_no_config_or_environment_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(tmp_path, monkeypatch)
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "httpx-1.0-py3-none-any.whl").write_bytes(b"synthetic local wheel")
    (wheels / "links.html").write_text('<a href="https://forbidden.invalid/x.whl">remote</a>')
    monkeypatch.setenv("PIP_INDEX_URL", "https://forbidden.invalid")
    monkeypatch.setenv("PIP_FIND_LINKS", "https://forbidden.invalid/wheels")
    monkeypatch.setenv("PIP_CONFIG_FILE", "/some/pip.conf")
    monkeypatch.setattr(alla_skill, "use_existing_venv", lambda _: True)
    monkeypatch.setattr(alla_skill, "ensure_pip", lambda _: True)
    monkeypatch.setattr(alla_skill, "validate_dependencies", lambda *_: True)
    calls = []

    def call(command, **kwargs):
        calls.append((command, kwargs))
        if "pip" in command:
            links = Path(command[command.index("--find-links") + 1])
            assert links != wheels
            assert [path.name for path in links.iterdir()] == ["httpx-1.0-py3-none-any.whl"]
            assert (links / "httpx-1.0-py3-none-any.whl").read_bytes() == b"synthetic local wheel"
        return 0

    monkeypatch.setattr(alla_skill.subprocess, "call", call)
    assert alla_skill.setup(["--wheelhouse", str(wheels)]) == 0
    command, options = next((cmd, options) for cmd, options in calls if "pip" in cmd)
    assert "--no-index" in command and "--only-binary=:all:" in command
    assert "--require-hashes" in command and "--no-deps" in command
    assert not Path(command[command.index("--find-links") + 1]).exists()
    pip_env = {key: value for key, value in options["env"].items() if key.startswith("PIP_")}
    assert pip_env == {"PIP_CONFIG_FILE": os.devnull}


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


@pytest.mark.parametrize("kind", ["html", "symlink", "directory"])
def test_wheelhouse_rejects_nonlocal_wheel_candidates(tmp_path: Path, kind: str) -> None:
    from alla_wheels import local_wheels

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    if kind == "html":
        (wheels / "links.html").write_text('<a href="https://forbidden.invalid/pkg.whl">wheel</a>')
    elif kind == "symlink":
        target = tmp_path / "outside.whl"
        target.write_bytes(b"wheel")
        (wheels / "pkg.whl").symlink_to(target)
    else:
        (wheels / "pkg.whl").mkdir()
    with pytest.raises((ValueError, OSError)):
        with local_wheels(wheels):
            pytest.fail("invalid wheelhouse accepted")
