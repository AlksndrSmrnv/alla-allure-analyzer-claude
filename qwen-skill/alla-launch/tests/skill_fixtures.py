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


ORDER_TEST_JAVA = (
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
    "}\n"
)
LOGIN_TEST_JAVA = (
    "package ru.company.auth;\n\npublic class LoginTest {\n    public void login() {}\n}\n"
)


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _isolate_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Изолированная папка скилла и настройки TestOps из окружения."""
    from alla_skill_lib import workspace

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    monkeypatch.setattr(workspace, "SKILL_DIR", skill_dir)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://testops.example")
    monkeypatch.setenv("ALLURE_TOKEN", TOKEN)
    monkeypatch.setenv("ALLURE_PAGE_SIZE", "3")
    # Сеанс Qwen того, кто запускает тесты, в папки разборов тестов не пишется.
    for name in ("QWEN_CODE_SESSION_ID", "QWEN_CODE_PROJECT_DIR", "QWEN_HOME"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Проект автотестов с исходниками и изолированной папкой скилла."""
    root = tmp_path / "autotests"
    (root / ".git").mkdir(parents=True)  # alla-kb ищется в корне git-репозитория
    java = root / "src" / "test" / "java" / "ru" / "company"
    _write(java / "orders" / "OrderTest.java", ORDER_TEST_JAVA)
    _write(java / "auth" / "LoginTest.java", LOGIN_TEST_JAVA)
    _isolate_skill(tmp_path, monkeypatch)
    return root


@pytest.fixture(name="nested_project")
def nested_project_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Репозиторий с единственным Maven-проектом ``autotests/pom.xml``; скилл стоит в корне."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write(root / "autotests" / "pom.xml", "<project/>\n")
    java = root / "autotests" / "src" / "test" / "java" / "ru" / "company"
    _write(java / "orders" / "OrderTest.java", ORDER_TEST_JAVA)
    _write(java / "auth" / "LoginTest.java", LOGIN_TEST_JAVA)
    _isolate_skill(tmp_path, monkeypatch)
    return root


@pytest.fixture(name="multimodule_project")
def multimodule_project_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Многомодульный Gradle/Maven-проект: ``orders`` (Gradle Kotlin DSL) и ``auth`` (Maven)."""
    root = tmp_path / "autotests"
    (root / ".git").mkdir(parents=True)
    _write(root / "settings.gradle.kts", 'include("orders")\n')
    _write(root / "orders" / "build.gradle.kts")
    _write(root / "auth" / "pom.xml", "<project/>\n")
    java = Path("src") / "test" / "java" / "ru" / "company"
    _write(root / "orders" / java / "orders" / "OrderTest.java", ORDER_TEST_JAVA)
    _write(root / "auth" / java / "auth" / "LoginTest.java", LOGIN_TEST_JAVA)
    _isolate_skill(tmp_path, monkeypatch)
    return root


@pytest.fixture(name="testops")
def testops_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeTestOps:
    """Фейковый TestOps: подменяет httpx.AsyncClient во вендоренном клиенте."""
    fake = FakeTestOps(default_launch())
    fake.install(monkeypatch)
    return fake
