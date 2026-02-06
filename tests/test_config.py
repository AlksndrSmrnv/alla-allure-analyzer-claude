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
    assert settings.clustering_enabled is True
    assert settings.clustering_threshold == 0.60
    assert settings.kb_enabled is False
    assert settings.log_level == "INFO"
