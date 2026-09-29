"""Итоговый отчёт: разделы «внимание / агент / вручную / стенд», простой язык, потолки."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from skill_fixtures import without_libmagic  # noqa: F401

from alla_core.models.clustering import ClusterSignature, ClusteringReport, FailureCluster
from alla_core.models import common
from alla_core.models.testops import FailedTestSummary, TriageReport
from alla_skill_lib import report
from alla_skill_lib.analysis_format import ClusterAnalysis, parse_analysis
from alla_skill_lib.proposals import Proposal
from alla_skill_lib.report import (
    MAX_ITEMS_PER_SECTION,
    MAX_SUMMARY_DETAILED,
    build_summary_task,
    render_green_report,
    render_report,
)
from alla_skill_lib.workspace import RunPaths

APP = (
    "ЧТО СЛОМАЛОСЬ: Заказ не создаётся, сервер отвечает 500.\n"
    "ПРИЧИНА: приложение — NPE в OrderService при пустом customer.\n"
    "КАК ИСПРАВИТЬ:\n1. Добавить проверку customer.\n2. Вернуть 400.\n"
)
TEST = (
    "ЧТО СЛОМАЛОСЬ: Тест ждёт код 200, а сервис отвечает 201.\n"
    "ПРИЧИНА: тест — ожидаемый код устарел.\n"
    "КАК ИСПРАВИТЬ:\n1. Ожидать 201.\n"
    "КОД: src/OrderTest.java:6 — assertEquals(200, …)\n"
)
TEST_NO_CODE = (
    "ЧТО СЛОМАЛОСЬ: Тест не находит кнопку.\n"
    "ПРИЧИНА: тест — локатор устарел.\n"
    "КАК ИСПРАВИТЬ:\n1. Обновить локатор.\n"
)
ENV = (
    "ЧТО СЛОМАЛОСЬ: Сервис авторизации недоступен.\n"
    "ПРИЧИНА: окружение — auth-service не отвечает.\n"
    "КАК ИСПРАВИТЬ:\n1. Поднять auth-service.\n"
)
DATA = (
    "ЧТО СЛОМАЛОСЬ: Нет тестового пользователя.\n"
    "ПРИЧИНА: данные — пользователь удалён.\n"
    "КАК ИСПРАВИТЬ:\n1. Создать пользователя.\n"
)
UNKNOWN = (
    "ЧТО СЛОМАЛОСЬ: Тест упал без внятной причины.\n"
    "ПРИЧИНА: неизвестно — данных мало.\n"
    "КАК ИСПРАВИТЬ:\n1. Посмотреть лог вручную.\n"
)


def _proposal(decision: str = "fix", why: str = "Контракт API изменился") -> Proposal:
    return Proposal(
        decision=decision,
        file="src/OrderTest.java",
        line=6,
        before=["assertEquals(200, status);"],
        after=["assertEquals(201, status);"],
        why=why,
    )


def _run(sizes: list[int], *, muted: int = 0, warnings: list[str] | None = None) -> dict[str, Any]:
    """Прогон с проблемами заданных размеров; проблема i имеет file_id «i» (две цифры)."""
    tests: list[FailedTestSummary] = []
    clusters: list[FailureCluster] = []
    next_id = 1
    for position, size in enumerate(sizes, start=1):
        members = []
        for _ in range(size):
            tests.append(FailedTestSummary(
                test_result_id=next_id,
                name=f"test_{next_id}",
                status=common.TestStatus.FAILED,
                link=f"https://testops.example/testresult/{next_id}",
            ))
            members.append(next_id)
            next_id += 1
        clusters.append(FailureCluster(
            cluster_id=f"c{position}",
            label=f"error {position}",
            signature=ClusterSignature(),
            member_test_ids=members,
            member_count=size,
            representative_test_id=members[0],
        ))
    failed = sum(sizes) + muted
    triage = TriageReport(
        launch_id=5,
        launch_name="Nightly",
        total_results=failed + 10,
        passed_count=10,
        failed_count=failed,
        muted_failure_count=muted,
        failed_tests=tests,
    )
    clustering = ClusteringReport(
        launch_id=5, total_failures=sum(sizes), cluster_count=len(sizes), clusters=clusters
    )
    return {
        "launch_id": 5,
        "launch_name": "Nightly",
        "launch_url": "https://testops.example/launch/5",
        "counts": {
            "total": failed + 10, "passed": 10, "failed": failed, "broken": 0,
            "skipped": 0, "unknown": 0, "muted_failures": muted, "active_failures": sum(sizes),
        },
        "warnings": warnings or [],
        "clusters": [
            {
                "file_id": str(position).zfill(2),
                "cluster_id": f"c{position}",
                "label": f"error {position}",
                "member_count": size,
                "auto": False,
                "kb": [],
                "history": None,
            }
            for position, size in enumerate(sizes, start=1)
        ],
        "triage": triage.model_dump(mode="json"),
        "clustering": clustering.model_dump(mode="json"),
    }


def _render(
    tmp_path: Path,
    run: dict[str, Any],
    texts: list[str],
    **kwargs: Any,
) -> tuple[str, str]:
    analyses = {
        entry["file_id"]: parse_analysis(text) for entry, text in zip(run["clusters"], texts)
    }
    flagged = kwargs.pop("flagged", set())
    summary = kwargs.pop("summary", "Итог прогона.")
    return render_report(run, analyses, flagged, summary, RunPaths(tmp_path), **kwargs)


def _section(console: str, title: str) -> str:
    start = console.index(f"### {title}")
    following = re.search(r"\n### |\nОбратная связь:", console[start + 4:])
    end = start + 4 + following.start() if following else len(console)
    return console[start:end]


# --- разделы ------------------------------------------------------------------------


def test_problems_are_grouped_by_who_acts(tmp_path: Path) -> None:
    run = _run([5, 4, 3, 2, 1])
    console, full = _render(
        tmp_path, run, [APP, TEST, ENV, DATA, UNKNOWN],
        proposals={"02": _proposal()}, applied=set(),
    )

    order = [
        "### Требуют вашего внимания (2)",
        "### Агент может поправить сам (1)",
        "### Стенд и тестовые данные (2)",
    ]
    positions = [console.index(title) for title in order]
    assert positions == sorted(positions)
    attention = _section(console, "Требуют вашего внимания")
    assert "**Проблема 1** — 5 тестов · возможная ошибка приложения" in attention
    assert "**Проблема 5** — 1 тест · причина не ясна" in attention
    assert attention.index("Проблема 1") < attention.index("Проблема 5")
    agent = _section(console, "Агент может поправить сам")
    assert "**Проблема 2** — `src/OrderTest.java:6` · 4 теста" in agent
    assert "- Почему это ошибка теста: Контракт API изменился" in agent
    assert "- Статус: ждёт вашего «да»" in agent
    environment = _section(console, "Стенд и тестовые данные")
    assert "**Проблема 3** — 3 теста · проблема стенда или окружения" in environment
    assert "**Проблема 4** — 2 теста · проблема с тестовыми данными" in environment
    assert "Автотест сломан, но править вручную" not in console  # пустых разделов нет

    overview = _section(console, "Что делать")
    assert "- Посмотреть самим: 2 проблемы (6 тестов) — проблемы 1, 5" in overview
    assert "- Агент поправит автотесты: 1 проблема (4 теста) — проблема 2" in overview
    assert full.startswith(console.rsplit("\n\nПолный отчёт:", 1)[0])


def test_attention_lists_app_bugs_before_unknown_even_when_smaller(tmp_path: Path) -> None:
    run = _run([9, 1])
    console, _ = _render(tmp_path, run, [UNKNOWN, APP])
    attention = _section(console, "Требуют вашего внимания")
    assert attention.index("Проблема 2") < attention.index("Проблема 1")


def test_manual_test_fix_explains_why_agent_did_not_fix(tmp_path: Path) -> None:
    run = _run([4, 3, 2, 1])
    console, _ = _render(
        tmp_path, run, [TEST, TEST, TEST, TEST_NO_CODE],
        proposals={"01": _proposal("skip", "нужен доступ к боевому стенду")},
        not_proposed={"03": "лимит — не больше 5 предложений правок на один разбор"},
    )
    manual = _section(console, "Автотест сломан, но править вручную")
    assert "агент решил не трогать код: нужен доступ к боевому стенду" in manual
    assert "лимит — не больше 5 предложений правок на один разбор" in manual
    assert "нет ссылки на строку кода автотеста" in manual  # проблема 4: нет КОД
    assert "Агент может поправить сам" not in console


def test_full_report_keeps_reason_for_problems_beyond_the_console_cap(tmp_path: Path) -> None:
    count = MAX_ITEMS_PER_SECTION + 3
    run = _run([1] * count)
    long_why = "нужен доступ к боевому стенду, " * 15
    console, full = _render(
        tmp_path, run, [TEST] * count,
        proposals={"09": _proposal("skip", long_why)},
        not_proposed={"10": "лимит — не больше 5 предложений правок на один разбор",
                      "11": "предложение правки не прошло проверку за 3 попытки и отброшено"},
    )
    manual = _section(console, "Автотест сломан, но править вручную")
    assert "**Проблема 9**" not in manual  # в консоли — только первые пункты
    details = full.split("## Подробности по проблемам", 1)[1]
    for number, reason in (
        ("09", f"агент решил не трогать код: {long_why.strip()}"),
        ("10", "лимит — не больше 5 предложений правок на один разбор"),
        ("11", "предложение правки не прошло проверку за 3 попытки и отброшено"),
    ):
        block = details.split(f"### Проблема {int(number)} ", 1)[1].split("### Проблема", 1)[0]
        assert f"- **Почему агент не правил сам:** {reason}" in block
    assert details.count("Почему агент не правил сам") == count  # у каждой проблемы раздела


def test_applied_fix_is_marked(tmp_path: Path) -> None:
    run = _run([2])
    console, full = _render(tmp_path, run, [TEST], proposals={"01": _proposal()}, applied={"01"})
    assert "- Статус: уже применено — запустите тест заново" in console
    assert "### Проблема 1: src/OrderTest.java:6 — уже применено" in full


def test_weakening_warning_is_visible_next_to_the_fix(tmp_path: Path) -> None:
    run = _run([2])
    console, _ = _render(tmp_path, run, [TEST], proposals={"01": _proposal()})
    assert "- Обратите внимание: меняется ожидаемое значение в проверке (200 → 201)" in console


def test_flagged_problem_needs_attention_and_shows_raw_start(tmp_path: Path) -> None:
    run = _run([2])
    console, full = _render(
        tmp_path, run, ["ПРИЧИНА: тест — совсем без формата"], flagged={"01"}
    )
    attention = _section(console, "Требуют вашего внимания")
    assert "причина не ясна — разбор не прошёл проверку формата" in attention
    assert "вот его начало: ПРИЧИНА: тест — совсем без формата" in attention
    assert "Что делать" not in attention
    assert "совсем без формата" in full


def test_history_and_known_problem_are_visible(tmp_path: Path) -> None:
    run = _run([2])
    run["clusters"][0]["history"] = {"launches": 3, "first_date": "2026-09-12", "last_date": "2026-09-28"}
    console, _ = _render(tmp_path, run, [APP + "БАЗА ЗНАНИЙ: order_npe\n"])
    assert "- Повторяется: уже была в 3 других прогонах, впервые 12.09.2026" in console
    assert "- Известная проблема: order_npe (есть в базе знаний проекта)" in console


# --- простой язык -------------------------------------------------------------------


def test_report_uses_plain_words(tmp_path: Path) -> None:
    run = _run([3, 2], muted=1, warnings=["данные могли не выгрузиться полностью"])
    console, _ = _render(tmp_path, run, [APP, ENV])
    assert "Всего тестов: 16 — прошло 10, упало 6 (failed 6, broken 0), пропущено 0." in console
    assert "В разборе: 5 упавших тестов → 2 проблемы (ещё 1 отключённый muted-тест не учитываем)." in console
    assert "Внимание: данные могли не выгрузиться полностью" in console
    for jargon in ("кластер", "Кластер", "Активных падений", "По категориям", "сигнатур"):
        assert jargon not in console
    # Без эмодзи и значков: отчёт читается в терминале любого вида.
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", console)


def test_green_report_is_plain(tmp_path: Path) -> None:
    run = _run([])
    console, text = render_green_report(run, RunPaths(tmp_path))
    assert "Упавших тестов нет — разбирать нечего." in console and "В разборе" not in text
    run["counts"]["muted_failures"] = 2
    assert "кроме отключённых (muted): 2" in render_green_report(run, RunPaths(tmp_path))[0]


# --- потолки ------------------------------------------------------------------------


def test_section_shows_limited_items_and_counts_the_rest(tmp_path: Path) -> None:
    count = MAX_ITEMS_PER_SECTION + 3
    run = _run([1] * count)
    console, full = _render(tmp_path, run, [APP] * count)
    attention = _section(console, "Требуют вашего внимания")
    assert f"### Требуют вашего внимания ({count})" in attention
    assert attention.count("**Проблема ") == MAX_ITEMS_PER_SECTION
    assert "… и ещё 3 (проблемы 9, 10, 11) — подробности в полном отчёте." in attention
    assert full.count("### Проблема ") == count  # в подробностях — все
    assert len(console) < 8_000


def test_console_lists_at_most_two_tests_per_problem(tmp_path: Path) -> None:
    run = _run([6])
    console, full = _render(tmp_path, run, [APP])
    assert "- Тесты: [test_1](https://testops.example/testresult/1), [test_2]" in console
    assert "и ещё 4" in console and "test_3" not in console
    assert full.count("   - [test_") == 5 and "   - … и ещё 1" in full  # в файле — до пяти


# --- общий анализ и контекст --------------------------------------------------------


def test_summary_task_replaces_server_instruction(tmp_path: Path) -> None:
    run = _run([3, 2])
    analyses = {"01": parse_analysis(APP), "02": parse_analysis(ENV)}
    task = build_summary_task(run, analyses, set(), RunPaths(tmp_path))
    assert "ПЕРВЫЙ ШАГ ИСПРАВЛЕНИЯ: Добавить проверку customer." in task
    assert "Напиши итог по прогону:" in task
    assert "Ключевые проблемы" not in task  # серверное задание заменено
    assert "Без жаргона" in task


def test_summary_task_stays_small_with_many_problems(tmp_path: Path) -> None:
    count = 120
    run = _run([1] * count)
    long_cause = "приложение — " + "очень длинная причина " * 40
    text = f"ЧТО СЛОМАЛОСЬ: {'Что-то упало. ' * 30}\nПРИЧИНА: {long_cause}\nКАК ИСПРАВИТЬ:\n1. {'Шаг ' * 100}\n"
    analyses = {entry["file_id"]: parse_analysis(text) for entry in run["clusters"]}
    for entry in run["clusters"]:
        entry["label"] = "длинная подпись " * 30
    for cluster in run["clustering"]["clusters"]:
        cluster["label"] = "длинная подпись " * 30
        cluster["example_message"] = "сырое сообщение " * 100
    task = build_summary_task(run, analyses, set(), RunPaths(tmp_path))

    assert task.count("ПЕРВЫЙ ШАГ ИСПРАВЛЕНИЯ:") == MAX_SUMMARY_DETAILED
    assert task.count("--- Проблема ") == count
    assert "сырое сообщение" not in task  # у каждой проблемы есть краткий разбор
    assert len(task) < 60_000


def test_compact_truncates_long_fields() -> None:
    analysis = parse_analysis(
        f"ЧТО СЛОМАЛОСЬ: {'Слово ' * 100}.\nПРИЧИНА: тест — {'причина ' * 100}\n"
        f"КАК ИСПРАВИТЬ:\n1. {'шаг ' * 100}\n"
    )
    lines = analysis.compact().splitlines()
    assert len(lines) == 3 and all(len(line) < 300 for line in lines)
    assert all(line.endswith("…") for line in lines)


def test_what_first_sentence() -> None:
    analysis = ClusterAnalysis(raw="", what="Тест упал. Причина в другом.")
    assert analysis.what_first_sentence() == "Тест упал."
    assert report.CATEGORY_LABELS.keys() == {"тест", "приложение", "окружение", "данные", "неизвестно"}
