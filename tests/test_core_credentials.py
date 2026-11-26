"""Credential boundaries and OS keyring storage on synthetic secrets."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import keyring
import keyring.backends.chainer as chainer_mod
import keyring.backends.fail as fail_mod
import keyring.backends.null as null_mod
import keyring.errors as kr_errors
import pytest
import skill_fixtures  # noqa: F401
from keyring.backend import KeyringBackend
from skill_fake_testops import TOKEN as FAKE_TESTOPS_TOKEN
from skill_fake_testops import FakeTestOps, default_launch
from skill_fixtures import project_fixture, testops_fixture  # noqa: F401

from alla_core import credentials
from alla_core.config import Settings
from alla_core.exceptions import ConfigurationError
from alla_skill_lib import cli
from qwen_credentials_support import (
    SYNTHETIC_TOKEN,
    install_isolated_keyring_backend,
    isolated_qwen_env,
    stage_extension,
)


class _MemoryKeyring(KeyringBackend):
    priority = 10

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise kr_errors.PasswordDeleteError("missing")
        del self.items[(service, username)]


def _use_memory_keyring(monkeypatch: pytest.MonkeyPatch) -> _MemoryKeyring:
    backend = _MemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    return backend


@pytest.mark.parametrize("override", [False, True])
def test_plaintext_token_rejected_even_with_environment_override(tmp_path: Path, override: bool) -> None:
    env = tmp_path / ".env"
    env.write_text(f"export ALLURE_TOKEN='{SYNTHETIC_TOKEN}'\n")
    with pytest.raises(ConfigurationError, match="ALLURE_TOKEN в .env запрещён") as error:
        Settings.load(env, environ={"ALLURE_TOKEN": "ci-token"} if override else {})
    assert SYNTHETIC_TOKEN not in str(error.value)


@pytest.mark.parametrize("declaration", ["ALLURE_TOKEN=#comment", "ALLURE_TOKEN= #comment", "ALLURE_TOKEN=\"\" #comment"])
def test_commented_empty_file_token_never_bypasses_keyring_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration: str,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"ALLURE_ENDPOINT=https://testops.example\n{declaration}\n")
    skill_fixtures.allow_testops(tmp_path, "https://testops.example")
    calls: list[Path | None] = []
    monkeypatch.setattr("alla_core.config.load_token", lambda root: calls.append(root) or SYNTHETIC_TOKEN)
    settings = Settings.load(env_file=env_file, project_root=tmp_path, environ={})
    assert settings.token == SYNTHETIC_TOKEN
    assert calls == [tmp_path]


@pytest.mark.parametrize("relative", [".env", ".qwen/.env", ".qwen/alla.env", "extension/.env"])
@pytest.mark.parametrize("declaration", ["ALLURE_TOKEN=secret\nALLURE_TOKEN=", "export ALLURE_TOKEN: secret"])
def test_all_plaintext_declarations_rejected(tmp_path: Path, relative: str, declaration: str) -> None:
    file = tmp_path / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(declaration)
    with pytest.raises(ConfigurationError, match="ALLURE_TOKEN в .env запрещён"):
        Settings.load(tmp_path / ".qwen/alla.env", project_root=tmp_path,
                      fallback_env_file=tmp_path / "extension/.env",
                      require_testops=False, environ={"ALLURE_TOKEN": "ci-token"})


def test_fallback_env_file_precedence(tmp_path: Path) -> None:
    project_env = tmp_path / ".qwen" / "alla.env"
    project_env.parent.mkdir(parents=True)
    ext_env = tmp_path / "extension" / ".env"
    ext_env.parent.mkdir(parents=True)
    ext_env.write_text(
        "ALLURE_ENDPOINT=https://fallback.example\n"
        "ALLURE_PROJECT_ID=99\n",
        encoding="utf-8",
    )
    project_env.write_text("ALLURE_PROJECT_ID=42\n", encoding="utf-8")
    skill_fixtures.allow_testops(tmp_path, "https://fallback.example")
    settings = Settings.load(
        env_file=project_env,
        fallback_env_file=ext_env,
        project_root=tmp_path,
        environ={"ALLURE_TOKEN": "ci-token", "ALLURE_PROJECT_ID": "7"},
    )
    assert settings.endpoint == "https://fallback.example"
    assert settings.project_id == "7"

    settings_from_files = Settings.load(
        env_file=project_env,
        fallback_env_file=ext_env,
        project_root=tmp_path,
        environ={"ALLURE_TOKEN": "ci-token"},
    )
    assert settings_from_files.endpoint == "https://fallback.example"
    assert settings_from_files.project_id == "42"


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


def test_environment_token_and_offline_commands_never_open_keyring_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def forbidden(*args: object) -> str:
        pytest.fail("keyring opened unnecessarily")

    monkeypatch.setattr("alla_core.config.load_token", forbidden)
    skill_fixtures.allow_testops(tmp_path, "https://testops.example")
    settings = Settings.load(project_root=tmp_path, environ={
        "ALLURE_ENDPOINT": "https://testops.example", "ALLURE_TOKEN": "ci-token"})
    assert settings.token == "ci-token"
    assert "ci-token" not in repr(settings)
    assert Settings.load(require_testops=False, environ={}).token == ""
    with pytest.raises(ConfigurationError, match="ALLURE_ENDPOINT"):
        Settings.load(environ={})


def test_settings_use_keyring_only_when_no_environment_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls: list[Path | None] = []
    monkeypatch.setattr("alla_core.config.load_token", lambda root: calls.append(root) or SYNTHETIC_TOKEN)
    skill_fixtures.allow_testops(tmp_path, "https://testops.example")
    env = {"ALLURE_ENDPOINT": "https://testops.example"}
    assert Settings.load(project_root=tmp_path, environ=env).token == SYNTHETIC_TOKEN
    assert calls == [tmp_path]


def test_keyring_scope_precedence_project_isolation_and_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _use_memory_keyring(monkeypatch)
    project_a = tmp_path / "project a"
    project_b = tmp_path / "project b"
    project_a.mkdir()
    project_b.mkdir()

    user_token = SYNTHETIC_TOKEN + "-user"
    ws_token = SYNTHETIC_TOKEN + "-ws"

    backend_name = credentials.store_token(f"  {user_token}  ", scope="user", project_root=project_a)
    assert backend_name.endswith("_MemoryKeyring")
    assert credentials.load_token(project_a) == user_token
    assert credentials.load_token(project_b) == user_token
    assert credentials.load_token(None) == user_token

    credentials.store_token(ws_token, scope="workspace", project_root=project_a)
    assert credentials.load_token(project_a) == ws_token
    assert credentials.load_token(project_b) == user_token

    status_a = credentials.token_status(project_a, {})
    assert status_a.backend_available is True
    assert status_a.has_workspace is True
    assert status_a.has_user is True
    assert status_a.has_env is False
    assert status_a.active_source == "keyring:workspace"

    status_env = credentials.token_status(project_a, {"ALLURE_TOKEN": "ci-token"})
    assert status_env.has_env is True
    assert status_env.active_source == "env"

    assert credentials.delete_token(scope="workspace", project_root=project_a) is True
    assert credentials.delete_token(scope="workspace", project_root=project_a) is False
    assert credentials.load_token(project_a) == user_token

    assert credentials.delete_token(scope="user", project_root=project_a) is True
    with pytest.raises(ConfigurationError, match="токен не найден"):
        credentials.load_token(project_a)


@pytest.mark.parametrize("bad_token", ["", "   ", f"{SYNTHETIC_TOKEN}\n", "a\rb", "a\x00b"])
def test_store_token_rejects_empty_and_control_chars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_token: str,
) -> None:
    _use_memory_keyring(monkeypatch)
    with pytest.raises(ConfigurationError) as error:
        credentials.store_token(bad_token, scope="workspace", project_root=tmp_path)
    assert SYNTHETIC_TOKEN not in str(error.value)


def test_unavailable_and_chainer_backends_handled_safely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    for bad_backend in (fail_mod.Keyring(), null_mod.Keyring()):
        monkeypatch.setattr(keyring, "get_keyring", lambda b=bad_backend: b)
        with pytest.raises(ConfigurationError, match="недоступно системное хранилище"):
            credentials.load_token(tmp_path)
        status = credentials.token_status(tmp_path, {})
        assert status.backend_available is False
        assert status.active_source == ""

    empty_chainer = chainer_mod.ChainerBackend()
    monkeypatch.setattr(chainer_mod.ChainerBackend, "backends", property(lambda self: []))
    monkeypatch.setattr(keyring, "get_keyring", lambda: empty_chainer)
    with pytest.raises(ConfigurationError, match="недоступно системное хранилище"):
        credentials.load_token(tmp_path)

    memory = _MemoryKeyring()
    monkeypatch.setattr(chainer_mod.ChainerBackend, "backends", property(lambda self: [memory]))
    monkeypatch.setattr(keyring, "get_keyring", lambda: empty_chainer)
    described = credentials.store_token(SYNTHETIC_TOKEN, scope="workspace", project_root=tmp_path)
    assert described.endswith("_MemoryKeyring")
    assert credentials.load_token(tmp_path) == SYNTHETIC_TOKEN


def test_keyring_errors_are_sanitized_without_leaking_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class FailingKeyring(KeyringBackend):
        priority = 10

        def get_password(self, service: str, username: str) -> str | None:
            raise kr_errors.KeyringError("boom " + SYNTHETIC_TOKEN)

        def set_password(self, service: str, username: str, password: str) -> None:
            raise kr_errors.PasswordSetError(f"boom {password}")

        def delete_password(self, service: str, username: str) -> None:
            raise kr_errors.PasswordDeleteError(f"boom {SYNTHETIC_TOKEN}")

    monkeypatch.setattr(keyring, "get_keyring", lambda: FailingKeyring())
    with pytest.raises(ConfigurationError, match="не удалось сохранить") as set_err:
        credentials.store_token(SYNTHETIC_TOKEN, scope="workspace", project_root=tmp_path)
    assert SYNTHETIC_TOKEN not in str(set_err.value)

    with pytest.raises(ConfigurationError, match="не удалось прочитать") as get_err:
        credentials.load_token(tmp_path)
    assert SYNTHETIC_TOKEN not in str(get_err.value)

    class DeleteFailingKeyring(_MemoryKeyring):
        def delete_password(self, service: str, username: str) -> None:
            raise kr_errors.PasswordDeleteError(f"boom {SYNTHETIC_TOKEN}")

    failing_del = DeleteFailingKeyring()
    failing_del.set_password(credentials.KEYRING_SERVICE, credentials.workspace_account(tmp_path), SYNTHETIC_TOKEN)
    monkeypatch.setattr(keyring, "get_keyring", lambda: failing_del)
    with pytest.raises(ConfigurationError, match="не удалось удалить") as del_err:
        credentials.delete_token(scope="workspace", project_root=tmp_path)
    assert SYNTHETIC_TOKEN not in str(del_err.value)


def test_cli_token_commands_and_check_with_keyring(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _use_memory_keyring(monkeypatch)
    monkeypatch.delenv("ALLURE_TOKEN", raising=False)
    monkeypatch.setenv("QWEN_HOME", str(project / ".qwen-home"))
    FakeTestOps(default_launch()).install(monkeypatch)

    # Non-TTY without --stdin refuses cleanly.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = cli.main(["token", "set", "--project-root", str(project)])
    assert code == 2
    out = capsys.readouterr().out
    assert out.startswith("STATUS: error") and "вручную" in out

    # Interactive prompt via getpass.
    class _TtyStdin(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _TtyStdin(""))
    monkeypatch.setattr("alla_skill_lib.cli.getpass.getpass", lambda _prompt="": FAKE_TESTOPS_TOKEN)
    code = cli.main(["token", "set", "--project-root", str(project)])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("STATUS: saved") and FAKE_TESTOPS_TOKEN not in out

    # Status reports workspace source without printing token.
    code = cli.main(["token", "status", "--project-root", str(project)])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("STATUS: ok") and "workspace" in out and FAKE_TESTOPS_TOKEN not in out

    # check resolves token from keyring without env ALLURE_TOKEN.
    code = cli.main(["check", "--project-root", str(project)])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("STATUS: ready") and "токен принят" in out and FAKE_TESTOPS_TOKEN not in out

    # token set --stdin overwrites user scope.
    monkeypatch.setattr(sys, "stdin", io.StringIO(SYNTHETIC_TOKEN + "\n"))
    code = cli.main(["token", "set", "--scope", "user", "--stdin", "--project-root", str(project)])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("STATUS: saved") and "user" in out and SYNTHETIC_TOKEN not in out

    # token status rejects plaintext token in .env.
    (project / ".env").write_text(f"ALLURE_TOKEN={SYNTHETIC_TOKEN}\n", encoding="utf-8")
    code = cli.main(["token", "status", "--project-root", str(project)])
    assert code == 2
    out = capsys.readouterr().out
    assert out.startswith("STATUS: error") and "запрещён" in out and SYNTHETIC_TOKEN not in out
    (project / ".env").unlink()

    # delete removes workspace token, then reports nothing to delete.
    code = cli.main(["token", "delete", "--scope", "workspace", "--project-root", str(project)])
    assert code == 0 and capsys.readouterr().out.startswith("STATUS: saved")
    code = cli.main(["token", "delete", "--scope", "workspace", "--project-root", str(project)])
    assert code == 0 and capsys.readouterr().out.startswith("STATUS: ok")


def test_subprocess_token_lifecycle_uses_isolated_keyring_backend(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX subprocess keyring fixture")
    project = tmp_path / "project with spaces"
    project.mkdir()
    ext = stage_extension(tmp_path / "staged-alla-extension")
    env = isolated_qwen_env(tmp_path)
    home = Path(env["HOME"])
    store_file = install_isolated_keyring_backend(home)
    # Use the active test .venv inside project/.venv via symlink so alla_skill.py finds keyring.
    venv_dir = Path(sys.prefix)
    if not (venv_dir / "bin" / "python").exists():
        pytest.skip("Требуется .venv с установленным keyring")
    (project / ".venv").symlink_to(venv_dir)

    entry = ext / "scripts" / "alla_skill.py"
    run_env = {**env, "PATH": os.environ.get("PATH", "")}

    res_set = subprocess.run(
        [sys.executable, str(entry), "token", "set", "--stdin", "--project-root", str(project)],
        input=SYNTHETIC_TOKEN,
        cwd=project,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_set.returncode == 0, res_set.stdout + res_set.stderr
    assert res_set.stdout.startswith("STATUS: saved")
    assert SYNTHETIC_TOKEN not in res_set.stdout + res_set.stderr
    assert store_file.is_file()
    assert store_file.stat().st_mode & 0o777 == 0o600
    assert SYNTHETIC_TOKEN.encode("utf-8") not in store_file.read_bytes()

    res_status = subprocess.run(
        [sys.executable, str(entry), "token", "status", "--project-root", str(project)],
        cwd=project,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_status.returncode == 0, res_status.stdout + res_status.stderr
    assert "keyring:workspace" in res_status.stdout or "системное хранилище ОС (workspace)" in res_status.stdout
    assert SYNTHETIC_TOKEN not in res_status.stdout + res_status.stderr

    # Reading via load_token inside subprocess confirms full end-to-end retrieval.
    res_load = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from pathlib import Path; from alla_core.credentials import load_token; "
                "assert load_token(Path(sys.argv[2])) == sys.argv[3]"
            ),
            str(ext / "scripts"),
            str(project),
            SYNTHETIC_TOKEN,
        ],
        cwd=project,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_load.returncode == 0, res_load.stderr

    res_del = subprocess.run(
        [sys.executable, str(entry), "token", "delete", "--project-root", str(project)],
        cwd=project,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_del.returncode == 0, res_del.stdout + res_del.stderr
    assert res_del.stdout.startswith("STATUS: saved")
    assert shutil.which("python3") is not None
