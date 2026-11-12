"""Credential boundaries and native Qwen encrypted storage on synthetic secrets."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import skill_fixtures  # noqa: F401

from alla_core import credentials
from alla_core.config import Settings
from alla_core.exceptions import ConfigurationError

from qwen_credentials_support import (
    SYNTHETIC_TOKEN, QwenCredentialUnavailable, install_synthetic_credentials,
    native_qwen_package, qwen_prompt,
)


@pytest.mark.parametrize("override", [False, True])
def test_plaintext_token_rejected_even_with_environment_override(tmp_path: Path, override: bool) -> None:
    env = tmp_path / ".env"
    env.write_text(f"export ALLURE_TOKEN='{SYNTHETIC_TOKEN}'\n")
    with pytest.raises(ConfigurationError, match="ALLURE_TOKEN в .env запрещён") as error:
        Settings.load(env, environ={"ALLURE_TOKEN": "ci-token"} if override else {})
    assert SYNTHETIC_TOKEN not in str(error.value)


@pytest.mark.parametrize("declaration", ["ALLURE_TOKEN=#comment", "ALLURE_TOKEN= #comment", "ALLURE_TOKEN=\"\" #comment"])
def test_commented_empty_file_token_never_bypasses_qwen_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration: str,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"ALLURE_ENDPOINT=https://testops.example\n{declaration}\n")
    skill_fixtures.allow_testops(tmp_path, "https://testops.example")
    calls = []
    monkeypatch.setattr("alla_core.config.load_qwen_token", lambda *args: calls.append(args) or SYNTHETIC_TOKEN)
    settings = Settings.load(env_file=env_file, project_root=tmp_path, environ={})
    assert settings.token == SYNTHETIC_TOKEN
    assert len(calls) == 1


@pytest.mark.parametrize("relative", [".env", ".qwen/.env", "skill/.env"])
@pytest.mark.parametrize("declaration", ["ALLURE_TOKEN=secret\nALLURE_TOKEN=", "export ALLURE_TOKEN: secret"])
def test_all_plaintext_declarations_rejected(tmp_path: Path, relative: str, declaration: str) -> None:
    file = tmp_path / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(declaration)
    with pytest.raises(ConfigurationError, match="ALLURE_TOKEN в .env запрещён"):
        Settings.load(tmp_path / "skill/.env", project_root=tmp_path,
                      require_testops=False, environ={"ALLURE_TOKEN": "ci-token"})


@pytest.mark.parametrize("source", ["dotEnv", "settingsEnv"])
def test_qwen_file_sourced_env_is_not_accepted_as_ci(source: str) -> None:
    origin = {"dotEnv": [], "settingsEnv": []}
    origin[source] = ["ALLURE_TOKEN"]
    with pytest.raises(ConfigurationError, match="получен Qwen"):
        Settings.load(require_testops=False, environ={
            "ALLURE_TOKEN": SYNTHETIC_TOKEN,
            credentials.PROVENANCE_KEY: json.dumps(origin)})


def test_invalid_qwen_environment_provenance_fails_closed() -> None:
    with pytest.raises(ConfigurationError, match="источник окружения") as error:
        Settings.load(require_testops=False, environ={
            "ALLURE_TOKEN": SYNTHETIC_TOKEN, credentials.PROVENANCE_KEY: SYNTHETIC_TOKEN})
    assert SYNTHETIC_TOKEN not in str(error.value)


def test_mac_sandbox_uses_existing_native_bun_without_changing_storage_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.setattr(credentials.platform, "machine", lambda: "arm64")
    assert credentials.credential_runtime(tmp_path, "/node") == "/node"
    bun = tmp_path / "node_modules/@oven/bun-darwin-aarch64/bin/bun"
    bun.parent.mkdir(parents=True)
    bun.write_text("synthetic runtime placeholder")
    bun.chmod(0o755)
    assert credentials.credential_runtime(tmp_path, "/node") == str(bun)


def test_environment_token_and_offline_commands_never_open_qwen_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def forbidden(*args: object) -> str:
        pytest.fail("credential store opened unnecessarily")
    monkeypatch.setattr("alla_core.config.load_qwen_token", forbidden)
    skill_fixtures.allow_testops(tmp_path, "https://testops.example")
    settings = Settings.load(project_root=tmp_path, environ={
        "ALLURE_ENDPOINT": "https://testops.example", "ALLURE_TOKEN": "ci-token"})
    assert settings.token == "ci-token"
    assert "ci-token" not in repr(settings)
    assert Settings.load(require_testops=False, environ={}).token == ""
    with pytest.raises(ConfigurationError, match="ALLURE_ENDPOINT"):
        Settings.load(environ={})


def test_settings_use_qwen_only_when_no_environment_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls = []
    monkeypatch.setattr("alla_core.config.load_qwen_token", lambda *args: calls.append(args) or SYNTHETIC_TOKEN)
    skill_fixtures.allow_testops(tmp_path, "https://testops.example")
    env = {"ALLURE_ENDPOINT": "https://testops.example"}
    assert Settings.load(project_root=tmp_path, environ=env).token == SYNTHETIC_TOKEN
    assert calls == [(tmp_path, env)]


@pytest.mark.parametrize("outcome", ["timeout", "stderr", "malformed", "empty"])
def test_private_bridge_failures_do_not_disclose_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str,
) -> None:
    monkeypatch.setattr(credentials, "qwen_package", lambda _: tmp_path)
    monkeypatch.setattr(credentials.shutil, "which", lambda *args, **kwargs: "/node")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert SYNTHETIC_TOKEN not in " ".join(command)
        assert kwargs["env"] == {"HOME": str(tmp_path), "PATH": "/bin"}
        assert kwargs["stdin"] == subprocess.DEVNULL
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output=SYNTHETIC_TOKEN)
        return subprocess.CompletedProcess(command, int(outcome == "stderr"),
            SYNTHETIC_TOKEN if outcome != "empty" else '{"token":"", "source":"qwen:user"}',
            SYNTHETIC_TOKEN)

    monkeypatch.setattr(credentials.subprocess, "run", run)
    with pytest.raises(ConfigurationError) as error:
        credentials.load_qwen_token(tmp_path, {
            "HOME": str(tmp_path), "PATH": "/bin", "NODE_OPTIONS": SYNTHETIC_TOKEN,
            "OPENAI_API_KEY": SYNTHETIC_TOKEN, "ALLURE_TOKEN": SYNTHETIC_TOKEN})
    assert SYNTHETIC_TOKEN not in str(error.value)


def test_native_qwen_cli_encryption_scope_precedence_and_failure_modes(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("native credential fixture requires POSIX PTY")
    try:
        package = native_qwen_package()
    except QwenCredentialUnavailable as exc:
        pytest.skip(str(exc))
    project = tmp_path / "project with spaces"
    project.mkdir()
    env = install_synthetic_credentials(tmp_path, project)
    secret_file = Path(env["QWEN_HOME"]) / "extension-secrets-v1.json"
    encrypted = secret_file.read_text()
    assert SYNTHETIC_TOKEN not in encrypted
    assert secret_file.stat().st_mode & 0o777 == 0o600
    (project / "bunfig.toml").write_text('preload = ["./preload.mjs"]\n')
    (project / "preload.mjs").write_text(
        "import {writeFileSync} from 'node:fs'; writeFileSync('PRELOAD_EXECUTED', 'bad');\n")
    (project / ".env.local").write_text("UNTRUSTED_CREDENTIAL_RUNTIME=loaded\n")
    assert credentials.load_qwen_token(project, env) == SYNTHETIC_TOKEN
    assert not (project / "PRELOAD_EXECUTED").exists()
    qwen = shutil.which("qwen")
    assert qwen is not None
    listed = subprocess.run([qwen, "extensions", "settings", "list", credentials.EXTENSION_NAME],
                            cwd=project, env=env, capture_output=True, text=True, timeout=30)
    assert listed.returncode == 0
    assert SYNTHETIC_TOKEN not in listed.stdout + listed.stderr
    workspace_token = SYNTHETIC_TOKEN + "-workspace"
    qwen_prompt([qwen, "extensions", "settings", "set", credentials.EXTENSION_NAME,
                 "ALLURE_TOKEN", "--scope", "workspace"], env, project, workspace_token)
    before = {p: p.read_bytes() for p in Path(env["HOME"]).rglob("*") if p.is_file()}
    assert credentials.load_qwen_token(project, env) == workspace_token
    assert {p: p.read_bytes() for p in Path(env["HOME"]).rglob("*") if p.is_file()} == before
    second_project = tmp_path / "other project"
    second_project.mkdir()
    assert credentials.load_qwen_token(second_project, env) == SYNTHETIC_TOKEN
    (project / ".env").write_text(f"ALLURE_TOKEN={SYNTHETIC_TOKEN}\n")
    with pytest.raises(ConfigurationError, match="удалите ALLURE_TOKEN"):
        credentials.load_qwen_token(project, env)
    (project / ".env").unlink()
    extension_env = Path(env["QWEN_HOME"]) / "extensions" / credentials.EXTENSION_NAME / ".env"
    extension_env.write_text(f"ALLURE_TOKEN={SYNTHETIC_TOKEN}\nALLURE_TOKEN=\n")
    with pytest.raises(ConfigurationError, match="удалите ALLURE_TOKEN"):
        credentials.load_qwen_token(project, env)
    extension_env.write_text("")
    secret_file.write_text("corrupted encrypted storage")
    with pytest.raises(ConfigurationError, match="не удалось прочитать"):
        credentials.load_qwen_token(project, env)
    # Unknown versions must fail before reading storage, never fall back to plaintext.
    unsupported = tmp_path / "unsupported"
    unsupported.mkdir()
    (unsupported / "package.json").write_text(json.dumps({"name": "@qwen-code/qwen-code", "version": "0.28.0"}))
    result = subprocess.run([shutil.which("node") or "node", str(credentials.BRIDGE),
                             str(unsupported), str(project)],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and result.stdout == "" and result.stderr == "QWEN_UNSUPPORTED\n"
    assert package.is_dir()
