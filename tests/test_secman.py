"""Тесты helper-а чтения секретов из secman."""

from __future__ import annotations

from pathlib import Path

import pytest

from alla.exceptions import ConfigurationError
from alla import secman


def test_build_secman_client_requires_addr(monkeypatch) -> None:
    """Без SECMAN_ADDR helper не создаёт клиента."""
    monkeypatch.delenv("SECMAN_ADDR", raising=False)

    with pytest.raises(ConfigurationError, match="SECMAN_ADDR"):
        secman.build_secman_client()


def test_login_with_kubernetes_requires_existing_jwt_file(monkeypatch, tmp_path: Path) -> None:
    """Отсутствующий JWT-файл приводит к ConfigurationError."""
    jwt_path = tmp_path / "missing-jwt"
    monkeypatch.setenv("SECMAN_K8S_ROLE", "alla-role")
    monkeypatch.setenv("SECMAN_K8S_JWT_PATH", str(jwt_path))

    class _Client:
        auth = None

    with pytest.raises(ConfigurationError, match="JWT file not found"):
        secman.login_with_kubernetes(_Client())


def test_login_with_kubernetes_requires_non_empty_jwt(monkeypatch, tmp_path: Path) -> None:
    """Пустой JWT-файл приводит к ConfigurationError."""
    jwt_path = tmp_path / "jwt"
    jwt_path.write_text("   \n", encoding="utf-8")

    monkeypatch.setenv("SECMAN_K8S_ROLE", "alla-role")
    monkeypatch.setenv("SECMAN_K8S_JWT_PATH", str(jwt_path))

    class _Client:
        auth = None

    with pytest.raises(ConfigurationError, match="JWT file is empty"):
        secman.login_with_kubernetes(_Client())


def test_login_with_kubernetes_calls_hvac_login(monkeypatch, tmp_path: Path) -> None:
    """Helper вызывает kubernetes login с ролью и JWT."""
    jwt_path = tmp_path / "jwt"
    jwt_path.write_text("jwt-token\n", encoding="utf-8")

    monkeypatch.setenv("SECMAN_K8S_ROLE", "alla-role")
    monkeypatch.setenv("SECMAN_K8S_JWT_PATH", str(jwt_path))

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

    secman.login_with_kubernetes(_Client())

    assert captured == {
        "role": "alla-role",
        "jwt": "jwt-token",
        "mount_point": "kubernetes",
    }


def test_fetch_allure_secrets_reads_kv_v2(monkeypatch) -> None:
    """KV v2-ответ корректно преобразуется в словарь секретов."""
    monkeypatch.setenv("SECMAN_MOUNT_POINT", "kv")
    monkeypatch.setenv("SECMAN_SECRET_PATH", "alla/prod")

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

    monkeypatch.setattr(secman, "build_secman_client", _fake_build_client)
    monkeypatch.setattr(secman, "login_with_kubernetes", _fake_login)

    secrets = secman.fetch_allure_secrets()

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
    monkeypatch.setenv("SECMAN_MOUNT_POINT", "kv")
    monkeypatch.setenv("SECMAN_SECRET_PATH", "alla/prod")

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

    monkeypatch.setattr(secman, "build_secman_client", lambda: _Client())
    monkeypatch.setattr(secman, "login_with_kubernetes", lambda client: None)

    with pytest.raises(ConfigurationError, match="ALLURE_LANGFLOW_API_KEY"):
        secman.fetch_allure_secrets()


def test_main_masks_secret_values(monkeypatch, capsys) -> None:
    """Demo-режим печатает только ключи и маскировку, не значения секретов."""
    monkeypatch.setattr(
        secman,
        "fetch_allure_secrets",
        lambda: {
            "ALLURE_TOKEN": "super-secret-token",
            "ALLURE_KB_POSTGRES_DSN": "postgresql://user:pass@db/app",
            "ALLURE_LANGFLOW_API_KEY": "langflow-secret",
        },
    )

    exit_code = secman.main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "ALLURE_TOKEN=<hidden:" in captured.out
    assert "ALLURE_KB_POSTGRES_DSN=<hidden:" in captured.out
    assert "ALLURE_LANGFLOW_API_KEY=<hidden:" in captured.out
    assert "super-secret-token" not in captured.out
    assert "postgresql://user:pass@db/app" not in captured.out
    assert "langflow-secret" not in captured.out
