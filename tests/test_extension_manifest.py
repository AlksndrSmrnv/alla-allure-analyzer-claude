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

from qwen_credentials_support import isolated_qwen_env, stage_extension

ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


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
    assert "settings" not in manifest
    assert not (ROOT / "scripts" / "qwen_credentials.mjs").exists()

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


def test_native_qwen_cli_install(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Native Qwen extension install test requires POSIX")
    qwen = shutil.which("qwen")
    if qwen is None:
        pytest.skip("Нужен qwen в PATH")

    project = tmp_path / "project"
    project.mkdir(parents=True)
    env = isolated_qwen_env(tmp_path)
    staged = stage_extension(tmp_path / "staged-alla-extension")

    install = subprocess.run(
        [qwen, "extensions", "install", str(staged), "--consent"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert install.returncode == 0, install.stderr

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
