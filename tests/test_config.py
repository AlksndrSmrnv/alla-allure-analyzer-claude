"""Дымовые тесты загрузки Settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from alla.config import Settings


def test_settings_loads_required_env(monkeypatch, tmp_path) -> None:
    """Обязательных переменных окружения достаточно для создания Settings."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "secret-token")

    settings = Settings()

    assert settings.endpoint == "https://allure.example.com"
    assert settings.token == "secret-token"


def test_clustering_threshold_rejects_out_of_range(monkeypatch, tmp_path) -> None:
    """clustering_threshold не принимает значения вне [0.0, 1.0]."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "tok")

    with pytest.raises(ValidationError):
        Settings(clustering_threshold=-0.1)

    with pytest.raises(ValidationError):
        Settings(clustering_threshold=1.1)


def test_logs_clustering_weight_rejects_out_of_range(monkeypatch, tmp_path) -> None:
    """logs_clustering_weight не принимает значения вне [0.0, 1.0]."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "tok")

    with pytest.raises(ValidationError):
        Settings(logs_clustering_weight=-0.01)

    with pytest.raises(ValidationError):
        Settings(logs_clustering_weight=1.5)
