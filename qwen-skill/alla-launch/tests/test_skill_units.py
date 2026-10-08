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
from alla_skill_lib.analysis_format import parse_analysis, parse_summary, validate_analysis
from alla_skill_lib.cluster_task import project_frames
from alla_skill_lib.code_hints import ProjectIndex, hints_for_cluster
from alla_core.utils.log_focus import FOCUS_NOTE, focus_log

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
        ("Приложение вернуло 500", "приложение"),
        ("Сервис упал", None),
        ("Сервис авторизации недоступен (окружение)", None),
        ("Приложение или окружение — не ясно", None),
        ("тест/данные — не ясно", None),
        ("Test environment is down", None),
        ("Data validation error in API", None),
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


def test_parse_numbered_and_dash_headers() -> None:
    analysis = parse_analysis(
        "1. ЧТО СЛОМАЛОСЬ: Тест получил 500.\n"
        "2. **Причина** — приложение: NPE в сервисе\n"
        "3) Как исправить:\n1. Починить сервис.\n"
        "4. КОД: OrderTest.java:6 — вызов\n"
    )
    assert analysis.what == "Тест получил 500."
    assert analysis.category == "приложение"
    assert analysis.cause == "приложение: NPE в сервисе"
    assert analysis.fix == "1. Починить сервис."
    assert analysis.code == ["OrderTest.java:6 — вызов"]


def test_dash_header_may_end_the_line() -> None:
    analysis = parse_analysis(
        "ЧТО СЛОМАЛОСЬ — Тест упал по таймауту.\nПРИЧИНА — тест: устаревший локатор.\n"
        "КАК ИСПРАВИТЬ —\n1. Обновить локатор.\n"
    )
    assert analysis.cause == "тест: устаревший локатор."
    assert analysis.fix == "1. Обновить локатор."
    assert parse_analysis("Код-ревью не проведён").code == []  # дефис без пробела — не заголовок


def test_parse_ignores_bom_and_crlf() -> None:
    analysis = parse_analysis(
        "\ufeffЧТО СЛОМАЛОСЬ: x\r\nПРИЧИНА: тест — y\r\nКАК ИСПРАВИТЬ:\r\n1. z\r\n"
    )
    assert (analysis.what, analysis.category, analysis.fix) == ("x", "тест", "1. z")


def test_plain_list_item_in_fix_steps_is_not_a_header() -> None:
    analysis = parse_analysis(
        "ЧТО СЛОМАЛОСЬ: x\nПРИЧИНА: тест — y\n"
        "КАК ИСПРАВИТЬ:\n"
        "1. Открыть тест.\n"
        "- Код: обновить локатор\n"
        "2. Причина: см. выше\n"
        "3. Проверить.\n"
        "КОД: Test.java:3 — локатор\n"
    )
    assert "обновить локатор" in analysis.fix
    assert "3. Проверить." in analysis.fix
    assert analysis.cause == "y" or analysis.cause.endswith("y")
    assert analysis.code == ["Test.java:3 — локатор"]


def test_emphasised_header_in_list_is_still_a_header() -> None:
    analysis = parse_analysis(
        "ЧТО СЛОМАЛОСЬ: x\nПРИЧИНА: тест — y\nКАК ИСПРАВИТЬ:\n1. z\n"
        "- **КОД:** Test.java:3 — локатор\n"
    )
    assert analysis.code == ["Test.java:3 — локатор"]
    assert analysis.fix == "1. z"


def test_header_words_inside_text_are_not_headers() -> None:
    analysis = parse_analysis(
        "ЧТО СЛОМАЛОСЬ: Код-ревью не проведён.\nКод ответа: 504\n"
        "ПРИЧИНА: окружение — причина-следствие не ясна\nКАК ИСПРАВИТЬ:\n1. a\n"
    )
    assert "Код-ревью" in analysis.what and "Код ответа: 504" in analysis.what
    assert analysis.category == "окружение"


def test_header_accepted_once_except_code() -> None:
    analysis = parse_analysis(
        "ЧТО СЛОМАЛОСЬ: a\nПРИЧИНА: тест — б\nКАК ИСПРАВИТЬ:\n1. в\n"
        "КОД: A.java:1\nКОД: B.java:2\nПРИЧИНА: окружение — повтор\n"
    )
    assert analysis.category == "тест"  # повтор ПРИЧИНЫ не перезаписывает первую
    assert analysis.code[:2] == ["A.java:1", "B.java:2"]  # а КОД бывает несколько раз


def test_header_rest_keeps_underscores() -> None:
    analysis = parse_analysis("КОД: tests/__init__.py:4 — импорт\nПРИЧИНА: **тест** — y")
    assert analysis.code == ["tests/__init__.py:4 — импорт"]
    assert analysis.category == "тест"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("нет", None),
        ("подходящих записей нет", None),
        ("не применимо", None),
        ("not applicable", None),
        ("-", None),
        ("timeout_in_login_1a2b3c4d", "timeout_in_login_1a2b3c4d"),
        ("`timeout_in_login_1a2b3c4d`", "timeout_in_login_1a2b3c4d"),
        ("timeout_in_login_1a2b3c4d — признак совпал", "timeout_in_login_1a2b3c4d"),
        ("timeout_in_login_1a2b3c4d подходит", "timeout_in_login_1a2b3c4d"),
    ],
)
def test_kb_reference_parsing(value: str, expected: str | None) -> None:
    assert parse_analysis(f"БАЗА ЗНАНИЙ: {value}").kb_ref == expected


def test_parse_summary_shows_what_was_understood() -> None:
    analysis = parse_analysis(
        "ЧТО ПОШЛО НЕ ТАК: всё\nПРИЧИНА: сервис лежит\nВЫВОД: плохо\n"
    )
    summary = parse_summary(analysis)
    assert "ЧТО СЛОМАЛОСЬ ✗" in summary
    assert "ПРИЧИНА ✓ (категория: не распознана)" in summary
    assert "КАК ИСПРАВИТЬ ✗" in summary
    assert "ЧТО ПОШЛО НЕ ТАК: всё" in summary


def test_ambiguous_category_error_asks_for_one(tmp_path: Path) -> None:
    analysis = parse_analysis(
        "ЧТО СЛОМАЛОСЬ: x\nПРИЧИНА: приложение или окружение\nКАК ИСПРАВИТЬ:\n1. z\n"
    )
    errors = validate_analysis(analysis, tmp_path)
    assert len(errors) == 1 and "одна категория" in errors[0]


def test_code_basename_resolves_when_unique(tmp_path: Path) -> None:
    (tmp_path / "src" / "a").mkdir(parents=True)
    (tmp_path / "src" / "b").mkdir(parents=True)
    (tmp_path / "src" / "a" / "OrderTest.java").write_text("1\n2\n3\n", encoding="utf-8")
    (tmp_path / "src" / "a" / "Dup.java").write_text("x\n", encoding="utf-8")
    (tmp_path / "src" / "b" / "Dup.java").write_text("y\n", encoding="utf-8")
    base = "ЧТО СЛОМАЛОСЬ: x\nПРИЧИНА: тест — y\nКАК ИСПРАВИТЬ:\n1. z\n"

    assert validate_analysis(parse_analysis(base + "КОД: OrderTest.java:2 — ok\n"), tmp_path) == []
    too_far = validate_analysis(parse_analysis(base + "КОД: OrderTest.java:9 — ok\n"), tmp_path)
    assert too_far and "вне файла" in too_far[0]
    ambiguous = validate_analysis(parse_analysis(base + "КОД: Dup.java:1 — ok\n"), tmp_path)
    assert ambiguous and "нескольких файлах" in ambiguous[0]
    missing = validate_analysis(parse_analysis(base + "КОД: Nope.java:1 — ok\n"), tmp_path)
    assert missing and "не найден" in missing[0]


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


def test_code_line_must_exist(tmp_path: Path) -> None:
    (tmp_path / "Test.java").write_text("line1\nline2\nline3\n", encoding="utf-8")
    base = "ЧТО СЛОМАЛОСЬ: x\nПРИЧИНА: тест — y\nКАК ИСПРАВИТЬ:\n1. z\n"

    for ok_line in ("Test.java:1", "Test.java:3", "Test.java"):
        assert validate_analysis(parse_analysis(base + f"КОД: {ok_line} — ok\n"), tmp_path) == []
    for bad_line in ("Test.java:999", "Test.java:0"):
        errors = validate_analysis(parse_analysis(base + f"КОД: {bad_line} — битая\n"), tmp_path)
        assert errors and "вне файла «Test.java» (в файле 3 строк)" in errors[0]


def test_compact_keeps_cause_sentence_and_first_step() -> None:
    analysis = parse_analysis(
        "ЧТО СЛОМАЛОСЬ: Первое.\nВторое.\nПРИЧИНА: данные —\nнет клиента\n"
        "КАК ИСПРАВИТЬ:\n1) Создать клиента в стенде.\n2. Повторить.\n"
    )
    assert analysis.compact() == (
        "ПРИЧИНА: данные — нет клиента\n"
        "ЧТО СЛОМАЛОСЬ: Первое.\n"
        "ПЕРВЫЙ ШАГ ИСПРАВЛЕНИЯ: Создать клиента в стенде."
    )


# --- отбор лога -------------------------------------------------------------


def _error_block(minute: int, text: str, trace_lines: int = 3) -> str:
    lines = [f"2026-09-01 10:{minute:02d}:00 [ERROR] {text}"]
    lines += [f"\tat ru.company.Service.method{i}(Service.java:{i})" for i in range(trace_lines)]
    return "\n".join(lines)


def test_focus_log_keeps_short_log() -> None:
    log = "--- [файл: app.log] ---\n" + _error_block(0, "boom")
    assert focus_log(log, "boom", 8000) == log


def test_focus_log_prefers_related_block_and_marks_gaps() -> None:
    noise = "\n\n".join(_error_block(i, f"Scheduler tick {i} slow", 10) for i in range(40))
    related = _error_block(50, "OrderService failed rqUID=0f8a1c2e-1b2c-4d5e-8f90-123456789abc")
    other = "--- [файл: audit.log] ---\n" + "\n\n".join(
        _error_block(i, f"Audit rotate {i}", 10) for i in range(20)
    )
    log = f"--- [файл: app.log] ---\n{noise}\n\n{related}\n\n{other}"
    error_text = "Order not created\nrqUID=0f8a1c2e-1b2c-4d5e-8f90-123456789abc"

    focused = focus_log(log, error_text, 3000)

    assert len(focused) <= 3000
    assert focused.startswith(FOCUS_NOTE)
    assert "10:50:00 [ERROR] OrderService failed rqUID=0f8a1c2e" in focused  # ID и время целы
    assert "[… пропущено блоков:" in focused
    assert focused.index("Scheduler tick 0 ") < focused.index("OrderService failed")  # порядок
    assert "--- [файл: audit.log] ---" in focused


def test_focus_log_marks_attachment_that_did_not_fit() -> None:
    first = "--- [файл: app.log] ---\n" + "\n\n".join(
        _error_block(i, f"Payment declined for order {i}", 5) for i in range(30)
    )
    second = "--- [HTTP: response.json] ---\n" + "\n\n".join(
        "\n".join(f"HTTP/1.1 500 unrelated body {i}.{j} with some padding" for j in range(10))
        for i in range(20)
    )
    focused = focus_log(f"{first}\n\n{second}", "Payment declined order", 2500)

    assert len(focused) <= 2500
    assert "--- [HTTP: response.json] ---\n[вложение не вошло в лимит задания: 200 строк]" in focused


def test_focus_log_without_overlap_keeps_earliest_blocks() -> None:
    log = "--- [файл: app.log] ---\n" + "\n\n".join(
        _error_block(i, f"NullPointerException customer {i}", 5) for i in range(60)
    )
    focused = focus_log(log, "expected: <200> but was: <500>", 2000)

    assert "10:00:00 [ERROR] NullPointerException customer 0" in focused
    assert "10:59:00" not in focused
    assert len(focused) <= 2000


def test_focus_log_tiny_budget_keeps_start_of_best_block() -> None:
    log = "--- [файл: app.log] ---\n" + _error_block(1, "OrderService customer is null", 5)
    focused = focus_log(log, "OrderService customer", 130)

    assert len(focused) <= 130
    assert "[ERROR] OrderService" in focused


def _journal(error_line: str | None) -> str:
    lines = [f'  {{"level": "INFO", "msg": "heartbeat {i}"}},' for i in range(300)]
    if error_line:
        lines.append(error_line)
    lines += [f'  {{"level": "INFO", "msg": "heartbeat tail {i}"}},' for i in range(300)]
    return "--- [журнал: journal.json] ---\n[\n" + "\n".join(lines) + "\n]"


def test_focus_log_without_overlap_keeps_application_errors() -> None:
    error = '  {"level": "ERROR", "msg": "NullPointerException in OrderService"},'
    focused = focus_log(_journal(error), "expected: <200> but was: <500>", 3000)

    assert "NullPointerException in OrderService" in focused  # раньше оставался только «[»
    assert "heartbeat 299" in focused  # контекст вокруг ошибки
    assert len(focused) <= 3000


def test_focus_log_without_any_signal_keeps_block_head() -> None:
    focused = focus_log(_journal(None), "expected: <200> but was: <500>", 3000)

    assert "heartbeat 0" in focused and "heartbeat 10" in focused
    assert focused.rstrip().endswith("[…]") and len(focused) <= 3000


def test_focus_log_prefers_error_blocks_over_noise_without_overlap() -> None:
    noise = "--- [HTTP: response.json] ---\n" + "\n\n".join(
        "\n".join(f"HTTP/1.1 200 OK body {i}.{j} padding padding" for j in range(8)) for i in range(15)
    )
    errors = "--- [файл: app.log] ---\n" + _error_block(30, "Database pool exhausted", 3)
    focused = focus_log(f"{noise}\n\n{errors}", "expected: <200> but was: <500>", 1500)

    assert "Database pool exhausted" in focused


def test_focus_log_keeps_fragment_of_huge_error_line() -> None:
    line = (
        "2026-09-01 10:00:01 [ERROR] OrderController: request failed payload="
        + "x" * 6000 + " OrderService: customer is null " + "y" * 3000
    )
    focused = focus_log("--- [файл: app.log] ---\n" + line, "OrderService customer", 8000)

    assert len(focused) <= 8000
    assert "2026-09-01 10:00:01 [ERROR] OrderController" in focused  # начало строки
    assert "OrderService: customer is null" in focused  # окно вокруг совпадения
    assert " … " in focused and focused.rstrip().endswith("…")


@pytest.mark.parametrize("budget", [100, 150, 300])  # 100 — минимум ALLURE_LLM_PROMPT_LOG_MAX_CHARS
def test_focus_log_keeps_error_line_fragment_on_small_budget(budget: int) -> None:
    line = (
        "2026-09-01 10:00:01 [ERROR] payload=" + "x" * 6000
        + " OrderService: customer is null " + "y" * 3000
    )
    focused = focus_log("--- [файл: app.log] ---\n" + line, "OrderService customer", budget)

    assert len(focused) <= budget
    assert "OrderService: customer is null" in focused
    assert "[…]" not in focused


def test_focus_log_keeps_head_of_huge_line_without_overlap() -> None:
    line = "2026-09-01 10:00:01 [ERROR] Gateway timeout " + "z" * 9000
    focused = focus_log("--- [файл: app.log] ---\n" + line, "expected: <200> but was: <500>", 8000)

    assert "[ERROR] Gateway timeout" in focused and len(focused) <= 8000
    assert focused.count("z") > 3000


def test_focus_log_shrinks_huge_block_to_matching_lines() -> None:
    journal = "\n".join(
        [f'  {{"level": "INFO", "msg": "heartbeat {i}"}},' for i in range(500)]
        + ['  {"level": "ERROR", "msg": "OrderService customer is null"},']
        + [f'  {{"level": "INFO", "msg": "heartbeat tail {i}"}},' for i in range(500)]
    )
    log = f"--- [журнал: journal.json] ---\n[\n{journal}\n]"
    focused = focus_log(log, "OrderService customer", 3000)

    assert len(focused) <= 3000
    assert "OrderService customer is null" in focused
    assert "heartbeat 498" in focused  # контекст вокруг совпадения
    assert "[…]" in focused


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


def test_caused_by_without_project_frames_is_kept_but_has_no_files() -> None:
    # Поздний «Caused by» в трейсе фреймворка может быть единственным указанием на причину:
    # его сохраняем, но открывать по нему нечего.
    from alla_skill_lib.cluster_task import has_frame_files
    trace = (
        "java.lang.IllegalStateException: boom\n"
        "\tat org.junit.Assert.fail(Assert.java:89)\n"
        "Caused by: java.net.ConnectException: Connection refused\n"
        "\tat java.base/java.net.Socket.connect(Socket.java:1)\n"
    )
    frames = project_frames(trace)
    assert frames == ["Caused by: java.net.ConnectException: Connection refused"]
    assert not has_frame_files(frames)


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


def test_clustering_gate_settings_reach_the_clustering_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alla_core.models.testops import TriageReport
    from alla_core.services import clustering_service
    from alla_skill_lib import pipeline
    from skill_factories import make_failed_test_summary

    settings = Settings.load(None, environ={
        "ALLURE_ENDPOINT": "https://a.example", "ALLURE_TOKEN": "t",
        "ALLURE_CLUSTERING_RESOURCE_GATE": "false",
        "ALLURE_CLUSTERING_LOG_SPLIT_THRESHOLD": "0.25"})
    seen: list[clustering_service.ClusteringConfig] = []
    real = clustering_service.ClusteringService.__init__

    def spy(self: clustering_service.ClusteringService,
            config: clustering_service.ClusteringConfig | None = None) -> None:
        assert config is not None
        seen.append(config)
        real(self, config)

    monkeypatch.setattr(pipeline.ClusteringService, "__init__", spy)
    triage = TriageReport(launch_id=1, total_results=1, failed_count=1, broken_count=0,
                          failed_tests=[make_failed_test_summary(status_message="boom")])
    pipeline.cluster_failures(1, triage, settings)
    assert (seen[0].resource_gate, seen[0].log_split_threshold) == (False, 0.25)


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
        (
            {"ALLURE_ENDPOINT": "https://a", "ALLURE_TOKEN": "t",
             "ALLURE_CLUSTERING_LOG_SPLIT_THRESHOLD": "-0.1"},
            "ALLURE_CLUSTERING_LOG_SPLIT_THRESHOLD: допустимо от 0.0 до 1.0",
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

    assert result.returncode == 0
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


def test_cli_does_not_load_clustering_libraries() -> None:
    """next/verify/apply не платят за numpy/scipy/sklearn: их грузит только prepare."""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    code = (
        "import sys; import alla_skill_lib.cli; "
        "print(sorted(m for m in ('numpy', 'scipy', 'sklearn') if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(scripts)},
        check=True,
    )
    assert result.stdout.strip() == "[]"



@pytest.mark.parametrize(("frame", "openable"), [
    ("at ru.company.orders.OrderTest.createOrder(OrderTest.java:6)", True),
    ("at createOrder (/ci/build/tests/orders.spec.ts:12:3)", True),
    ("at run (/ci/build/tests/My Orders.spec.ts:12:3)", True),  # пробел в имени (ревью)
    ("at /ci/build/tests/orders.spec.ts:12:3", True),
    ('File "/ci/build/tests/api/test_orders.py", line 12, in test_create', True),
    ("at ru.company.Job.run(Unknown Source)", False),
    ("at ru.company.Job.run(Native Method)", False),
    ("Caused by: java.net.ConnectException: Connection refused", False),
])
def test_has_frame_files_needs_a_file_position(frame: str, openable: bool) -> None:
    from alla_skill_lib.cluster_task import has_frame_files
    assert has_frame_files([frame]) is openable



def test_bare_js_frame_reaches_the_task_and_jdk_modules_do_not() -> None:
    # Кадр JS без имени функции отбрасывался как «модуль JDK» из-за «/», и поздний кадр за
    # пределами обрезанного трейса пропадал из задания (ревью).
    from alla_skill_lib.cluster_task import has_frame_files
    trace = (
        "Error: expected 200 but got 500\n"
        + "".join(f"    at node:internal/process/task_queues:{i}:5\n" for i in range(30))
        + "    at java.base/java.lang.Thread.run(Thread.java:833)\n"
        + "    at /ci/build/tests/orders.spec.ts:12:3\n"
    )
    frames = project_frames(trace)
    assert frames == ["at /ci/build/tests/orders.spec.ts:12:3"]
    assert has_frame_files(frames)



@pytest.mark.parametrize(("frame", "kept"), [
    ("at java.base/java.lang.Thread.run(Thread.java:833)", False),
    ("at javafx.graphics@21.0.1/com.sun.javafx.application.LauncherImpl"
     ".launchApplication1(LauncherImpl.java:651)", False),  # модуль с версией (ревью)
    ("at com.foo.loader/foo@9.2/com.foo.Main.run(Main.java:101)", True),
    ("at app//ru.company.orders.OrderTest.createOrder(OrderTest.java:6)", True),
    ("at ru.company.orders.OrderTest.createOrder(OrderTest.java:6)", True),
    ("at /ci/build/tests/orders.spec.ts:12:3", True),
])
def test_java_frames_are_judged_by_declaring_class(frame: str, kept: bool) -> None:
    # Формат StackTraceElement.toString(): загрузчик и модуль@версия перед классом.
    assert (project_frames(f"Error\n    {frame}\n") == [frame.strip()]) is kept
