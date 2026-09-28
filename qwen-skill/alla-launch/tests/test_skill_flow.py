"""Сквозной сценарий: prepare → разборы кластеров → summary → done."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
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
    assert "данные / неизвестно («неизвестно» — только если" in first
    assert "ru.company.orders.OrderTest.createOrder" in first
    assert "at ru.company.orders.OrderTest.createOrder(OrderTest.java:6)" in first
    assert "org.junit.Assert" not in first.split("--- Кадры стека из кода проекта ---")[1]
    assert "src/test/java/ru/company/orders/OrderTest.java:5 — код теста" in first
    assert "КОД: <путь относительно корня проекта>" in first

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
    assert "[приложение] expected: <200> but was: <500> — 2 теста" in brief
    assert "[окружение]" in brief
    assert "[неизвестно] silent — нет данных об ошибке — 1 тест" in brief
    assert "По категориям:" in brief
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert report.startswith(brief.rsplit("\n\nПолный отчёт:", 1)[0])
    assert "## Детали кластеров" in report
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
    assert _next(run_dir, capsys) == out  # та же версия файла не считается новой попыткой

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
    assert "(формат разбора нарушен)" in out


def test_green_launch_is_done_immediately(project: Path, monkeypatch, capsys) -> None:
    FakeTestOps(green_launch()).install(monkeypatch)
    code, out = _run(["prepare", "778", "--project-root", str(project)], capsys)
    assert code == 0
    assert out.startswith("STATUS: done")
    assert "Активных падений нет" in out


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
    assert "### Можно исправить в автотестах" in out
    assert "1. src/test/java/ru/company/orders/OrderTest.java:6 — API создания заказа" in out
    assert "apply 01 --run" in out and "--yes" in out

    code, diff = _run(["apply", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and diff.startswith("STATUS: diff")
    assert "+        assertEquals(201, api.create().status());" in diff
    assert "assertEquals(200" in test_file.read_text(encoding="utf-8")

    code, out = _run(["apply", "1", "--run", str(run_dir), "--yes"], capsys)
    assert code == 0 and out.startswith("STATUS: applied")
    assert "assertEquals(201" in test_file.read_text(encoding="utf-8")
    assert "(уже применено)" in _next(run_dir, capsys)


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
    assert code == 1 and out.startswith("STATUS: fix") and "«неизвестно» запоминать нельзя" in out

    feedback.write_text(FEEDBACK + "ПРИЗНАК: Payment gateway declined\n", encoding="utf-8")
    code, out = _run(["remember", "1", "--run", str(run_dir)], capsys)
    assert code == 1 and "строки признака нет в данных кластера" in out

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
    assert code == 1 and "уже подтверждали" in out and f"--entry {entry_id}" in out

    # Уточнение записи с ошибкой: секрет в рецепте не сохраняется, а повтор
    # предлагается с тем же --entry, чтобы обновить именно эту запись.
    feedback.write_text(FEEDBACK + '2. Взять "password": "hunter2" из vault\n', encoding="utf-8")
    code, out = _run(["remember", "1", "--entry", entry_id, "--run", str(run_dir)], capsys)
    assert code == 1 and out.startswith("STATUS: fix")
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
    assert f"· известная: {entry_id}" in out and "· повтор: 1 прогон с" in out

    # Пользователь сказал, что запись здесь ни при чём — в следующий раз её нет.
    code, out = _run(["reject", "1", entry_id, "--run", str(run_dir2)], capsys)
    assert code == 0 and out.startswith("STATUS: saved")
    FakeTestOps(default_launch(779)).install(monkeypatch)
    _, run3, _ = _prepare(project, capsys, launch_id=779)
    assert next(e for e in run3["clusters"] if e["file_id"] == order)["kb"] == []


def test_schema_1_run_still_reaches_done(project: Path, testops: FakeTestOps, capsys) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    run["schema"] = 1
    (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]

    out = _finish(run_dir, capsys, {order: TEST_ANALYSIS, login: MARKDOWN_ANALYSIS})
    assert "Обратная связь:" not in out and "Можно исправить" not in out
    assert not (project / "alla-reports" / "history.jsonl").exists()


def test_feedback_commands_require_run_and_explicit_analysis_confirmation(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    out = _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: MARKDOWN_ANALYSIS})
    assert f"remember N --run {run_dir}" in out  # готовые команды именно этого разбора

    # Без --run команда не угадывает разбор по .last_run.
    for argv in (["remember", "3"], ["reject", "3", "some_id"], ["apply", "1"]):
        with pytest.raises(SystemExit):
            cli.main([*argv, "--project-root", str(project)])
    capsys.readouterr()

    # Нет файла обратной связи — разбор модели не сохраняется «молча».
    code, out = _run(["remember", "3", "--run", str(run_dir)], capsys)
    assert code == 1 and out.startswith("STATUS: fix") and "--from-analysis" in out
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
