"""Общие фикстуры тестов скилла alla-launch.

Не ``conftest.py`` намеренно: тестовые модули скилла импортируют
фикстуры явно, а папка скилла остаётся самодостаточной.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from skill_fake_testops import TOKEN, FakeTestOps, default_launch  # noqa: E402


@pytest.fixture(autouse=True)
def without_libmagic(monkeypatch: pytest.MonkeyPatch) -> None:
    """В venv скилла нет python-magic — тесты не должны зависеть от libmagic."""
    from alla_core.services import log_extraction_service

    monkeypatch.setattr(log_extraction_service, "_MAGIC_AVAILABLE", False)


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Проект автотестов с исходниками и изолированной папкой скилла."""
    from alla_skill_lib import workspace

    root = tmp_path / "autotests"
    orders = root / "src" / "test" / "java" / "ru" / "company" / "orders"
    orders.mkdir(parents=True)
    (root / ".git").mkdir()  # alla-kb ищется в корне git-репозитория
    (orders / "OrderTest.java").write_text(
        "package ru.company.orders;\n"
        "\n"
        "public class OrderTest {\n"
        "    @Test\n"
        "    public void createOrder() {\n"
        "        assertEquals(200, api.create().status());\n"
        "    }\n"
        "\n"
        "    @Test\n"
        "    public void updateOrder() {\n"
        "        assertEquals(200, api.update().status());\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    auth = root / "src" / "test" / "java" / "ru" / "company" / "auth"
    auth.mkdir(parents=True)
    (auth / "LoginTest.java").write_text(
        "package ru.company.auth;\n\npublic class LoginTest {\n    public void login() {}\n}\n",
        encoding="utf-8",
    )
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    monkeypatch.setattr(workspace, "SKILL_DIR", skill_dir)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://testops.example")
    monkeypatch.setenv("ALLURE_TOKEN", TOKEN)
    monkeypatch.setenv("ALLURE_PAGE_SIZE", "3")
    return root


@pytest.fixture(name="testops")
def testops_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeTestOps:
    """Фейковый TestOps: подменяет httpx.AsyncClient во вендоренном клиенте."""
    fake = FakeTestOps(default_launch())
    fake.install(monkeypatch)
    return fake
