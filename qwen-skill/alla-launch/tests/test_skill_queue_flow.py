"""Сквозные связки команд, которых нет в других тестах: пакетный разбор → verify → fix →
propose → apply/revert → remember на одном прогоне; известная проблема через CLI и reject."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from eval.corpus_dev import known_issue_symptoms
from qwen_stand import KNOWLEDGE
from skill_fake_testops import FakeTestOps
from test_skill_flow import (
    FEEDBACK,
    MARKDOWN_ANALYSIS,
    PROPOSAL,
    TEST_ANALYSIS,
    _full_report,
    _next,
    _prepare,
    _run,
)

from alla_skill_lib import cli

APP_ANALYSIS = (
    "ЧТО СЛОМАЛОСЬ: Сервис платежей не получил соединение с БД.\n"
    "\n"
    "ПРИЧИНА: приложение — пул соединений БД платежей исчерпан.\n"
    "\n"
    "НАБЛЮДЕНИЯ:\n"
    "- [S1] «{quote}»\n"
    "\n"
    "КАК ИСПРАВИТЬ:\n"
    "1. Проверить пул соединений payment-db.\n"
)


@pytest.fixture(name="small_batches")
def small_batches_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пакетный режим уже для двух кластеров: по одному в пакете, два субагента."""
    monkeypatch.setattr(cli, "PARALLEL_MIN_PENDING", 2)
    monkeypatch.setattr(cli, "BATCH_SIZE", 1)
    monkeypatch.setattr(cli, "DEFAULT_WORKERS", 2)


def _quote(run_dir: Path, file_id: str) -> str:
    sources = json.loads((run_dir / "evidence" / f"{file_id}.sources.json").read_text("utf-8"))
    return str(sources["S1"]["text"].splitlines()[0][:60])


def _write(run_dir: Path, file_id: str, text: str) -> None:
    (run_dir / "analyses" / f"{file_id}.md").write_text(text, encoding="utf-8")


def test_batch_answers_go_through_verify_fix_proposal_apply_and_remember(
    project: Path, testops: FakeTestOps, small_batches: None, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, run, out = _prepare(project, capsys)
    assert out.startswith("STATUS: analyze_batch\n")
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    test_file = project / "src/test/java/ru/company/orders/OrderTest.java"

    # Субагенты пишут разборы и проверяют их сами; один разбор битый.
    _write(run_dir, order, TEST_ANALYSIS)
    _write(run_dir, login, "ПРИЧИНА: стенд\nбез остальных разделов")
    code, verified = _run(["verify", order, login, "--run", str(run_dir)], capsys)
    assert code == 0 and verified.startswith("STATUS: fix")
    assert f"Кластер {order}: принят." in verified
    assert f"Кластер {login}: разбор не прошёл проверку" in verified

    # Основной агент идёт по номерам: правка для принятого разбора, затем битый — по одному.
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: propose"), out
    assert str(run_dir / "proposals" / f"{order}.md") in out
    (run_dir / "proposals" / f"{order}.md").write_text(PROPOSAL, encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: fix\n") and f"Кластер {int(login)} из" in out
    assert "попытка 1 из 3" in out
    _write(run_dir, login, MARKDOWN_ANALYSIS)
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: summary"), out
    (run_dir / "summary.md").write_text("Итог прогона.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done"), out
    assert "### 🟢 Агент может поправить сам (1)" in out and "apply 01 --run" in out

    code, diff = _run(["apply", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and diff.startswith("STATUS: diff")
    diff_hash = next(line for line in diff.splitlines() if "--yes --diff" in line).split("--diff ")[1].split()[0]
    code, out = _run(["apply", "1", "--run", str(run_dir), "--yes", "--diff", diff_hash], capsys)
    assert code == 0 and out.startswith("STATUS: applied")
    assert "assertEquals(201" in test_file.read_text(encoding="utf-8")
    assert "— уже применено" in _next(run_dir, capsys)  # next пересобирает report.md
    assert "- Статус: уже применено" in _full_report(run_dir)
    code, out = _run(["revert", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: reverted")
    assert "assertEquals(200" in test_file.read_text(encoding="utf-8")
    assert "— уже применено" not in _next(run_dir, capsys)

    (run_dir / "feedback" / f"{order}.md").write_text(FEEDBACK, encoding="utf-8")
    code, out = _run(["remember", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    assert len(list((project / "alla-kb").glob("*.json"))) == 1


def test_known_issue_is_grouped_through_cli_and_splits_after_reject(
    project: Path, monkeypatch: pytest.MonkeyPatch, small_batches: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    [record] = KNOWLEDGE["payments_pool"]
    entry_id = record["id"]
    (project / "alla-kb").mkdir()
    (project / "alla-kb" / f"{entry_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8")
    FakeTestOps(known_issue_symptoms().fixture).install(monkeypatch)

    run_dir, run, out = _prepare(project, capsys, launch_id=5112)
    assert out.startswith("STATUS: analyze_batch\n")
    ids = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    offered = [entry["file_id"] for entry in run["clusters"]
               if any(kb["id"] == entry_id for kb in entry["kb"])]
    assert len(ids) == 4 and len(offered) == 3
    [other] = [file_id for file_id in ids if file_id not in offered]
    for file_id in offered:
        _write(run_dir, file_id, APP_ANALYSIS.replace("{quote}", _quote(run_dir, file_id))
               + f"БАЗА ЗНАНИЙ: {entry_id}\n")
    _write(run_dir, other, APP_ANALYSIS.replace("{quote}", _quote(run_dir, other))
           .replace("пул соединений БД платежей исчерпан", "цена товара в каталоге пустая"))
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: summary"), out
    (run_dir / "summary.md").write_text("Итог прогона.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done"), out

    numbers = ", ".join(str(int(file_id)) for file_id in offered)
    assert f"**Проблемы {numbers}** · 6 тестов · [известная проблема: {entry_id}]" in out
    assert f"Одна известная проблема: «{record['title']}»" in out
    report = _full_report(run_dir)
    assert "### Известные проблемы из базы знаний (1)" in report
    assert f"— проблемы {numbers} · 6 тестов" in report
    assert f"**Проблема {int(other)}** · 2 теста" in out

    # Пользователь: к первой проблеме запись не относится.
    first, *rest = offered
    code, out = _run(["reject", str(int(first)), entry_id, "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: summary"), out
    (run_dir / "summary.md").write_text("Итог после reject.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done"), out
    left = ", ".join(str(int(file_id)) for file_id in rest)
    assert f"**Проблемы {left}** · 4 теста · [известная проблема: {entry_id}]" in out
    assert f"**Проблема {int(first)}** · 2 теста" in out
    assert "[разбор опирался на отвергнутую запись базы знаний]" in out
    assert f"— проблемы {left} · 4 теста" in _full_report(run_dir)
