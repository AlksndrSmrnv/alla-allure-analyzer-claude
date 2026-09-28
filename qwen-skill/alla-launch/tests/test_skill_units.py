"""Модульные тесты: формат разбора, подсказки по коду, кадры стека, конфиг."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from skill_fixtures import without_libmagic  # noqa: F401

from alla_core.config import Settings
from alla_core.exceptions import ConfigurationError
from alla_core.models.clustering import ClusterSignature, FailureCluster
from alla_core.services.prompt_builder_service import build_cluster_analysis_prompt
from alla_skill_lib.analysis_format import parse_analysis, validate_analysis
from alla_skill_lib.cluster_task import project_frames, split_prompt, strip_knowledge_base
from alla_skill_lib.code_hints import ProjectIndex, hints_for_cluster

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


# --- формат разбора -------------------------------------------------------


def test_parse_plain_format() -> None:
    analysis = parse_analysis(
        "ЧТО СЛОМАЛОСЬ: Тест получил 500.\nВ логе NPE.\n\n"
        "ПРИЧИНА: приложение — NPE в OrderService.\n\n"
        "КАК ИСПРАВИТЬ:\n1. Починить OrderService.\n2. Добавить проверку.\n"
    )
    assert analysis.what == "Тест получил 500.\nВ логе NPE."
    assert analysis.category == "приложение"
    assert analysis.cause_reason == "NPE в OrderService."
    assert analysis.fix.startswith("1. Починить")
    assert analysis.code == []


@pytest.mark.parametrize(
    ("cause", "category"),
    [
        ("**Тест** — неверное ожидание", "тест"),
        ("[окружение] стенд недоступен", "окружение"),
        ("Автотест: устаревший локатор", "тест"),
        ("тестовые данные — нет клиента", "данные"),
        ("неизвестно — мало данных", "неизвестно"),
        ("Сервис упал", "приложение"),
        ("тестовый стенд лежит", None),
        ("баг", None),
    ],
)
def test_detect_category(cause: str, category: str | None) -> None:
    assert parse_analysis(f"ПРИЧИНА: {cause}").category == category


def test_parse_markdown_decorations() -> None:
    analysis = parse_analysis(
        "Вот разбор:\n"
        "### **Что сломалось:**\nТаймаут.\n"
        "- **Причина:** окружение — БД недоступна\n"
        "**КАК ИСПРАВИТЬ**\n1. Поднять БД.\n"
        "Код ответа: 504\n"
    )
    assert analysis.what == "Таймаут."
    assert analysis.category == "окружение"
    assert "Код ответа: 504" in analysis.fix  # «Код ответа:» — не раздел КОД
    assert analysis.code == []


def test_validate_reports_missing_sections(tmp_path: Path) -> None:
    errors = validate_analysis(parse_analysis("ПРИЧИНА: что-то сломалось"), tmp_path)
    assert any("ЧТО СЛОМАЛОСЬ" in error for error in errors)
    assert any("категория" in error for error in errors)
    assert any("КАК ИСПРАВИТЬ" in error for error in errors)


def test_validate_code_paths(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text("def test_x(): pass\n", encoding="utf-8")
    base = "ЧТО СЛОМАЛОСЬ: x\nПРИЧИНА: тест — y\nКАК ИСПРАВИТЬ:\n1. z\n"

    ok = parse_analysis(base + "КОД: tests/test_api.py:1 — вызывает api.create()\n")
    assert validate_analysis(ok, tmp_path) == []

    method_only = parse_analysis(base + "КОД: вызов orderApi.create в шаге\n")
    assert validate_analysis(method_only, tmp_path) == []

    missing = parse_analysis(base + "КОД: tests/test_gone.py:3 — нет файла\n")
    assert any("tests/test_gone.py" in error for error in validate_analysis(missing, tmp_path))

    outside = parse_analysis(base + "КОД: ../secret.py:1 — вне проекта\n")
    (tmp_path.parent / "secret.py").write_text("", encoding="utf-8")
    assert validate_analysis(outside, tmp_path)


def test_compact_keeps_cause_and_first_sentence() -> None:
    analysis = parse_analysis(
        "ЧТО СЛОМАЛОСЬ: Первое. Второе.\nПРИЧИНА: данные — нет клиента\nКАК ИСПРАВИТЬ:\n1. x\n"
    )
    assert analysis.compact() == "ПРИЧИНА: данные — нет клиента\nЧТО СЛОМАЛОСЬ: Первое."


# --- задание кластера -----------------------------------------------------


@pytest.mark.parametrize(
    ("message", "log"),
    [("expected 200 but was 500", "ERROR NPE"), (None, "ERROR NPE"), ("boom", None)],
)
def test_task_has_no_knowledge_base_mentions(message: str | None, log: str | None) -> None:
    cluster = FailureCluster(
        cluster_id="c1",
        label="boom",
        signature=ClusterSignature(),
        member_count=1,
        example_message=message,
        example_step_path="Шаг",
    )
    prompt = build_cluster_analysis_prompt(cluster, None, log_snippet=log)
    _, task = split_prompt(prompt.user_prompt)
    assert task.startswith("═")
    cleaned = strip_knowledge_base(task)
    assert "знаний" not in cleaned
    assert "ЧТО СЛОМАЛОСЬ:" in cleaned and "ПРИЧИНА:" in cleaned


def test_project_frames_skip_frameworks() -> None:
    trace = (
        "java.lang.AssertionError: boom\n"
        "\tat org.junit.Assert.fail(Assert.java:89)\n"
        "\tat ru.company.orders.OrderTest.createOrder(OrderTest.java:6)\n"
        "\tat ru.company.orders.OrderTest.createOrder(OrderTest.java:6)\n"
        "\tat java.base/jdk.internal.reflect.Method.invoke(Method.java:1)\n"
        "Caused by: java.net.SocketTimeoutException: Read timed out\n"
        '  File "/ci/venv/lib/python3.11/site-packages/requests/api.py", line 59, in get\n'
        '  File "/ci/build/tests/api/test_orders.py", line 12, in test_create\n'
    )
    assert project_frames(trace) == [
        "at ru.company.orders.OrderTest.createOrder(OrderTest.java:6)",
        "Caused by: java.net.SocketTimeoutException: Read timed out",
        'File "/ci/build/tests/api/test_orders.py", line 12, in test_create',
    ]


# --- подсказки по коду ----------------------------------------------------


@pytest.fixture
def code_project(tmp_path: Path) -> Path:
    java = tmp_path / "src" / "test" / "java" / "ru" / "company"
    java.mkdir(parents=True)
    (java / "OrderTest.java").write_text(
        "class OrderTest {\n  @Test\n  void createOrder() {}\n}\n", encoding="utf-8"
    )
    api = tmp_path / "tests" / "api"
    api.mkdir(parents=True)
    (api / "test_orders.py").write_text(
        "import pytest\n\n\nclass TestOrders:\n    def test_create(self):\n        pass\n",
        encoding="utf-8",
    )
    ignored = tmp_path / "node_modules" / "lib"
    ignored.mkdir(parents=True)
    (ignored / "OrderTest.java").write_text("", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("full_name", "expected"),
    [
        ("ru.company.OrderTest.createOrder", "src/test/java/ru/company/OrderTest.java:3"),
        ("ru.company.OrderTest#createOrder", "src/test/java/ru/company/OrderTest.java:3"),
        ("tests/api/test_orders.py::TestOrders::test_create[1]", "tests/api/test_orders.py:5"),
        ("tests.api.test_orders.TestOrders.test_create", "tests/api/test_orders.py:5"),
    ],
)
def test_hint_from_full_name(code_project: Path, full_name: str, expected: str) -> None:
    hints = hints_for_cluster(ProjectIndex(code_project), [full_name], [])
    assert [hint.render().split(" — ")[0] for hint in hints] == [expected]


def test_hint_from_frames_and_unknown_names(code_project: Path) -> None:
    index = ProjectIndex(code_project)
    hints = hints_for_cluster(
        index,
        ["com.other.Missing.test", "Checkout flow > pays by card"],
        [
            "at ru.company.OrderTest.createOrder(OrderTest.java:3)",
            'File "/ci/build/tests/api/test_orders.py", line 5, in test_create',
        ],
    )
    assert [hint.render() for hint in hints] == [
        "src/test/java/ru/company/OrderTest.java:3 — кадр стека",
        "tests/api/test_orders.py:5 — кадр стека",
    ]


# --- конфигурация ---------------------------------------------------------


def test_settings_env_overrides_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nexport ALLURE_ENDPOINT='https://file.example'\nALLURE_TOKEN=file-token\n"
        "ALLURE_SSL_VERIFY=false\nDATABASE_URL=postgres://ignored\n",
        encoding="utf-8",
    )
    settings = Settings.load(env_file, environ={"ALLURE_TOKEN": "env-token", "ALLURE_PAGE_SIZE": "7"})
    assert settings.endpoint == "https://file.example"
    assert settings.token == "env-token"
    assert settings.ssl_verify is False
    assert settings.page_size == 7
    assert "env-token" not in repr(settings)


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"ALLURE_TOKEN": "t"}, "ALLURE_ENDPOINT"),
        ({"ALLURE_ENDPOINT": "allure.example", "ALLURE_TOKEN": "t"}, "http://"),
        ({"ALLURE_ENDPOINT": "https://a.example"}, "ALLURE_TOKEN"),
        ({"ALLURE_ENDPOINT": "https://a", "ALLURE_TOKEN": "t", "ALLURE_PAGE_SIZE": "x"}, "число"),
        (
            {"ALLURE_ENDPOINT": "https://a", "ALLURE_TOKEN": "t", "ALLURE_DETAIL_CONCURRENCY": "0"},
            "ALLURE_DETAIL_CONCURRENCY: допустимо от 1",
        ),
        (
            {"ALLURE_ENDPOINT": "https://a", "ALLURE_TOKEN": "t", "ALLURE_LOGS_CONCURRENCY": "0"},
            "ALLURE_LOGS_CONCURRENCY: допустимо от 1",
        ),
        (
            {"ALLURE_ENDPOINT": "https://a", "ALLURE_TOKEN": "t", "ALLURE_CLUSTERING_THRESHOLD": "1.5"},
            "допустимо от 0.0 до 1.0",
        ),
    ],
)
def test_settings_errors(environ: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        Settings.load(None, environ=environ)


def test_detect_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alla_skill_lib.workspace import detect_project_root

    project = tmp_path / "autotests"
    assert detect_project_root(project / ".qwen" / "skills" / "alla-launch") == project.resolve()

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    personal = home / ".qwen" / "skills" / "alla-launch"
    assert detect_project_root(personal) == tmp_path.resolve()
    assert detect_project_root(tmp_path / "repo" / "qwen-skill" / "alla-launch") == tmp_path.resolve()


# --- точка входа ----------------------------------------------------------


def _install_skill_copy(tmp_path: Path) -> tuple[Path, Path]:
    """Копия скилла без .venv: (папка скилла, путь к alla_skill.py)."""
    skill = tmp_path / "skill"
    shutil.copytree(SCRIPTS_DIR, skill / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (skill / "requirements.txt").write_text("httpx>=0.27\n", encoding="utf-8")
    return skill, skill / "scripts" / "alla_skill.py"


def _fake_venv_python(skill: Path) -> Path:
    """«Python» в .venv скилла — обёртка над текущим интерпретатором."""
    python = skill / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    return python


def _run_stub(stub: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "ALLA_SKILL_IN_VENV"}
    return subprocess.run(
        [sys.executable, str(stub), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("missing", "не установлено"),
        ("no_marker", "не доустановлено"),
        ("stale_marker", "не доустановлено"),
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="обёртка .venv/bin/python — POSIX")
def test_entrypoint_requires_complete_setup(tmp_path: Path, state: str, message: str) -> None:
    skill, stub = _install_skill_copy(tmp_path)
    if state != "missing":
        _fake_venv_python(skill)
    if state == "stale_marker":
        (skill / ".venv" / ".alla-setup-complete").write_text("0" * 64, encoding="utf-8")

    result = _run_stub(stub, "prepare", "123")

    assert result.returncode == 3
    assert result.stdout.startswith("STATUS: setup_required")
    assert message in result.stdout
    assert f"{stub} setup" in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="обёртка .venv/bin/python — POSIX")
def test_entrypoint_runs_cli_after_complete_setup(tmp_path: Path) -> None:
    skill, stub = _install_skill_copy(tmp_path)
    _fake_venv_python(skill)
    digest = hashlib.sha256((skill / "requirements.txt").read_bytes()).hexdigest()
    (skill / ".venv" / ".alla-setup-complete").write_text(digest + "\n", encoding="utf-8")

    result = _run_stub(stub, "next", "--project-root", str(tmp_path / "project"))

    assert result.returncode == 1, result.stderr
    assert result.stdout.startswith("STATUS: error")  # CLI отработал: разборов ещё нет
    assert "prepare" in result.stdout
