"""Дымовые тесты загрузки Settings."""

from __future__ import annotations

import base64
import os
import tempfile

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


def test_resolve_cert_files_cleans_up_cert_when_key_creation_fails(monkeypatch, tmp_path) -> None:
    """При сбое создания key temp-файла ранее созданный cert-файл удаляется."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "tok")

    settings = Settings(
        gigachat_cert_b64=base64.b64encode(b"cert").decode("ascii"),
        gigachat_key_b64=base64.b64encode(b"key").decode("ascii"),
    )

    created_paths: list[str] = []
    real_named_temporary_file = tempfile.NamedTemporaryFile
    call_count = 0

    def fake_named_temporary_file(*args: object, **kwargs: object):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            temp_file = real_named_temporary_file(*args, **kwargs)
            created_paths.append(temp_file.name)
            return temp_file
        raise OSError("disk full")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", fake_named_temporary_file)

    with pytest.raises(OSError, match="disk full"):
        settings.resolve_cert_files()

    assert created_paths
    assert not os.path.exists(created_paths[0])
