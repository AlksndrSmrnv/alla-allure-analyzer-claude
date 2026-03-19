"""Smoke tests for Settings loading."""

from __future__ import annotations

from alla.config import Settings


def test_settings_loads_required_env(monkeypatch, tmp_path) -> None:
    """Required env vars are enough to build Settings."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "secret-token")

    settings = Settings()

    assert settings.endpoint == "https://allure.example.com"
    assert settings.token == "secret-token"
