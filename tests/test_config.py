"""Тесты загрузки конфигурации Settings из переменных окружения."""

from __future__ import annotations

from alla.config import Settings


def test_settings_loads_from_env_vars(monkeypatch, tmp_path) -> None:
    """Settings корректно читает ALLURE_* переменные окружения."""
    monkeypatch.chdir(tmp_path)  # изоляция от .env в корне проекта
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "secret-token")
    monkeypatch.setenv("ALLURE_PROJECT_ID", "42")

    settings = Settings()

    assert settings.endpoint == "https://allure.example.com"
    assert settings.token == "secret-token"
    assert settings.project_id == 42


def test_settings_defaults_are_applied(monkeypatch, tmp_path) -> None:
    """При наличии только обязательных env vars — все остальные поля имеют дефолты."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "secret-token")

    settings = Settings()

    assert settings.ssl_verify is True
    assert settings.page_size == 100
    assert settings.max_pages == 50
    assert settings.request_timeout == 30
    assert settings.clustering_threshold == 0.60
    assert settings.log_level == "INFO"
    assert settings.secman_k8s_jwt_path == "/var/run/secrets/kubernetes.io/serviceaccount/token"
    assert settings.secman_kv_version == "v2"
    assert settings.secman_addr == ""
    assert settings.secman_mount_point == ""
    assert settings.secman_secret_path == ""
    # Computed properties: без DSN/URL — фичи неактивны
    assert settings.kb_active is False
    assert settings.llm_active is False


def test_settings_loads_secman_fields_from_env(monkeypatch, tmp_path) -> None:
    """Settings читает ALLURE_SECMAN_* переменные окружения."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "secret-token")
    monkeypatch.setenv("ALLURE_SECMAN_ADDR", "https://secman.example.com")
    monkeypatch.setenv("ALLURE_SECMAN_NAMESPACE", "team-a")
    monkeypatch.setenv("ALLURE_SECMAN_K8S_ROLE", "alla-role")
    monkeypatch.setenv("ALLURE_SECMAN_K8S_JWT_PATH", "/tmp/jwt")
    monkeypatch.setenv("ALLURE_SECMAN_KV_VERSION", "v2")
    monkeypatch.setenv("ALLURE_SECMAN_MOUNT_POINT", "secret")
    monkeypatch.setenv("ALLURE_SECMAN_SECRET_PATH", "alla/prod")

    settings = Settings()

    assert settings.secman_addr == "https://secman.example.com"
    assert settings.secman_namespace == "team-a"
    assert settings.secman_k8s_role == "alla-role"
    assert settings.secman_k8s_jwt_path == "/tmp/jwt"
    assert settings.secman_kv_version == "v2"
    assert settings.secman_mount_point == "secret"
    assert settings.secman_secret_path == "alla/prod"
