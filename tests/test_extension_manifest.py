"""Validate the native Qwen Code CLI extension manifest and layout."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import skill_fixtures  # noqa: F401

from alla_core import credentials
from qwen_credentials_support import (
    SYNTHETIC_TOKEN,
    QwenCredentialUnavailable,
    install_synthetic_credentials,
)

ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
PROCESS_CONTROL_ENV_VARS = frozenset({
    "PATH",
    "NODE_OPTIONS",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "BUNFIG_TOML",
    "ENV",
    "BASH_ENV",
    "HOME",
    "QWEN_HOME",
})


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "Missing YAML frontmatter"
    _, header, _ = text.split("---\n", 2)
    result: dict[str, str] = {}
    for line in header.splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip("\"'")
    return result


def test_extension_manifest_schema_and_safety() -> None:
    manifest_path = ROOT / "qwen-extension.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["name"] == "alla"
    assert SEMVER_RE.fullmatch(str(manifest.get("version", "")))
    assert manifest["commands"] == "commands"
    assert manifest["skills"] == "skills"
    assert manifest["agents"] == "agents"

    settings = manifest.get("settings")
    assert isinstance(settings, list) and settings
    env_vars = {item.get("envVar"): item for item in settings if isinstance(item, dict)}
    assert "ALLURE_TOKEN" in env_vars
    assert env_vars["ALLURE_TOKEN"].get("sensitive") is True
    for env_var in env_vars:
        assert env_var not in PROCESS_CONTROL_ENV_VARS

    # Context file at repo root is intentionally absent so instructions load lazily via /alla:launch.
    assert not (ROOT / "QWEN.md").exists()

    for directory_key in ("commands", "skills", "agents"):
        assert (ROOT / manifest[directory_key]).is_dir()

    for path in ROOT.rglob("*"):
        if ".git" in path.parts or ".venv" in path.parts:
            continue
        if path.is_symlink():
            assert path.resolve().is_relative_to(ROOT.resolve()), f"Escaping symlink: {path}"


def test_extension_skill_and_agents_frontmatter() -> None:
    skill_md = ROOT / "skills" / "launch" / "SKILL.md"
    assert skill_md.is_file()
    skill_fm = _frontmatter(skill_md.read_text(encoding="utf-8"))
    assert skill_fm.get("name") == "launch"
    assert skill_fm.get("description")

    expected_agents = ("alla-batch", "alla-git-publish", "alla-sut-code", "alla-tickets")
    for agent_name in expected_agents:
        agent_path = ROOT / "agents" / f"{agent_name}.md"
        assert agent_path.is_file(), f"Missing agent file: {agent_path}"
        agent_fm = _frontmatter(agent_path.read_text(encoding="utf-8"))
        assert agent_fm.get("name") == agent_name
        assert agent_fm.get("description")


def test_native_qwen_cli_install_and_token_resolution(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Native Qwen extension install test requires POSIX PTY")
    project = tmp_path / "project"
    project.mkdir(parents=True)
    try:
        env = install_synthetic_credentials(tmp_path, project)
    except QwenCredentialUnavailable as exc:
        pytest.skip(str(exc))

    qwen = shutil.which("qwen")
    assert qwen is not None

    ext_list = subprocess.run(
        [qwen, "extensions", "list"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ext_list.returncode == 0, ext_list.stderr
    assert "alla" in ext_list.stdout

    settings_list = subprocess.run(
        [qwen, "extensions", "settings", "list", "alla"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert settings_list.returncode == 0, settings_list.stderr
    assert "ALLURE_TOKEN" in settings_list.stdout
    assert SYNTHETIC_TOKEN not in settings_list.stdout + settings_list.stderr

    assert credentials.load_qwen_token(project, env) == SYNTHETIC_TOKEN
