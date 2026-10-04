"""Итоговый отчёт: краткий разбор в терминале и полный report.md по ссылке."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from skill_fixtures import without_libmagic  # noqa: F401

from alla_core.models.clustering import ClusterSignature, ClusteringReport, FailureCluster
from alla_core.models import common
from alla_core.models.testops import FailedTestSummary, TriageReport
from alla_skill_lib import report
from alla_skill_lib.analysis_format import ClusterAnalysis, parse_analysis
from alla_skill_lib.proposals import Proposal
from alla_skill_lib.report import (
    BRIEF_TEXT_CHARS,
    MAX_BRIEF_ITEMS,
    MAX_SUMMARY_DETAILED,
    MAX_SUMMARY_LISTED,
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
    """Раздел отчёта до следующего заголовка; в терминале перед названием — значок раздела."""
    found = re.search(rf"^### (?:{_ICONS} )?{re.escape(title)}", console, re.MULTILINE)
    assert found, title
    start = found.start()
    following = re.search(r"\n### |\n---\n|\nОбратная связь:", console[start + 4:])
    end = start + 4 + following.start() if following else len(console)
    return console[start:end]


_ICONS = "[🔴🟢🟡🔵]"


# --- краткий разбор в терминале -----------------------------------------------------


def test_terminal_brief_says_what_broke_what_the_agent_thinks_and_what_to_do(tmp_path: Path) -> None:
    run = _run([5, 4, 3, 2, 1])
    console, _ = _render(
        tmp_path, run, [APP, TEST, ENV, DATA, UNKNOWN],
        proposals={"02": _proposal()}, apply_states={"02": "not_applied"},
    )

    order = [
        "### 🔴 Требуют вашего внимания (2)",
        "### 🟢 Агент может поправить сам (1)",
        "### 🔵 Стенд и тестовые данные (2)",
    ]
    positions = [console.index(title) for title in order]
    assert positions == sorted(positions)
    attention = _section(console, "Требуют вашего внимания")
    assert (
        "**Проблема 1** · 5 тестов\n"
        "Заказ не создаётся, сервер отвечает 500.\n"
        "   Агент считает: [ПРИЛОЖЕНИЕ] NPE в OrderService при пустом customer.\n"
        "   Что делать:    Добавить проверку customer.\n"
        "   Например:      test_1\n"
    ) in attention
    assert (
        "**Проблема 5** · 1 тест\n"
        "Тест упал без внятной причины.\n"
        "   Агент считает: [НЕ ЯСНО] данных мало.\n"
        "   Что делать:    Посмотреть лог вручную.\n"
        "   Тест:          test_15\n"
    ) in attention + "\n"
    assert attention.index("Проблема 1") < attention.index("Проблема 5")
    agent = _section(console, "Агент может поправить сам")
    assert (
        "**Проблема 2** · 4 теста\n"
        "Тест ждёт код 200, а сервис отвечает 201.\n"
        "   Агент считает: [АВТОТЕСТ] Контракт API изменился\n"
        "   Правка:        `src/OrderTest.java:6` — ждёт вашего «да»\n"
        "   Проверьте:     меняется ожидаемое значение в проверке (200 → 201)"
    ) in agent
    environment = _section(console, "Стенд и тестовые данные")
    assert "   Агент считает: [СТЕНД] auth-service не отвечает." in environment
    assert "   Что делать:    Поднять auth-service." in environment
    assert "   Агент считает: [ДАННЫЕ] пользователь удалён." in environment
    assert "Автотест сломан, но править вручную" not in console  # пустых разделов нет

    # Кратко: у каждой проблемы своё мнение агента; подробности (все шаги, тесты) — в файле.
    assert console.count("**Проблема ") == 5
    assert console.count("   Агент считает: ") == 5
    for detail in ("Что случилось:", "Почему:", "- Тесты:", "testresult", "### Что делать"):
        assert detail not in console


def test_terminal_header_shows_pass_bar_and_section_overview(tmp_path: Path) -> None:
    run = _run([5, 4, 3, 2, 1])
    console, _ = _render(
        tmp_path, run, [APP, TEST, ENV, DATA, UNKNOWN],
        proposals={"02": _proposal()}, apply_states={"02": "not_applied"},
    )
    assert console.startswith(
        "## Разбор прогона #5 — Nightly\n\n"
        "[####------] 25 тестов: прошло 10, упало 15\n"
        "15 упавших тестов -> 5 проблем:\n"
        "   🔴 требуют вашего внимания .... 2 (6 тестов)\n"
        "   🟢 агент поправит сам ......... 1 (4 теста)\n"
        "   🔵 стенд и данные ............. 2 (5 тестов)\n"
        "TestOps: https://testops.example/launch/5\n"
    )


def test_pass_bar_is_empty_or_full_only_when_all_tests_failed_or_passed() -> None:
    assert report._pass_bar(0, 7) == "[----------]"
    assert report._pass_bar(1, 100) == "[#---------]"  # прошёл хоть один — не пусто
    assert report._pass_bar(99, 100) == "[#########-]"  # упал хоть один — не полно
    assert report._pass_bar(5, 5) == "[##########]"


def test_terminal_header_lists_only_nonzero_counters(tmp_path: Path) -> None:
    run = _run([2])
    run["counts"].update(broken=1, failed=1, skipped=3, unknown=2, total=18)
    console, full = _render(tmp_path, run, [APP])
    assert "18 тестов: прошло 10, упало 2 (из них broken 1), пропущено 3, статус не определён 2\n" in console
    assert "(failed 1, broken 1), пропущено 3, статус не определён 2." in full


def test_terminal_brief_explains_why_the_agent_left_a_test_fix_to_the_engineer(tmp_path: Path) -> None:
    run = _run([2, 1])
    console, _ = _render(
        tmp_path, run, [TEST, TEST_NO_CODE],
        proposals={"01": _proposal("skip", "нужен доступ к боевому стенду")},
    )
    manual = _section(console, "Автотест сломан, но править вручную")
    assert "   Агент считает: [АВТОТЕСТ] ожидаемый код устарел." in manual
    assert "   Почему не сам: агент решил не трогать код: нужен доступ к боевому стенду" in manual
    assert "   Почему не сам: в разборе нет ссылки на строку кода автотеста" in manual


def test_terminal_brief_ends_with_a_link_to_the_full_report(tmp_path: Path) -> None:
    folder = tmp_path / "alla reports" / "777-20260929"  # пробел в пути — как в реальных папках
    folder.mkdir(parents=True)
    run = _run([2])
    console, full = _render(folder, run, [APP])
    report_path = (folder / "report.md").absolute()
    assert console.rstrip().endswith(f"Файл: {report_path}")
    assert f"[report.md]({report_path.as_uri()})" in console
    assert "%20" in report_path.as_uri()  # ссылка кликабельна и с пробелом в пути
    assert f"Полный разбор: [report.md]({report_path.as_uri()})" in console
    assert "file://" not in full  # в самом файле ссылка на себя не нужна
    green, _ = render_green_report(_run([]), RunPaths(folder))
    assert f"[report.md]({report_path.as_uri()})" in green


def test_terminal_brief_lists_limited_items_per_section(tmp_path: Path) -> None:
    count = MAX_BRIEF_ITEMS + 6
    run = _run([1] * count)
    console, _ = _render(tmp_path, run, [APP] * count)
    attention = _section(console, "Требуют вашего внимания")
    assert f"### 🔴 Требуют вашего внимания ({count})" in attention
    assert attention.count("**Проблема ") == MAX_BRIEF_ITEMS
    assert (
        "- … и ещё 6 проблем (6 тестов) — полный список в report.md, "
        "раздел «Требуют вашего внимания»" in attention
    )
    assert "проблемы 6" not in attention  # голый список номеров ничего не говорит
    assert len(console) < 4_000


def test_terminal_brief_stays_small_on_a_huge_launch(tmp_path: Path) -> None:
    count = 80
    run = _run([2] * count)
    texts = [[APP, TEST, TEST_NO_CODE, ENV, DATA, UNKNOWN][i % 6] for i in range(count)]
    proposals = {f"{i + 1:02d}": _proposal() for i, text in enumerate(texts) if text is TEST}
    console, full = _render(tmp_path, run, texts, proposals=proposals)
    assert len(console) < 7_000 < len(full)


def test_terminal_brief_does_not_grow_with_the_number_of_problems(tmp_path: Path) -> None:
    count = 2_000
    run = _run([1] * count)
    console, full = _render(tmp_path, run, [APP] * count)
    attention = _section(console, "Требуют вашего внимания")
    assert attention.count("**Проблема ") == MAX_BRIEF_ITEMS
    assert (
        f"- … и ещё {count - MAX_BRIEF_ITEMS} проблем ({count - MAX_BRIEF_ITEMS} тестов) — "
        "полный список в report.md, раздел «Требуют вашего внимания»" in attention
    )
    assert "14, 15" not in attention  # номера не перечисляются
    assert len(console) < 2_500 < len(full)
    assert full.count("**Проблема ") == count  # в файле — каждая проблема


def test_terminal_brief_has_a_fixed_ceiling_even_with_long_texts_in_every_section(tmp_path: Path) -> None:
    """Каждое поле пункта обрезано, поэтому брифу, который повторяет каждый next, есть потолок."""
    text = "ЧТО СЛОМАЛОСЬ: {w}.\nПРИЧИНА: {c} — {w}.\nКАК ИСПРАВИТЬ:\n1. {w}.\n".replace("{w}", LONG.strip())
    categories = ["приложение", "тест", "окружение", "данные"]  # «тест» без КОД — «вручную»
    sizes = []
    for count in (40, 400):
        run = _run([1] * count)
        texts = [text.replace("{c}", categories[i % 4]) for i in range(count)]
        console, _ = _render(tmp_path, run, texts)
        assert console.count("**Проблема ") == 3 * MAX_BRIEF_ITEMS  # внимание, вручную, стенд
        sizes.append(len(console))
    assert max(sizes) < 16_000
    assert abs(sizes[0] - sizes[1]) < 300  # размер не зависит от числа проблем


def test_terminal_brief_limits_warnings_and_notes_but_file_keeps_them(tmp_path: Path) -> None:
    warnings = [f"Проблема {n}: задание не подготовлено — " + "причина " * 60 for n in range(1, 201)]
    run = _run([2], warnings=warnings)
    notes = ["Пропущено без разбора по просьбе пользователя: " + ", ".join(map(str, range(1, 400))) + "."]
    console, full = _render(tmp_path, run, [APP], notes=notes)

    assert console.count("(!) Проблема ") == report.MAX_BRIEF_NOTES
    assert "(!) … и ещё 195 предупреждений — в полном разборе" in console
    assert all(len(line) <= report.BRIEF_NOTE_CHARS + len("(!) ") for line in console.splitlines()
               if line.startswith("(!) "))
    notes_block = _section(console, "Замечания")
    assert len(notes_block) < report.BRIEF_NOTE_CHARS + 60 and notes_block.rstrip().endswith("…")
    assert len(console) < 4_000
    # В файле — ничего не потеряно.
    assert full.count("Внимание: ") == 200
    assert "395, 396, 397, 398, 399." in full


def test_long_first_sentence_is_clipped_in_terminal_but_full_in_file(tmp_path: Path) -> None:
    blob = '{"error":' + '"x' * 400 + '"}'  # без завершающей пунктуации — одно «предложение»
    text = f"ЧТО СЛОМАЛОСЬ: {blob}\nПРИЧИНА: приложение — сервер упал.\nКАК ИСПРАВИТЬ:\n1. Починить.\n"
    run = _run([1])
    console, full = _render(tmp_path, run, [text])
    lines = console.splitlines()
    line = lines[lines.index("**Проблема 1** · 1 тест") + 1]  # «что случилось» — под заголовком
    assert len(line) <= BRIEF_TEXT_CHARS and line.endswith("…") and line.startswith('{"error":')
    assert blob in full  # полный текст остаётся в report.md


# Как Qwen Code узнаёт блочную разметку в начале строки (его рендерер построчный): строка-
# ограда открывает блок кода до конца ответа, остальные меняют вид строки.
_QWEN_BLOCK_STARTS = (
    r"^ *(`{3,}|~{3,}) *([^`]*)$",  # ограда кода
    r"^ *#{1,4} +",  # заголовок
    r"^ *> ?",  # цитата
    r"^\s*\|(.+)\|\s*$",  # строка таблицы
    r"^ *([-*_] *){3,} *$",  # горизонтальная линия
    r"^ *\$\$ *$",  # формула
)


@pytest.mark.parametrize("what, shown", [
    ("\n```\nNPE в OrderService. Подробности ниже.\n```", "NPE в OrderService."),
    ("\n~~~\nNPE в OrderService.\n~~~", "NPE в OrderService."),
    (" # Заказ не создаётся.", "Заказ не создаётся."),
    (" > Сервер ответил 500.", "Сервер ответил 500."),
    (" | 500 | Internal Server Error |", "500 | Internal Server Error |"),
    (" - Заказ не создаётся.", "Заказ не создаётся."),
    (" 1. Заказ не создаётся.", "Заказ не создаётся."),
    (" ---", None),
])
def test_symptom_markdown_does_not_break_the_rest_of_the_brief(
    tmp_path: Path, what: str, shown: str | None
) -> None:
    """Симптом идёт строкой без подписи: блочная разметка модели в его начале (ограда кода,
    заголовок, цитата…) убирается, иначе Qwen показал бы остаток отчёта как код."""
    text = f"ЧТО СЛОМАЛОСЬ:{what}\nПРИЧИНА: приложение — NPE.\nКАК ИСПРАВИТЬ:\n1. Починить.\n"
    console, _ = _render(tmp_path, _run([2]), [text])
    lines = console.splitlines()
    head = lines.index("**Проблема 1** · 2 теста")
    if shown is None:  # от симптома ничего не осталось — строки нет
        assert lines[head + 1].startswith("   Агент считает: ")
    else:
        assert lines[head + 1] == shown
    for line in lines:
        if line.startswith(("### ", "## ")) or line == "---":
            continue  # заголовки и линии брифа — свои
        assert not any(re.match(pattern, line) for pattern in _QWEN_BLOCK_STARTS), line
    assert "Полный разбор: [report.md](" in console


def test_terminal_tags_show_repeats_and_known_problems(tmp_path: Path) -> None:
    run = _run([2])
    run["clusters"][0]["history"] = {"launches": 3, "first_date": "2026-09-12", "last_date": "2026-09-28"}
    console, full = _render(tmp_path, run, [APP + "БАЗА ЗНАНИЙ: order_npe\n"])
    line = next(line for line in console.splitlines() if line.startswith("**Проблема 1**"))
    assert line == (
        "**Проблема 1** · 2 теста · [повторяется: уже была в 3 других прогонах] "
        "· [известная проблема: order_npe]"
    )
    assert "- Повторяется: уже была в 3 других прогонах, впервые 12.09.2026" in full
    assert "- Известная проблема: order_npe (есть в базе знаний проекта)" in full


def test_terminal_shows_applied_fix_and_flagged_problem(tmp_path: Path) -> None:
    run = _run([2, 1])
    console, full = _render(
        tmp_path, run, [TEST, "ПРИЧИНА: тест — совсем без формата"],
        proposals={"01": _proposal()}, apply_states={"01": "applied"}, flagged={"02"},
    )
    assert "   Агент считает: [АВТОТЕСТ] Контракт API изменился" in console
    assert "   Правка:        `src/OrderTest.java:6` — уже применено" in console
    assert (
        "**Проблема 2** · 1 тест\n"
        "   Агент считает: [ФОРМАТ НАРУШЕН] разбор не прошёл проверку формата, текст — в report.md\n"
        "   Тест:          test_3"
    ) in console
    assert "совсем без формата" not in console  # текст такого разбора — только в файле
    assert "- Статус: уже применено — запустите тест заново" in full
    assert "### Проблема 1: src/OrderTest.java:6 — уже применено" in full


def test_unknown_fix_state_is_not_offered_as_waiting_for_consent(tmp_path: Path) -> None:
    run = _run([3, 2])
    console, full = _render(
        tmp_path, run, [TEST, TEST],
        proposals={"01": _proposal(), "02": _proposal()},
        apply_states={"01": "unknown", "02": "not_applied"},
    )
    agent = _section(console, "Агент может поправить сам")
    assert "Проблема 2" in agent and "Проблема 1" not in agent
    manual = _section(console, "Автотест сломан, но править вручную")
    assert "**Проблема 1** · 3 теста\n" in manual
    assert "   Агент считает: [АВТОТЕСТ] ожидаемый код устарел." in manual
    assert "ждёт вашего «да»" not in manual  # обещания правки для проблемы 1 нет
    # Причина видна в файле: и в разделе, и в подробностях, с честной пометкой состояния.
    top = full.split("\n---\n", 1)[0]
    file_manual = _section(top, "Автотест сломан, но править вручную")
    assert "стоит ли она, неизвестно" in file_manual and "apply повторно её не применит" in file_manual
    assert "### Что делать" not in full  # обзора из голых номеров в файле нет
    assert "### Проблема 1: src/OrderTest.java:6 — состояние правки неизвестно" in full
    assert "### Проблема 2: src/OrderTest.java:6\n" in full
    details = full.split("## Подробности по проблемам", 1)[1]
    assert "**Почему агент не правил сам:** правка в `src/OrderTest.java:6` применялась" in details


# --- полный разбор (report.md) ------------------------------------------------------


def test_report_file_groups_problems_with_full_items(tmp_path: Path) -> None:
    run = _run([5, 4, 3, 2, 1])
    _, full = _render(
        tmp_path, run, [APP, TEST, ENV, DATA, UNKNOWN],
        proposals={"02": _proposal()}, apply_states={"02": "not_applied"},
    )
    top = full.split("\n---\n", 1)[0]
    assert "### Что делать" not in top  # обзор из голых номеров проблем ничего не говорил
    assert "— проблемы 1, 5" not in top
    attention = _section(top, "Требуют вашего внимания")
    assert "**Проблема 1** — 5 тестов\n" in attention
    assert "- Что случилось: Заказ не создаётся, сервер отвечает 500." in attention
    assert "- Агент считает: возможная ошибка приложения — NPE в OrderService при пустом customer." in attention
    assert "- Что делать:\n   1. Добавить проверку customer.\n   2. Вернуть 400." in attention
    assert "- Агент считает: причина не ясна — данных мало." in attention
    assert "- Что делать: Посмотреть лог вручную." in attention  # один шаг — без номера «1.»
    agent = _section(top, "Агент может поправить сам")
    assert "**Проблема 2** — `src/OrderTest.java:6` · 4 теста" in agent
    assert "- Агент считает: ошибка в автотесте — Контракт API изменился" in agent
    assert "- Обратите внимание: меняется ожидаемое значение в проверке (200 → 201)" in agent
    assert "- Статус: ждёт вашего «да»" in agent
    assert "[test_1](https://testops.example/testresult/1)" in attention


def test_attention_lists_app_bugs_before_unknown_even_when_smaller(tmp_path: Path) -> None:
    run = _run([9, 1])
    console, full = _render(tmp_path, run, [UNKNOWN, APP])
    for text in (console, full):
        attention = _section(text, "Требуют вашего внимания")
        assert attention.index("Проблема 2") < attention.index("Проблема 1")


def test_manual_test_fix_explains_why_agent_did_not_fix(tmp_path: Path) -> None:
    run = _run([4, 3, 2, 1])
    console, full = _render(
        tmp_path, run, [TEST, TEST, TEST, TEST_NO_CODE],
        proposals={"01": _proposal("skip", "нужен доступ к боевому стенду")},
        not_proposed={"03": "лимит — не больше 5 предложений правок на один разбор"},
    )
    assert "**Проблема 4** · 1 тест\nТест не находит кнопку.\n" in _section(
        console, "Автотест сломан, но править вручную"
    )
    top = full.split("\n---\n", 1)[0]
    manual = _section(top, "Автотест сломан, но править вручную")
    assert "агент решил не трогать код: нужен доступ к боевому стенду" in manual
    assert "лимит — не больше 5 предложений правок на один разбор" in manual
    assert "нет ссылки на строку кода автотеста" in manual  # проблема 4: нет КОД
    assert "- Где в коде: src/OrderTest.java:6 — assertEquals(200, …)" in manual  # куда идти править
    assert "Агент может поправить сам" not in full


def test_full_report_keeps_reason_for_every_problem(tmp_path: Path) -> None:
    count = MAX_BRIEF_ITEMS + 6
    run = _run([1] * count)
    long_why = "нужен доступ к боевому стенду, " * 15
    console, full = _render(
        tmp_path, run, [TEST] * count,
        proposals={"09": _proposal("skip", long_why)},
        not_proposed={"10": "лимит — не больше 5 предложений правок на один разбор",
                      "11": "предложение правки не прошло проверку за 3 попытки и отброшено"},
    )
    assert "**Проблема 9**" not in console  # в терминале — только первые пункты
    details = full.split("## Подробности по проблемам", 1)[1]
    for number, reason in (
        ("09", f"агент решил не трогать код: {long_why.strip()}"),
        ("10", "лимит — не больше 5 предложений правок на один разбор"),
        ("11", "предложение правки не прошло проверку за 3 попытки и отброшено"),
    ):
        block = details.split(f"### Проблема {int(number)} ", 1)[1].split("### Проблема", 1)[0]
        assert f"- **Почему агент не правил сам:** {reason}" in block
    assert details.count("Почему агент не правил сам") == count  # у каждой проблемы раздела


def test_weakening_warning_is_visible_next_to_the_fix(tmp_path: Path) -> None:
    run = _run([2])
    console, full = _render(tmp_path, run, [TEST], proposals={"01": _proposal()})
    assert "   Проверьте:     меняется ожидаемое значение в проверке (200 → 201)" in console
    assert "- Обратите внимание: меняется ожидаемое значение в проверке (200 → 201)" in full


def test_report_file_keeps_full_text_and_all_steps(tmp_path: Path) -> None:
    run = _run([3])
    run["triage"]["failed_tests"][0]["name"] = "тест с очень длинным названием " * 10
    console, full = _render(tmp_path, run, [_long_analysis()])
    top = full.split("\n---\n", 1)[0]

    assert LONG.strip() not in console  # в терминале — только начало каждого текста
    assert all(len(line) <= 300 for line in console.splitlines() if line.startswith("  "))
    assert "…" not in full  # в файле нет ни одного сокращения
    text = _long_texts()
    assert f"- Что случилось: {' '.join(text['what'].split())}" in top  # оба предложения
    assert f"- Агент считает: возможная ошибка приложения — {' '.join(text['cause'].split()[2:])}" in top
    assert "- Что делать:\n   1. Первый шаг" in top and "   2. Второй шаг — тоже важен." in top
    assert " ".join(("тест с очень длинным названием " * 10).split()) in top  # имя теста целиком


def test_report_file_shows_every_problem_and_all_numbers(tmp_path: Path) -> None:
    count = MAX_BRIEF_ITEMS + 6
    run = _run([1] * count)
    why = "нужен доступ к боевому стенду, " * 15
    console, full = _render(
        tmp_path, run, [TEST] * count, proposals={"01": _proposal("skip", why)}
    )
    top = full.split("\n---\n", 1)[0]
    assert top.count("**Проблема ") == count  # файл — без «… и ещё N»
    assert "… и ещё" not in top and "и др." not in top
    assert f"агент решил не трогать код: {why.strip()}" in top
    assert why.strip() not in console  # в терминале причина отказа обрезана
    assert top.count("- Агент считает: ") == count  # мнение агента — у каждой проблемы файла
    assert "… и ещё 6" in console


def test_report_file_lists_all_tests_and_points_to_testops_beyond_the_cap(tmp_path: Path) -> None:
    size = report.MAX_DETAIL_TESTS + 5
    run = _run([size])
    _, full = _render(tmp_path, run, [APP])
    details = full.split("## Подробности по проблемам", 1)[1]
    assert details.count("   - [test_") == report.MAX_DETAIL_TESTS
    assert "   - и ещё 5 — полный список в TestOps: https://testops.example/launch/5" in details

    run = _run([12])
    _, full = _render(tmp_path, run, [APP])
    assert full.split("## Подробности по проблемам", 1)[1].count("   - [test_") == 12
    top = full.split("\n---\n", 1)[0]
    assert "[test_5](https://testops.example/testresult/5) и ещё 7 (список — в подробностях" in top


def test_flagged_problem_raw_text_is_in_file_details(tmp_path: Path) -> None:
    raw = "ПРИЧИНА: тест — " + LONG
    run = _run([2])
    console, full = _render(tmp_path, run, [raw], flagged={"01"})
    assert "[ФОРМАТ НАРУШЕН] разбор не прошёл проверку формата, текст — в report.md" in console
    assert LONG.strip()[:30] not in console
    top, details = full.split("\n---\n", 1)
    assert "его текст — в подробностях ниже" in top and "…" not in top
    assert " ".join(LONG.split()) in " ".join(details.split())


# --- простой язык -------------------------------------------------------------------


def test_report_uses_plain_words(tmp_path: Path) -> None:
    run = _run([3, 2], muted=1, warnings=["данные могли не выгрузиться полностью"])
    console, full = _render(tmp_path, run, [APP, ENV])
    assert "Всего тестов: 16 — прошло 10, упало 6 (failed 6, broken 0), пропущено 0." in full
    assert "В разборе: 5 упавших тестов → 2 проблемы (ещё 1 отключённый muted-тест не учитываем)." in full
    assert "Внимание: данные могли не выгрузиться полностью" in full
    assert "[######----] 16 тестов: прошло 10, упало 6\n" in console  # нулевые счётчики не пишем
    assert "5 упавших тестов -> 2 проблемы (ещё 1 отключённый muted-тест не учитываем):" in console
    assert "(!) данные могли не выгрузиться полностью" in console
    for text in (console, full):
        for jargon in ("кластер", "Кластер", "Активных падений", "По категориям", "сигнатур"):
            assert jargon not in text
    # report.md — без эмодзи; в терминале только значки разделов, без селектора U+FE0F
    # (с ним в части терминалов съезжает ширина строки), остальное — ASCII.
    emoji = re.compile(r"[\U0001F300-\U0001FAFF☀-➿\uFE0F]")
    assert not emoji.search(full)
    assert set(emoji.findall(console)) == {"🔴", "🔵"}


def test_green_report_is_plain(tmp_path: Path) -> None:
    run = _run([])
    console, text = render_green_report(run, RunPaths(tmp_path))
    assert "Упавших тестов нет — разбирать нечего." in console and "В разборе" not in text
    run["counts"]["muted_failures"] = 2
    assert "кроме отключённых (muted): 2" in render_green_report(run, RunPaths(tmp_path))[0]


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
    assert task.count("--- Проблема ") == MAX_SUMMARY_LISTED
    assert "сырое сообщение" not in task  # у каждой проблемы есть краткий разбор
    assert f"Уникальных проблем (кластеров): {count}" in task  # счётчик — по всем проблемам
    rest = count - MAX_SUMMARY_LISTED
    assert f"--- Ещё {rest} проблем поменьше ({rest} тестов), по причинам: приложение — {rest}" in task
    assert len(task) < 25_000


def test_summary_task_size_does_not_grow_with_problems(tmp_path: Path) -> None:
    def task_size(count: int) -> int:
        run = _run([1] * count)
        analyses = {entry["file_id"]: parse_analysis(APP) for entry in run["clusters"]}
        return len(build_summary_task(run, analyses, set(), RunPaths(tmp_path)))

    assert task_size(500) - task_size(MAX_SUMMARY_LISTED + 1) < 200


def test_summary_task_keeps_report_numbers_and_groups_the_tail(tmp_path: Path) -> None:
    # Большие проблемы в конце: в задание по одной попадают они, со своими номерами из отчёта.
    sizes = [1] * MAX_SUMMARY_LISTED + [5, 7]
    run = _run(sizes)
    analyses = {entry["file_id"]: parse_analysis(ENV) for entry in run["clusters"]}
    analyses["01"] = parse_analysis(APP)
    task = build_summary_task(run, analyses, {"40"}, RunPaths(tmp_path))
    last = len(sizes)
    assert f"--- Проблема {last}: error {last} (7 тестов) ---" in task
    assert f"--- Проблема {last - 1}: error {last - 1} (5 тестов) ---" in task
    # Хвост — две самые маленькие по порядку номеров среди равных: 39 и 40.
    assert "--- Проблема 39:" not in task and "--- Проблема 40:" not in task
    assert (
        "--- Ещё 2 проблемы поменьше (2 теста), по причинам: "
        "не ясна — 1 проблема (1 тест); окружение — 1 проблема (1 тест) ---"
    ) in task


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


LONG = "очень длинное слово " * 40  # заведомо больше любого потолка терминала


def _long_texts() -> dict[str, str]:
    return {
        "what": f"Тест упал. {LONG.strip()}. Вторая мысль здесь.",
        "cause": f"приложение — {LONG.strip()}.",
        "fix": f"1. Первый шаг {LONG.strip()}.\n2. Второй шаг — тоже важен.",
    }


def _long_analysis(category_line: str | None = None) -> str:
    text = _long_texts()
    return (
        f"ЧТО СЛОМАЛОСЬ: {text['what']}\n"
        f"ПРИЧИНА: {category_line or text['cause']}\n"
        f"КАК ИСПРАВИТЬ:\n{text['fix']}\n"
    )
