"""Сквозной сценарий: prepare → разборы кластеров → summary → done."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fixtures import (  # noqa: F401
    multimodule_project_fixture,
    nested_project_fixture,
    project_fixture,
    testops_fixture,
    without_libmagic,
)
from skill_fake_testops import TOKEN, FakeTestOps, default_launch, green_launch

from alla_skill_lib import cli

VALID_ANALYSIS = (
    "ЧТО СЛОМАЛОСЬ: Тест получил HTTP 500 вместо 200. В логе приложения: "
    "«java.lang.NullPointerException: customer is null».\n"
    "\n"
    "ПРИЧИНА: приложение — OrderService.create падает на пустом customer.\n"
    "\n"
    "КАК ИСПРАВИТЬ:\n"
    "1. Добавить проверку customer в OrderService.create.\n"
    "2. Вернуть 400 при пустом customer.\n"
    "\n"
    "КОД: src/test/java/ru/company/orders/OrderTest.java:6 — тест ждёт 200\n"
)
MARKDOWN_ANALYSIS = (
    "### **Что сломалось:**\n"
    "Сервис авторизации недоступен: Connection refused.\n"
    "\n"
    "**ПРИЧИНА:** Окружение — auth-service:8080 не принимает соединения.\n"
    "\n"
    "**Как исправить:**\n"
    "1. Поднять auth-service на стенде.\n"
)


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli.main(argv)
    return code, capsys.readouterr().out


def _prepare(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    launch_id: int = 777,
) -> tuple[Path, dict, str]:
    code, out = _run(["prepare", str(launch_id), "--project-root", str(project)], capsys)
    assert code == 0, out
    run_dir = Path(next(line for line in out.splitlines() if line.startswith("Папка разбора:"))
                   .split(":", 1)[1].strip())
    return run_dir, json.loads((run_dir / "run.json").read_text(encoding="utf-8")), out


def _next(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> str:
    code, out = _run(["next", str(run_dir)], capsys)
    assert code == 0, out
    return out


def _full_report(run_dir: Path) -> str:
    """Полный разбор (report.md): в терминал идёт краткий, подробности — только здесь."""
    return (run_dir / "report.md").read_text(encoding="utf-8")


def test_prepare_builds_run(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, run, out = _prepare(project, capsys)

    assert out.startswith("STATUS: analyze")
    assert run_dir.parent == project / "alla-reports"
    assert (project / "alla-reports" / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")
    assert run["counts"] == {
        "total": 7, "passed": 1, "failed": 4, "broken": 1, "skipped": 1,
        "unknown": 0, "muted_failures": 1, "active_failures": 4,
    }
    sizes = [entry["member_count"] for entry in run["clusters"]]
    assert sizes == sorted(sizes, reverse=True) and sizes[0] == 2 and sum(sizes) == 4

    first = (run_dir / "clusters" / "01.md").read_text(encoding="utf-8")
    assert "Кластер 1 из 3" in first
    assert str(run_dir / "analyses" / "01.md") in first
    assert "недоверенный" in first
    assert "customer is null" in first  # лог из вложения
    assert "2026-09-01 10:00:01 [ERROR] OrderService" in first  # время не стёрто
    assert "<TS>" not in first and "<ID>" not in first
    assert "«неизвестно» — только если ни одну из четырёх остальных нельзя обосновать" in first
    assert "ru.company.orders.OrderTest.createOrder" in first
    assert "at ru.company.orders.OrderTest.createOrder(OrderTest.java:6)" in first
    assert "org.junit.Assert" not in first.split("--- Кадры стека из кода проекта ---")[1]
    assert "src/test/java/ru/company/orders/OrderTest.java:5 — код теста" in first
    assert "КОД: <путь от корня проекта>:<строка>" in first
    assert "testresult/101" not in first  # ссылки на тесты модели не нужны: их строит отчёт
    assert "| шаг:" not in first  # шаг теста уже назван в данных кластера

    auto = [entry for entry in run["clusters"] if entry["auto"]]
    assert len(auto) == 1
    assert "неизвестно" in (run_dir / "analyses" / f"{auto[0]['file_id']}.md").read_text(encoding="utf-8")
    assert not (run_dir / "clusters" / f"{auto[0]['file_id']}.md").exists()


def test_prepare_is_read_only_and_hides_token(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, _, out = _prepare(project, capsys)

    methods = {(method, path) for method, path in testops.requests if method != "GET"}
    assert methods == {("POST", "/api/uaa/oauth/token")}
    assert TOKEN not in out
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(encoding="utf-8"), path


def test_full_flow_until_done(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    pending = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    assert len(pending) == 2
    order_id, login_id = pending

    (run_dir / "analyses" / f"{order_id}.md").write_text(VALID_ANALYSIS, encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: analyze")
    assert f"clusters/{login_id}.md" in out

    (run_dir / "analyses" / f"{login_id}.md").write_text(MARKDOWN_ANALYSIS, encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: summary")
    task = (run_dir / "summary_task.md").read_text(encoding="utf-8")
    assert "OrderService.create падает на пустом customer" in task
    assert "ПЕРВЫЙ ШАГ ИСПРАВЛЕНИЯ: Добавить проверку customer в OrderService.create." in task
    assert "Вернуть 400" not in task  # в сводку идут сжатые разборы
    assert "КОД:" not in task
    assert str(run_dir / "summary.md") in task

    (run_dir / "summary.md").write_text(
        "Упало 4 теста, выявлено 3 проблемы. Главная — NPE в OrderService.", encoding="utf-8"
    )
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done")
    brief = out.split("===ОТЧЁТ===\n", 1)[1].split("\n===КОНЕЦ===", 1)[0]
    assert "Разбор прогона #777 — Regression nightly" in brief
    assert "Главная — NPE в OrderService." in brief
    # В терминале — кратко: у каждой проблемы что случилось, что думает агент и что делать.
    assert "- **Проблема 1** · 2 теста, напр. " in brief
    assert "Агент считает: возможная ошибка приложения" in brief
    assert "Агент считает: проблема стенда или окружения" in brief
    assert "Агент считает: причина не ясна" in brief
    assert "  Что делать: Добавить проверку customer в OrderService.create." in brief
    assert "### Требуют вашего внимания (2)" in brief
    assert brief.index("Требуют вашего внимания (2)") < brief.index("Стенд и тестовые данные (1)")
    assert brief.index("возможная ошибка приложения") < brief.index("причина не ясна")
    assert "По категориям" not in brief and "[приложение]" not in brief
    assert "testresult" not in brief and "### Что делать" not in brief  # подробности — в файле
    # И ссылка на файл с полным разбором (кликабельная и обычный путь).
    link = (run_dir / "report.md").absolute()
    assert f"[report.md]({link.as_uri()})" in brief and brief.rstrip().endswith(f"Файл: {link}")
    report = _full_report(run_dir)
    # Шапка и «Коротко» в файле те же; ниже файл содержит тексты целиком и все тесты.
    assert report.startswith(brief.split("\n\n### Требуют вашего внимания", 1)[0])
    assert "### Что делать" not in report and "## Подробности по проблемам" in report
    assert "- Агент считает: возможная ошибка приложения — " in report
    assert "[silent](https://testops.example/launch/777/testresult/108)" in report
    assert "- **Ошибка в TestOps:** expected: <200> but was: <500>" in report
    assert "[createOrder](https://testops.example/launch/777/testresult/101)" in report

    # Повторный next без аргумента берёт последний разбор и ничего не ломает.
    code, again = _run(["next", "--project-root", str(project)], capsys)
    assert code == 0 and again.startswith("STATUS: done")


def test_fix_loop_counts_distinct_attempts(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    file_id = run["clusters"][0]["file_id"]
    analysis = run_dir / "analyses" / f"{file_id}.md"

    analysis.write_text("ЧТО СЛОМАЛОСЬ: что-то\nКАК ИСПРАВИТЬ:\n1. шаг\n", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: fix") and "попытка 1 из 3" in out
    assert "нет раздела «ПРИЧИНА:»" in out
    assert "Разобрано: ЧТО СЛОМАЛОСЬ ✓, ПРИЧИНА ✗, КАК ИСПРАВИТЬ ✓" in out
    again = _next(run_dir, capsys)  # та же версия файла — не новая попытка, но с предупреждением
    assert "попытка 1 из 3" in again and "Файл не изменился с прошлого вызова next" in again

    analysis.write_text(VALID_ANALYSIS.replace("OrderTest.java:6", "Missing.java:1")
                        .replace("src/test/java/ru/company/orders/", ""), encoding="utf-8")
    out = _next(run_dir, capsys)
    assert "попытка 2 из 3" in out and "«Missing.java» не найден" in out

    analysis.write_text("ПРИЧИНА: баг\nсовсем без формата", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: analyze")  # третья неудача принята с пометкой

    for entry in run["clusters"]:
        path = run_dir / "analyses" / f"{entry['file_id']}.md"
        if not path.exists():
            path.write_text(MARKDOWN_ANALYSIS, encoding="utf-8")
    (run_dir / "summary.md").write_text("Итог.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done")
    assert "причина не ясна: разбор не прошёл проверку формата, текст — в report.md" in out
    report = _full_report(run_dir)
    assert "Разбор не прошёл проверку формата, его текст — в подробностях ниже." in report
    assert "ПРИЧИНА: баг\nсовсем без формата" in report


def test_green_launch_is_done_immediately(project: Path, monkeypatch, capsys) -> None:
    FakeTestOps(green_launch()).install(monkeypatch)
    code, out = _run(["prepare", "778", "--project-root", str(project)], capsys)
    assert code == 0
    assert out.startswith("STATUS: done")
    assert "Упавших тестов нет — разбирать нечего." in out


def test_auth_failure_reports_error(project: Path, monkeypatch, capsys) -> None:
    FakeTestOps(default_launch(), auth_status=401).install(monkeypatch)
    code, out = _run(["prepare", "777", "--project-root", str(project)], capsys)
    assert code == 1
    assert out.startswith("STATUS: error")
    assert "HTTP 401" in out and "Traceback" not in out and TOKEN not in out
    assert not (project / "alla-reports").exists()


def test_missing_config_reports_error(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("ALLURE_TOKEN")
    code, out = _run(["prepare", "777", "--project-root", str(project)], capsys)
    assert code == 2
    assert out.startswith("STATUS: error") and "ALLURE_TOKEN" in out


def test_next_without_runs(project: Path, capsys) -> None:
    code, out = _run(["next", "--project-root", str(project)], capsys)
    assert code == 1
    assert out.startswith("STATUS: error") and "prepare" in out


# --- правки автотестов, обратная связь, история ---------------------------------

TEST_ANALYSIS = (
    "ЧТО СЛОМАЛОСЬ: Тест ждёт код 200, а API создания заказа теперь отвечает 201.\n"
    "ПРИЧИНА: тест — ожидаемый код ответа устарел.\n"
    "КАК ИСПРАВИТЬ:\n1. Ожидать 201 Created в OrderTest.createOrder.\n"
    "КОД: src/test/java/ru/company/orders/OrderTest.java:6 — assertEquals(200, …)\n"
)
PROPOSAL = (
    "РЕШЕНИЕ: исправить\n"
    "ФАЙЛ: src/test/java/ru/company/orders/OrderTest.java:6\n"
    "БЫЛО:\n        assertEquals(200, api.create().status());\n"
    "СТАЛО:\n        assertEquals(201, api.create().status());\n"
    "ПОЧЕМУ: API создания заказа по контракту возвращает 201 Created\n"
)
FEEDBACK = (
    "НАЗВАНИЕ: NPE в OrderService при пустом customer\n"
    "ПРИЧИНА: приложение — сервис заказов не проверяет customer\n"
    "КАК ИСПРАВИТЬ:\n1. Передавать customer в запросе создания заказа\n"
)


def _finish(run_dir: Path, capsys: pytest.CaptureFixture[str], analyses: dict[str, str]) -> str:
    for file_id, text in analyses.items():
        (run_dir / "analyses" / f"{file_id}.md").write_text(text, encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: summary"), out
    (run_dir / "summary.md").write_text("Итог прогона.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done"), out
    return out


def test_test_cluster_gets_fix_proposal_and_apply(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    test_file = project / "src/test/java/ru/company/orders/OrderTest.java"

    (run_dir / "analyses" / f"{order}.md").write_text(TEST_ANALYSIS, encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: propose")
    assert str(run_dir / "proposals" / f"{order}.md") in out and "НЕ меняй" in out

    proposal = run_dir / "proposals" / f"{order}.md"
    proposal.write_text(PROPOSAL.replace("assertEquals(200", "assertEqual(200", 1), encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: fix") and "не найдены" in out
    assert "6:         assertEquals(200, api.create().status());" in out  # настоящие строки

    proposal.write_text(PROPOSAL, encoding="utf-8")
    out = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    assert "### Агент может поправить сам (1)" in out
    assert (
        "  Агент считает: ошибка в автотесте — API создания заказа по контракту возвращает "
        "201 Created\n"
        "  Правка: `src/test/java/ru/company/orders/OrderTest.java:6` (ждёт вашего «да»)" in out
    )
    assert "apply 01 --run" in out and "--yes" in out
    report = _full_report(run_dir)
    assert "- Агент считает: ошибка в автотесте — API создания заказа" in report
    assert "- Статус: ждёт вашего «да»" in report

    code, diff = _run(["apply", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and diff.startswith("STATUS: diff")
    assert "+        assertEquals(201, api.create().status());" in diff
    assert "Проверь: меняется ожидаемое значение в проверке (200 → 201)" in diff
    assert "assertEquals(200" in test_file.read_text(encoding="utf-8")
    assert (run_dir / "proposals" / f"{order}.patch").is_file()
    confirm = next(line for line in diff.splitlines() if "--yes --diff" in line)
    diff_hash = confirm.split("--diff ")[1].split()[0]

    # --yes без хэша показанного diff не пишет в файл, а показывает diff снова.
    code, out = _run(["apply", "1", "--run", str(run_dir), "--yes"], capsys)
    assert code == 0 and out.startswith("STATUS: diff") and "Хэш --diff не совпал" in out
    assert "assertEquals(200" in test_file.read_text(encoding="utf-8")

    code, out = _run(["apply", "1", "--run", str(run_dir), "--yes", "--diff", diff_hash], capsys)
    assert code == 0 and out.startswith("STATUS: applied") and "Откатить:" in out
    assert "assertEquals(201" in test_file.read_text(encoding="utf-8")
    assert (run_dir / "proposals" / f"{order}.applied.json").is_file()
    assert (run_dir / "proposals" / f"{order}.orig").is_file()
    assert "(уже применено)" in _next(run_dir, capsys)
    assert "- Статус: уже применено" in _full_report(run_dir)

    code, out = _run(["apply", "1", "--run", str(run_dir), "--yes", "--diff", diff_hash], capsys)
    assert code == 0 and "уже применена" in out

    code, out = _run(["revert", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: reverted")
    assert "assertEquals(200" in test_file.read_text(encoding="utf-8")
    assert "(уже применено)" not in _next(run_dir, capsys)
    assert "- Статус: уже применено" not in _full_report(run_dir)
    code, out = _run(["revert", "1", "--run", str(run_dir)], capsys)
    assert code == 1 and out.startswith("STATUS: error") and "откатывать нечего" in out
    assert test_file.read_text(encoding="utf-8").count("assertEquals(201") == 0


def _test_cluster_without_fix(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[Path, str, str]:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    (run_dir / "analyses" / f"{order}.md").write_text(TEST_ANALYSIS, encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: propose")
    return run_dir, order, login


def test_report_says_why_agent_declined_to_fix(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, order, login = _test_cluster_without_fix(project, capsys)
    (run_dir / "proposals" / f"{order}.md").write_text(
        "РЕШЕНИЕ: не трогать\nПОЧЕМУ: ожидаемый код задан в общем контракте\n", encoding="utf-8"
    )
    out = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    assert "### Автотест сломан, но править вручную (1)" in out
    assert "### Агент может поправить сам" not in out and "apply 01" not in out
    assert (
        "- Почему агент не правил сам: агент решил не трогать код: ожидаемый код задан"
        in _full_report(run_dir)
    )


def test_report_says_why_proposal_was_dropped(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, order, login = _test_cluster_without_fix(project, capsys)
    proposal = run_dir / "proposals" / f"{order}.md"
    for typo in ("assertEqual(200", "assertEqua(200", "assertEq(200"):
        proposal.write_text(PROPOSAL.replace("assertEquals(200", typo, 1), encoding="utf-8")
        out = _next(run_dir, capsys)
    assert out.startswith("STATUS: analyze")  # третья неудача: предложение отброшено
    out = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    assert "### Замечания" not in out  # причина видна прямо у проблемы в файле
    assert (
        "- Почему агент не правил сам: предложение правки не прошло проверку за 3 попытки"
        in _full_report(run_dir)
    )


def test_report_says_when_proposal_limit_is_reached(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli, "MAX_PROPOSALS", 0)
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    (run_dir / "analyses" / f"{order}.md").write_text(TEST_ANALYSIS, encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: analyze")  # правку не предлагали — сразу к следующему
    out = _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    assert "### Автотест сломан, но править вручную (1)" in out
    assert (
        "- Почему агент не правил сам: лимит — не больше 0 предложений правок"
        in _full_report(run_dir)
    )


def test_feedback_is_remembered_and_recognized_next_launch(
    project: Path, testops: FakeTestOps, monkeypatch, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    out = _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: MARKDOWN_ANALYSIS})
    assert "Обратная связь:" in out and "remember" in out
    history = (project / "alla-reports" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history) == 2  # кластер без данных в историю не пишется
    _next(run_dir, capsys)
    assert len((project / "alla-reports" / "history.jsonl").read_text(encoding="utf-8").splitlines()) == 2

    feedback = run_dir / "feedback" / f"{order}.md"
    feedback.write_text(FEEDBACK.replace("приложение", "неизвестно"), encoding="utf-8")
    code, out = _run(["remember", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: fix") and "«неизвестно» запоминать нельзя" in out

    feedback.write_text(FEEDBACK + "ПРИЗНАК: Payment gateway declined\n", encoding="utf-8")
    code, out = _run(["remember", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and "строки признака нет в данных кластера" in out

    feedback.write_text(FEEDBACK, encoding="utf-8")
    code, out = _run(["remember", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    [kb_file] = (project / "alla-kb").glob("*.json")
    entry_id = kb_file.stem
    data = json.loads(kb_file.read_text(encoding="utf-8"))
    order_entry = next(e for e in run["clusters"] if e["file_id"] == order)
    assert data["category"] == "service"
    assert data["confirmed_signatures"] == [order_entry["signature"]]
    assert data["error_example"] == order_entry["fingerprint"]
    assert data["resolution_steps"] == ["Передавать customer в запросе создания заказа"]
    assert (project / "alla-kb" / "README.md").is_file()

    code, out = _run(["remember", "1", "--run", str(run_dir)], capsys)
    assert code == 1 and out.startswith("STATUS: error")
    assert "уже подтверждали" in out and f"--entry {entry_id}" in out
    assert "--from-analysis" not in out  # источник — feedback/NN.md, как и в исходной команде

    # Уточнение записи с ошибкой: секрет в рецепте не сохраняется, а повтор
    # предлагается с тем же --entry, чтобы обновить именно эту запись.
    feedback.write_text(FEEDBACK + '2. Взять "password": "hunter2" из vault\n', encoding="utf-8")
    code, out = _run(["remember", "1", "--entry", entry_id, "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: fix")
    assert "«КАК ИСПРАВИТЬ:» строка похожа на секрет" in out
    assert f"remember 01 --entry {entry_id} --run" in out
    assert "hunter2" not in kb_file.read_text(encoding="utf-8")
    feedback.write_text(FEEDBACK, encoding="utf-8")

    # Следующий прогон с теми же падениями: ошибка узнаётся точно и видна как повтор.
    FakeTestOps(default_launch(778)).install(monkeypatch)
    run_dir2, run2, _ = _prepare(project, capsys, launch_id=778)
    order2 = next(e for e in run2["clusters"] if e["file_id"] == order)
    assert order2["kb"][0]["id"] == entry_id and order2["kb"][0]["origin"] == "exact"
    assert order2["history"]["launches"] == 1
    task = (run_dir2 / "clusters" / f"{order}.md").read_text(encoding="utf-8")
    assert "ТОЧНОЕ" in task and entry_id in task and "БАЗА ЗНАНИЙ:" in task

    (run_dir2 / "analyses" / f"{order}.md").write_text(
        VALID_ANALYSIS + "БАЗА ЗНАНИЙ: unknown_entry\n", encoding="utf-8"
    )
    assert "не предлагалась" in _next(run_dir2, capsys)
    out = _finish(run_dir2, capsys, {
        order: VALID_ANALYSIS + f"БАЗА ЗНАНИЙ: {entry_id}\n", login: MARKDOWN_ANALYSIS,
    })
    assert f"известная проблема: {entry_id}" in out
    assert "повторяется (уже была в 1 другом прогоне)" in out
    report2 = _full_report(run_dir2)
    assert f"- Известная проблема: {entry_id}" in report2
    assert "- Повторяется: уже была в 1 другом прогоне, впервые" in report2

    # Пользователь сказал, что запись здесь ни при чём — в следующий раз её нет.
    code, out = _run(["reject", "1", entry_id, "--run", str(run_dir2)], capsys)
    assert code == 0 and out.startswith("STATUS: saved")
    FakeTestOps(default_launch(779)).install(monkeypatch)
    _, run3, _ = _prepare(project, capsys, launch_id=779)
    assert next(e for e in run3["clusters"] if e["file_id"] == order)["kb"] == []


def test_update_suggestions_keep_the_source_the_user_chose(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    """«Чтобы обновить запись» не должно подменять подтверждённый разбор старым feedback/NN.md."""
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: MARKDOWN_ANALYSIS})
    base = ["remember", "1", "--run", str(run_dir), "--from-analysis"]
    stale = run_dir / "feedback" / f"{order}.md"
    stale.write_text(FEEDBACK, encoding="utf-8")  # старый файл: он не должен победить

    code, out = _run(base, capsys)
    assert code == 0 and out.startswith("STATUS: saved") and "feedback" in out, out
    [kb_file] = (project / "alla-kb").glob("*.json")
    entry_id = kb_file.stem
    assert "OrderService.create падает" in json.loads(kb_file.read_text(encoding="utf-8"))["description"]

    # Ошибку уже подтверждали: предложенная команда обновления сохраняет --from-analysis.
    code, out = _run(base, capsys)
    suggested = next(line for line in out.splitlines() if "уже подтверждали" in line)
    assert code == 1 and f"--entry {entry_id}" in suggested and suggested.endswith("--from-analysis")

    # Запись с таким id уже есть, но сигнатура не подтверждена: то же самое.
    data = json.loads(kb_file.read_text(encoding="utf-8"))
    data["confirmed_signatures"] = []
    kb_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    code, out = _run(base, capsys)
    suggested = next(line for line in out.splitlines() if "уже есть" in line)
    assert code == 1 and f"--entry {entry_id}" in suggested and suggested.endswith("--from-analysis")

    # Предложенная команда обновляет запись по разбору, а не по старому feedback/NN.md.
    code, out = _run(["remember", "1", "--entry", entry_id, *base[2:]], capsys)
    assert code == 0 and out.startswith("STATUS: saved") and "запись обновлена" in out, out
    updated = json.loads(kb_file.read_text(encoding="utf-8"))
    assert "OrderService.create падает" in updated["description"]
    assert "сервис заказов не проверяет customer" not in updated["description"]


def test_feedback_commands_require_run_and_explicit_analysis_confirmation(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    out = _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: MARKDOWN_ANALYSIS})
    assert f"remember N --run {run_dir}" in out  # готовые команды именно этого разбора

    # Без --run команда не угадывает разбор по .last_run.
    for argv in (["remember", "3"], ["reject", "3", "some_id"], ["apply", "1"]):
        with pytest.raises(SystemExit) as raised:
            cli.main([*argv, "--project-root", str(project)])
        assert raised.value.code == 2
        printed = capsys.readouterr().out
        assert printed.startswith("STATUS: error") and "--run" in printed

    # Нет файла обратной связи — разбор модели не сохраняется «молча».
    code, out = _run(["remember", "3", "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: fix") and "--from-analysis" in out
    assert not (project / "alla-kb").exists()

    # Старый файл обратной связи противоречит подтверждённому разбору — флаг
    # явно выбирает разбор.
    (run_dir / "feedback" / f"{login}.md").write_text(
        "НАЗВАНИЕ: Старая версия\nПРИЧИНА: данные — устаревшая причина\n"
        "КАК ИСПРАВИТЬ:\n1. Устаревший рецепт\n",
        encoding="utf-8",
    )
    code, out = _run(["remember", "3", "--run", str(run_dir), "--from-analysis"], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    assert "не использован" in out
    [kb_file] = (project / "alla-kb").glob("*.json")
    saved = json.loads(kb_file.read_text(encoding="utf-8"))
    assert saved["category"] == "env"
    assert saved["resolution_steps"] == ["Поднять auth-service на стенде."]


# --- база знаний по модулям --------------------------------------------------------


MODULE_ANALYSIS = VALID_ANALYSIS.replace("src/test/java", "orders/src/test/java")


def _entry_by_module(run: dict, module: str) -> dict:
    return next(entry for entry in run["clusters"] if entry["module"] == module and not entry["auto"])


def test_kb_stays_in_the_root_for_a_single_module_project(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    _, run, _ = _prepare(project, capsys)

    assert {entry["module"] for entry in run["clusters"]} == {""}
    assert {entry["kb_dir"] for entry in run["clusters"]} == {str(project / "alla-kb")}


def test_kb_is_kept_per_module_and_clusters_do_not_see_other_modules(
    multimodule_project: Path, testops: FakeTestOps, capsys
) -> None:
    project = multimodule_project
    run_dir, run, _ = _prepare(project, capsys)
    orders, auth = _entry_by_module(run, "orders"), _entry_by_module(run, "auth")
    assert orders["kb_dir"] == str(project / "orders" / "alla-kb")
    assert auth["kb_dir"] == str(project / "auth" / "alla-kb")
    # Тест без данных об ошибке в прогоне из двух модулей — в общей базе корня.
    [silent] = [entry for entry in run["clusters"] if entry["auto"]]
    assert silent["module"] == "" and silent["kb_dir"] == str(project / "alla-kb")

    _finish(run_dir, capsys, {orders["file_id"]: MODULE_ANALYSIS, auth["file_id"]: MARKDOWN_ANALYSIS})
    (run_dir / "feedback" / f"{orders['file_id']}.md").write_text(FEEDBACK, encoding="utf-8")
    code, out = _run(["remember", orders["file_id"], "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    assert "orders/alla-kb/" in out
    [kb_file] = (project / "orders" / "alla-kb").glob("*.json")
    assert (project / "orders" / "alla-kb" / "README.md").is_file()
    assert not (project / "alla-kb").exists() and not (project / "auth" / "alla-kb").exists()
    entry_id = kb_file.stem

    # Тот же модуль в следующем прогоне запись находит, а повтор по истории виден.
    testops.fixture = default_launch(779)
    _, run2, _ = _prepare(project, capsys, launch_id=779)
    again = _entry_by_module(run2, "orders")
    assert [match["id"] for match in again["kb"]] == [entry_id]
    assert again["history"]["launches"] == 1

    # Та же ошибка (то же сообщение, трейс и лог), но тест из другого модуля:
    # запись orders ей не подсказывается и повтором из orders не считается.
    moved = default_launch(780)
    for result in moved.results:
        if result["id"] in (101, 102):
            result["fullName"] = "ru.company.auth.LoginTest.login"
    testops.fixture = moved
    _, run3, _ = _prepare(project, capsys, launch_id=780)
    foreign = next(e for e in run3["clusters"] if e["signature"] == orders["signature"])
    assert foreign["module"] == "auth" and foreign["kb_dir"] == auth["kb_dir"]
    assert foreign["kb"] == [] and foreign["history"] is None


def test_reject_works_in_the_module_of_the_cluster(
    multimodule_project: Path, testops: FakeTestOps, capsys
) -> None:
    project = multimodule_project
    run_dir, run, _ = _prepare(project, capsys)
    orders = _entry_by_module(run, "orders")
    (run_dir / "feedback" / f"{orders['file_id']}.md").write_text(FEEDBACK, encoding="utf-8")
    code, out = _run(["remember", orders["file_id"], "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    [kb_file] = (project / "orders" / "alla-kb").glob("*.json")

    code, out = _run(["reject", orders["file_id"], kb_file.stem, "--run", str(run_dir)], capsys)

    assert code == 0 and out.startswith("STATUS: saved") and "orders/alla-kb/" in out, out
    assert json.loads(kb_file.read_text(encoding="utf-8"))["rejected_signatures"] == [orders["signature"]]


def test_unknown_module_takes_the_run_module_or_the_root(
    multimodule_project: Path, testops: FakeTestOps, capsys
) -> None:
    unresolved = default_launch()
    for result in unresolved.results:
        if result["id"] == 103:  # сценарий без исходника: модуль по имени не найти
            result["fullName"] = "Scenario: user logs in"
    unresolved.results = [r for r in unresolved.results if r["id"] != 108]
    testops.fixture = unresolved

    # В прогоне определился один модуль — безымянный тест относится к нему.
    _, run, _ = _prepare(multimodule_project, capsys)
    assert {entry["module"] for entry in run["clusters"]} == {"orders"}
    assert {entry["kb_dir"] for entry in run["clusters"]} == {str(multimodule_project / "orders" / "alla-kb")}

    # Определились два модуля — безымянному остаётся общая база корня.
    mixed = default_launch(779)
    for result in mixed.results:
        if result["id"] == 108:
            result["fullName"] = "Scenario: nothing to see"
    testops.fixture = mixed
    _, run, _ = _prepare(multimodule_project, capsys, launch_id=779)
    silent = next(entry for entry in run["clusters"] if entry["auto"])
    assert silent["module"] == "" and silent["kb_dir"] == str(multimodule_project / "alla-kb")


def test_old_run_without_module_info_uses_the_common_knowledge_base(
    multimodule_project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, run, _ = _prepare(multimodule_project, capsys)
    orders = _entry_by_module(run, "orders")
    for entry in run["clusters"]:  # разбор, подготовленный версией скилла без модулей
        entry.pop("module"), entry.pop("kb_dir")
    (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    (run_dir / "feedback" / f"{orders['file_id']}.md").write_text(FEEDBACK, encoding="utf-8")

    code, out = _run(["remember", orders["file_id"], "--run", str(run_dir)], capsys)

    assert code == 0 and out.startswith("STATUS: saved"), out
    assert len(list((multimodule_project / "alla-kb").glob("*.json"))) == 1
    assert not (multimodule_project / "orders" / "alla-kb").exists()


def test_check_lists_every_knowledge_base(
    multimodule_project: Path, testops: FakeTestOps, capsys
) -> None:
    root = multimodule_project.resolve()
    (root / "orders" / "alla-kb").mkdir()

    code, out = _run(["check", "--project-root", str(root)], capsys)

    assert code == 0 and out.startswith("STATUS: ready"), out
    assert f"База знаний: {root / 'alla-kb'} — записей 0" in out
    assert f"База знаний: {root / 'orders' / 'alla-kb'} — записей 0" in out


NESTED_ANALYSIS = VALID_ANALYSIS.replace("src/test/java", "autotests/src/test/java")


def test_single_project_in_a_nested_folder_keeps_the_root_knowledge_base(
    nested_project: Path, testops: FakeTestOps, capsys
) -> None:
    repo = nested_project
    run_dir, run, _ = _prepare(repo, capsys)
    # ``autotests/pom.xml`` — не повод переезжать: иначе прежние записи и история пропадут.
    assert {entry["module"] for entry in run["clusters"]} == {""}
    assert {entry["kb_dir"] for entry in run["clusters"]} == {str(repo / "alla-kb")}

    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    _finish(run_dir, capsys, {order: NESTED_ANALYSIS, login: MARKDOWN_ANALYSIS})
    (run_dir / "feedback" / f"{order}.md").write_text(FEEDBACK, encoding="utf-8")
    code, out = _run(["remember", order, "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    assert len(list((repo / "alla-kb").glob("*.json"))) == 1
    assert not (repo / "autotests" / "alla-kb").exists()

    testops.fixture = default_launch(779)
    _, run2, _ = _prepare(repo, capsys, launch_id=779)
    again = next(entry for entry in run2["clusters"] if entry["signature"] == run["clusters"][0]["signature"])
    assert again["kb"] and again["history"]["launches"] == 1


def test_same_class_in_two_modules_goes_to_the_root_knowledge_base(
    multimodule_project: Path, testops: FakeTestOps, capsys
) -> None:
    project = multimodule_project
    # ``OrderTest`` есть и в ``orders``, и в ``legacy``: по имени теста модуль не определить,
    # а единственный определившийся модуль прогона (``auth``) среди кандидатов не значится.
    _write_module_file(
        project / "legacy" / "src" / "test" / "java" / "ru" / "company" / "orders" / "OrderTest.java"
    )
    _write_module_file(project / "legacy" / "pom.xml")

    _, run, _ = _prepare(project, capsys)

    order = next(entry for entry in run["clusters"] if entry["member_count"] == 2)
    assert order["module"] == "" and order["kb_dir"] == str(project / "alla-kb")
    assert _entry_by_module(run, "auth")["kb_dir"] == str(project / "auth" / "alla-kb")


def _write_module_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<project/>\n" if path.suffix == ".xml" else "class OrderTest {}\n", encoding="utf-8")
