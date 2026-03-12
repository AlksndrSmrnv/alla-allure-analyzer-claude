"""Тесты helper-а чтения секретов из secman."""

from __future__ import annotations

from pathlib import Path

import pytest

from alla.config import Settings
from alla.clients import secman_client
from alla.exceptions import ConfigurationError


def _make_settings(**overrides) -> Settings:
    defaults = {
        "endpoint": "https://allure.example.com",
        "token": "secret-token",
        "secman_addr": "https://secman.example.com",
        "secman_namespace": "",
        "secman_k8s_role": "alla-role",
        "secman_k8s_jwt_path": secman_client.DEFAULT_K8S_JWT_PATH,
        "secman_kv_version": "v2",
        "secman_mount_point": "kv",
        "secman_secret_path": "alla/prod",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_build_secman_client_requires_addr(monkeypatch) -> None:
    """Без ALLURE_SECMAN_ADDR helper не создаёт клиента."""
    client = secman_client.SecmanClient(_make_settings(secman_addr=""))

    with pytest.raises(ConfigurationError, match="ALLURE_SECMAN_ADDR"):
        client.build_secman_client()


def test_login_with_kubernetes_requires_existing_jwt_file(monkeypatch, tmp_path: Path) -> None:
    """Отсутствующий JWT-файл приводит к ConfigurationError."""
    jwt_path = tmp_path / "missing-jwt"
    client = secman_client.SecmanClient(
        _make_settings(secman_k8s_jwt_path=str(jwt_path)),
    )

    class _Client:
        auth = None

    with pytest.raises(ConfigurationError, match="JWT file not found"):
        client.login_with_kubernetes(_Client())


def test_login_with_kubernetes_requires_non_empty_jwt(monkeypatch, tmp_path: Path) -> None:
    """Пустой JWT-файл приводит к ConfigurationError."""
    jwt_path = tmp_path / "jwt"
    jwt_path.write_text("   \n", encoding="utf-8")
    client = secman_client.SecmanClient(
        _make_settings(secman_k8s_jwt_path=str(jwt_path)),
    )

    class _Client:
        auth = None

    with pytest.raises(ConfigurationError, match="JWT file is empty"):
        client.login_with_kubernetes(_Client())


def test_login_with_kubernetes_calls_hvac_login(monkeypatch, tmp_path: Path) -> None:
    """Helper вызывает kubernetes login с ролью и JWT."""
    jwt_path = tmp_path / "jwt"
    jwt_path.write_text("jwt-token\n", encoding="utf-8")
    client = secman_client.SecmanClient(
        _make_settings(secman_k8s_jwt_path=str(jwt_path)),
    )

    captured: dict[str, str] = {}

    class _KubernetesAuth:
        def login(self, *, role: str, jwt: str, mount_point: str) -> None:
            captured["role"] = role
            captured["jwt"] = jwt
            captured["mount_point"] = mount_point

    class _Auth:
        kubernetes = _KubernetesAuth()

    class _Client:
        auth = _Auth()

    client.login_with_kubernetes(_Client())

    assert captured == {
        "role": "alla-role",
        "jwt": "jwt-token",
        "mount_point": "kubernetes",
    }


def test_fetch_allure_secrets_reads_kv_v2(monkeypatch) -> None:
    """KV v2-ответ корректно преобразуется в словарь секретов."""
    client = secman_client.SecmanClient(_make_settings())

    login_called = False
    captured: dict[str, str] = {}

    class _V2:
        def read_secret_version(self, *, path: str, mount_point: str) -> dict:
            captured["path"] = path
            captured["mount_point"] = mount_point
            return {
                "data": {
                    "data": {
                        "ALLURE_TOKEN": "test-token",
                        "ALLURE_KB_POSTGRES_DSN": "postgresql://db",
                        "ALLURE_LANGFLOW_API_KEY": "langflow-key",
                    },
                },
            }

    class _KV:
        v2 = _V2()

    class _Secrets:
        kv = _KV()

    class _Client:
        secrets = _Secrets()

    def _fake_build_client():
        return _Client()

    def _fake_login(client) -> None:
        nonlocal login_called
        login_called = True

    monkeypatch.setattr(client, "build_secman_client", _fake_build_client)
    monkeypatch.setattr(client, "login_with_kubernetes", _fake_login)

    secrets = client.fetch_allure_secrets()

    assert login_called is True
    assert captured == {
        "path": "alla/prod",
        "mount_point": "kv",
    }
    assert secrets == {
        "ALLURE_TOKEN": "test-token",
        "ALLURE_KB_POSTGRES_DSN": "postgresql://db",
        "ALLURE_LANGFLOW_API_KEY": "langflow-key",
    }


def test_fetch_allure_secrets_requires_all_expected_keys(monkeypatch) -> None:
    """Если хотя бы одного ключа нет, helper падает."""
    client = secman_client.SecmanClient(_make_settings())

    class _V2:
        def read_secret_version(self, *, path: str, mount_point: str) -> dict:
            return {
                "data": {
                    "data": {
                        "ALLURE_TOKEN": "test-token",
                        "ALLURE_KB_POSTGRES_DSN": "postgresql://db",
                    },
                },
            }

    class _KV:
        v2 = _V2()

    class _Secrets:
        kv = _KV()

    class _Client:
        secrets = _Secrets()

    monkeypatch.setattr(client, "build_secman_client", lambda: _Client())
    monkeypatch.setattr(client, "login_with_kubernetes", lambda client: None)

    with pytest.raises(ConfigurationError, match="ALLURE_LANGFLOW_API_KEY"):
        client.fetch_allure_secrets()


def test_fetch_allure_secrets_rejects_null_response_data(monkeypatch) -> None:
    """data=null в ответе KV не должен приводить к AttributeError."""
    client = secman_client.SecmanClient(_make_settings())

    class _V2:
        def read_secret_version(self, *, path: str, mount_point: str) -> dict:
            return {"data": None}

    class _KV:
        v2 = _V2()

    class _Secrets:
        kv = _KV()

    class _Client:
        secrets = _Secrets()

    monkeypatch.setattr(client, "build_secman_client", lambda: _Client())
    monkeypatch.setattr(client, "login_with_kubernetes", lambda client: None)

    with pytest.raises(ConfigurationError, match="KV v2 secret data"):
        client.fetch_allure_secrets()


def test_main_masks_secret_values(monkeypatch, capsys) -> None:
    """Demo-режим печатает только ключи и маскировку, не значения секретов."""
    monkeypatch.setattr(secman_client, "_build_demo_settings", _make_settings)
    monkeypatch.setattr(
        secman_client.SecmanClient,
        "fetch_allure_secrets",
        lambda self: {
            "ALLURE_TOKEN": "super-secret-token",
            "ALLURE_KB_POSTGRES_DSN": "postgresql://user:pass@db/app",
            "ALLURE_LANGFLOW_API_KEY": "langflow-secret",
        },
    )

    exit_code = secman_client.main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "ALLURE_TOKEN=<hidden:" in captured.out
    assert "ALLURE_KB_POSTGRES_DSN=<hidden:" in captured.out
    assert "ALLURE_LANGFLOW_API_KEY=<hidden:" in captured.out
    assert "super-secret-token" not in captured.out
    assert "postgresql://user:pass@db/app" not in captured.out
    assert "langflow-secret" not in captured.out


def test_main_returns_exit_code_2_for_configuration_error(monkeypatch, capsys) -> None:
    """ConfigurationError должен следовать общей CLI-конвенции exit code 2."""
    monkeypatch.setattr(secman_client, "_build_demo_settings", _make_settings)

    def _raise_configuration_error(self) -> dict[str, str]:
        raise ConfigurationError("missing env")

    monkeypatch.setattr(
        secman_client.SecmanClient,
        "fetch_allure_secrets",
        _raise_configuration_error,
    )

    exit_code = secman_client.main()
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "secman helper error: missing env" in captured.err


def test_main_handles_unexpected_errors(monkeypatch, capsys) -> None:
    """Неожиданные ошибки helper-а должны печататься без traceback."""
    monkeypatch.setattr(secman_client, "_build_demo_settings", _make_settings)

    def _raise_runtime_error(self) -> dict[str, str]:
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(
        secman_client.SecmanClient,
        "fetch_allure_secrets",
        _raise_runtime_error,
    )

    exit_code = secman_client.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "secman helper failed: vault unavailable" in captured.err


def test_main_handles_keyboard_interrupt(monkeypatch, capsys) -> None:
    """Ctrl+C должен завершать demo-режим без traceback."""
    monkeypatch.setattr(secman_client, "_build_demo_settings", _make_settings)

    def _raise_keyboard_interrupt(self) -> dict[str, str]:
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        secman_client.SecmanClient,
        "fetch_allure_secrets",
        _raise_keyboard_interrupt,
    )

    exit_code = secman_client.main()
    captured = capsys.readouterr()

    assert exit_code == 130
    assert "secman helper interrupted" in captured.err
