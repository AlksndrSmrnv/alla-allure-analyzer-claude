"""Параллельный разбор: пакеты для субагентов, ``verify``, ``next --workers/--serial``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from skill_fake_testops import FakeTestOps, many_launch

from alla_skill_lib import cli

VALID = (
    "ЧТО СЛОМАЛОСЬ: Сервис вернул не то, что ждал тест.\n"
    "\n"
    "ПРИЧИНА: окружение — стенд отдаёт другие данные.\n"
    "\n"
    "НАБЛЮДЕНИЯ:\n"
    "- [S1] «{quote}»\n"
    "\n"
    "КАК ИСПРАВИТЬ:\n"
    "1. Проверить данные на стенде.\n"
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


def _next(run_dir: Path, capsys: pytest.CaptureFixture[str], *flags: str) -> str:
    code, out = _run(["next", str(run_dir), *flags], capsys)
    assert code == 0, out
    return out


def _manual_ids(run: dict) -> list[str]:
    return [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]


def _answer(run_dir: Path, file_ids: list[str], text: str = VALID) -> None:
    """Записать разборы; в ``{quote}`` — начало сообщения об ошибке (S1) этого кластера."""
    for file_id in file_ids:
        sources = json.loads((run_dir / "evidence" / f"{file_id}.sources.json").read_text("utf-8"))
        quote = sources["S1"]["text"].splitlines()[0][:60]
        (run_dir / "analyses" / f"{file_id}.md").write_text(text.replace("{quote}", quote),
                                                           encoding="utf-8")


@pytest.fixture(name="small_batches")
def small_batches_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пакетный режим уже для двух кластеров: по одному в пакете, два субагента."""
    monkeypatch.setattr(cli, "PARALLEL_MIN_PENDING", 2)
    monkeypatch.setattr(cli, "BATCH_SIZE", 1)
    monkeypatch.setattr(cli, "DEFAULT_WORKERS", 2)


# --- планирование пакетов ---------------------------------------------------------


def test_plan_batches_cuts_a_wave_in_order_without_overlap() -> None:
    pending = [f"{n:02d}" for n in range(1, 66)]
    wave = cli.plan_batches(pending, 6, 4)
    assert [len(batch) for batch in wave] == [6, 6, 6, 6]
    assert [file_id for batch in wave for file_id in batch] == pending[:24]
    assert cli.plan_batches(pending[24:], 6, 4)[0][0] == "25"


def test_plan_batches_short_tail_and_serial_mode() -> None:
    assert cli.plan_batches(["01", "02", "03"], 2, 4) == [["01", "02"], ["03"]]
    assert cli.plan_batches(["01", "02"], 6, 1) == []
    assert cli.plan_batches([], 6, 4) == []


# --- пакетный режим ---------------------------------------------------------------


def test_few_clusters_are_analyzed_one_by_one(project: Path, testops: FakeTestOps, capsys) -> None:
    _, _, out = _prepare(project, capsys)
    assert out.startswith("STATUS: analyze\n")
    assert "analyze_batch" not in out


def test_many_clusters_are_split_into_batches(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, run, out = _prepare(project, capsys)
    assert out.startswith("STATUS: analyze_batch\n")
    manual = _manual_ids(run)
    auto = [entry["file_id"] for entry in run["clusters"] if entry["auto"]]
    assert len(manual) == 2 and auto

    batch_files = sorted((run_dir / "batches").glob("*.md"))
    assert [path.name for path in batch_files] == ["1.md", "2.md"]
    covered = []
    for path, file_id in zip(batch_files, manual):
        text = path.read_text(encoding="utf-8")
        assert str(run_dir / "clusters" / f"{file_id}.md") in text
        assert str(run_dir / "analyses" / f"{file_id}.md") in text
        assert f"verify {file_id} --run {run_dir}" in text.replace("'", "")
        assert "references/analysis-format.md" in text  # формат — в задании кластера и в справочнике
        assert "ПРИЧИНА: <тест|приложение" not in text  # шаблон не дублируем: он в clusters/NN.md
        covered.append(file_id)
    assert covered == manual
    assert "subagent_type: alla-batch" in out and "без subagent_type" in out
    assert all(f"{file_id}.md" not in "".join(p.read_text(encoding="utf-8") for p in batch_files)
               for file_id in auto)

    assert f"Пакет 1 (кластеры {manual[0]})" in out and f"Пакет 2 (кластеры {manual[1]})" in out
    assert str(run_dir / "batches" / "1.md") in out
    assert "run_in_background: false" in out and "в одном сообщении" in out
    assert "--serial" in out and "Выполни: " in out
    # основной агент не получает задания кластеров — их читают субагенты
    assert "1. Прочитай задание" not in out


def test_batch_task_forbids_state_changing_commands(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, _, _ = _prepare(project, capsys)
    text = (run_dir / "batches" / "1.md").read_text(encoding="utf-8")
    for command in ("next", "skip", "apply", "remember", "reject"):
        assert command in text.split("## Нельзя", 1)[1]
    assert "недоверенный" in text  # предупреждение о данных TestOps дублируется в пакете


def test_waves_cover_all_clusters_then_go_to_summary(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    FakeTestOps(many_launch(40)).install(monkeypatch)
    run_dir, run, out = _prepare(project, capsys, launch_id=900)
    assert len(run["clusters"]) == 40
    manual = _manual_ids(run)
    assert len(manual) == 40

    # волна 1: 4 субагента по 6 кластеров
    assert out.startswith("STATUS: analyze_batch\n")
    assert "40 из 40" in out and "остальные (16)" in out
    assert sorted(p.name for p in (run_dir / "batches").glob("*.md")) == ["1.md", "2.md", "3.md", "4.md"]
    _answer(run_dir, manual[:24])

    # волна 2: остаток 16 → пакеты 6 + 6 + 4, нумерация с 1, только оставшиеся кластеры
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: analyze_batch\n") and "16 из 40" in out
    third = (run_dir / "batches" / "3.md").read_text(encoding="utf-8")
    assert f"кластеры {', '.join(manual[36:40])}" in third
    assert "Пакет 4" not in out
    _answer(run_dir, manual[24:])

    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: summary")
    (run_dir / "summary.md").write_text("Итог.", encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: done")


def test_remaining_clusters_below_threshold_go_one_by_one(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    FakeTestOps(many_launch(30)).install(monkeypatch)
    run_dir, run, _ = _prepare(project, capsys, launch_id=900)
    manual = _manual_ids(run)
    _answer(run_dir, manual[:24])  # осталось 6 < 10
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: analyze\n")
    assert "Кластер 25 из 30" in out


def test_invalid_batch_answer_is_repaired_one_by_one(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    first, second = _manual_ids(run)
    _answer(run_dir, [first])
    _answer(run_dir, [second], "ПРИЧИНА: баг\nбез остальных разделов")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: fix\n") and f"Кластер {int(second)} из" in out
    assert "попытка 1 из 3" in out
    _answer(run_dir, [second])
    assert _next(run_dir, capsys).startswith("STATUS: summary")


def test_resume_after_interruption_issues_only_unfinished_clusters(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    first, second = _manual_ids(run)
    _answer(run_dir, [first])
    out = _next(run_dir, capsys, "--workers", "2")
    # остался один кластер, порог 2 не достигнут — обычный разбор одного кластера
    assert out.startswith("STATUS: analyze\n") and f"Кластер {int(second)} из" in out


# --- неудачная волна не повторяется -----------------------------------------------


def test_failed_wave_is_not_issued_again(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, run, out = _prepare(project, capsys)
    assert out.startswith("STATUS: analyze_batch\n")
    first, _ = _manual_ids(run)

    # субагенты ничего не записали: пакетов больше нет, кластеры идут по одному
    for _ in range(3):
        out = _next(run_dir, capsys)
        assert out.startswith("STATUS: analyze\n")
        assert f"Кластер {int(first)} из" in out
    assert json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["workers"] == 1


def test_failed_wave_notice_is_shown_once(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    first, second = _manual_ids(run)
    out = _next(run_dir, capsys)
    assert "Прошлая волна не записала ни одного разбора" in out
    assert f"кластеры {first}, {second}" in out and "--workers N" in out
    assert "Прошлая волна" not in _next(run_dir, capsys)


def test_workers_flag_issues_a_failed_wave_again(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, _, _ = _prepare(project, capsys)
    assert _next(run_dir, capsys).startswith("STATUS: analyze\n")  # волна не удалась
    out = _next(run_dir, capsys, "--workers", "2")  # явная просьба — пакеты выдаются заново
    assert out.startswith("STATUS: analyze_batch\n") and "Пакет 1" in out
    assert _next(run_dir, capsys).startswith("STATUS: analyze\n")  # и снова не больше одного раза


def test_partial_wave_leftovers_go_one_by_one(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    FakeTestOps(many_launch(40)).install(monkeypatch)
    run_dir, run, _ = _prepare(project, capsys, launch_id=900)
    manual = _manual_ids(run)
    _answer(run_dir, manual[:20])  # из волны 01–24 субагенты не записали 21–24

    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: analyze_batch\n")
    assert "Субагенты прошлой волны не записали разборы кластеров 21, 22, 23, 24" in out
    assert "16 из 40" in out
    issued = "".join(
        path.read_text(encoding="utf-8") for path in (run_dir / "batches").glob("*.md")
    )
    assert f"кластеры {', '.join(manual[24:30])}" in (run_dir / "batches" / "1.md").read_text(encoding="utf-8")
    assert all(f"clusters/{file_id}.md" not in (run_dir / "batches" / "1.md").read_text(encoding="utf-8")
               for file_id in manual[20:24])
    assert issued  # пакеты волны 2 записаны

    _answer(run_dir, manual[24:])
    out = _next(run_dir, capsys)  # осталось четыре невыданных повторно кластера — по одному
    assert out.startswith("STATUS: analyze\n") and "Кластер 21 из 40" in out


# --- serial / workers -------------------------------------------------------------


def test_serial_flag_is_remembered(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, _, out = _prepare(project, capsys)
    assert out.startswith("STATUS: analyze_batch")
    out = _next(run_dir, capsys, "--serial")
    assert out.startswith("STATUS: analyze\n")
    assert json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["workers"] == 1
    assert _next(run_dir, capsys).startswith("STATUS: analyze\n")  # без флага — всё ещё по одному


def test_workers_limit_the_wave(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    FakeTestOps(many_launch(40)).install(monkeypatch)
    run_dir, run, _ = _prepare(project, capsys, launch_id=900)
    out = _next(run_dir, capsys, "--workers", "2")
    assert "В этой волне: пакетов — 2, кластеров — 12" in out
    assert "Пакет 3" not in out
    _answer(run_dir, _manual_ids(run)[:12])
    out = _next(run_dir, capsys)  # значение сохранено: снова два пакета
    assert "В этой волне: пакетов — 2, кластеров — 12" in out and "Пакет 3" not in out


@pytest.mark.parametrize("value", ["0", "-1", "99"])
def test_bad_workers_value_is_an_error(
    project: Path, testops: FakeTestOps, value: str, capsys
) -> None:
    run_dir, _, _ = _prepare(project, capsys)
    code, out = _run(["next", str(run_dir), "--workers", value], capsys)
    assert code == 1 and out.startswith("STATUS: error")
    assert not (run_dir / "state.json").exists()


def test_workers_and_serial_are_mutually_exclusive(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, _, _ = _prepare(project, capsys)
    with pytest.raises(SystemExit):
        cli.main(["next", str(run_dir), "--workers", "3", "--serial"])
    assert capsys.readouterr().out.startswith("STATUS: error")


def test_skip_reduces_pending_clusters(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    first, _ = _manual_ids(run)
    code, out = _run(["skip", first, "--run", str(run_dir), "--reason", "не нужен"], capsys)
    assert code == 0 and out.startswith("STATUS: saved")
    assert _next(run_dir, capsys).startswith("STATUS: analyze\n")  # ожидающих меньше порога


# --- verify -----------------------------------------------------------------------


def test_verify_accepts_valid_and_reports_broken_files(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    first, second = _manual_ids(run)
    _answer(run_dir, [first])
    code, out = _run(["verify", first, "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: ok")
    assert f"Кластер {first}: принят." in out

    _answer(run_dir, [second], "ЧТО СЛОМАЛОСЬ: что-то\nКАК ИСПРАВИТЬ:\n1. шаг\n")
    code, out = _run(["verify", first, second, "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: fix")
    assert f"Кластер {first}: принят." in out
    assert f"Кластер {second}: разбор не прошёл проверку" in out
    assert "нет раздела «ПРИЧИНА:»" in out and "Ожидаемый формат:" in out


def test_verify_reports_missing_analysis(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    first, _ = _manual_ids(run)
    code, out = _run(["verify", first, "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: fix")
    assert "файл разбора пуст или не создан" in out


def test_verify_is_read_only(
    project: Path, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    first, second = _manual_ids(run)
    _answer(run_dir, [first])
    _answer(run_dir, [second], "битый разбор")
    before = sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*"))
    state_before = (run_dir / "state.json").read_text(encoding="utf-8")
    contents = (run_dir / "analyses" / f"{second}.md").read_text(encoding="utf-8")
    for _ in range(4):  # повторные проверки не считаются попытками исправления
        _run(["verify", first, second, "--run", str(run_dir)], capsys)
    assert sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*")) == before
    assert (run_dir / "state.json").read_text(encoding="utf-8") == state_before
    assert not (project / "alla-reports" / "history.jsonl").exists()
    assert (run_dir / "analyses" / f"{second}.md").read_text(encoding="utf-8") == contents
    out = _next(run_dir, capsys)  # next по-прежнему на первой попытке
    assert out.startswith("STATUS: fix") and "попытка 1 из 3" in out


def test_verify_unknown_cluster_and_run_are_errors(
    project: Path, testops: FakeTestOps, capsys
) -> None:
    run_dir, _, _ = _prepare(project, capsys)
    code, out = _run(["verify", "99", "--run", str(run_dir)], capsys)
    assert code == 1 and out.startswith("STATUS: error") and "99" in out
    code, out = _run(["verify", "1", "--run", str(run_dir / "nope")], capsys)
    assert code == 1 and out.startswith("STATUS: error")


QWEN_TOOLS = {"read_file", "write_file", "edit", "run_shell_command", "glob", "grep_search"}


def test_batch_agent_file_is_valid_for_qwen_and_narrow() -> None:
    # Невалидный файл агента Qwen Code пропускает молча (подсказка короче 10 символов,
    # нет name/description) — тогда пакеты снова уходят general-purpose субагенту.
    from alla_skill_lib import batch_task

    text = batch_task.batch_agent_source().read_text(encoding="utf-8")
    _, header, prompt = text.split("---\n", 2)
    fields = dict(line.split(":", 1) for line in header.splitlines() if not line.startswith(" "))
    assert fields["name"].strip() == batch_task.BATCH_AGENT
    assert fields["description"].strip()
    tools = [line.strip()[2:] for line in header.splitlines() if line.startswith("  - ")]
    assert set(tools) == {"read_file", "write_file", "run_shell_command"} <= QWEN_TOOLS
    assert len(prompt.strip()) >= 10
    for phrase in ("Первым действием прочитай", "ls, find, cat, git", "другие пакеты",
                   "Проблема скилла:"):
        assert phrase in " ".join(prompt.split()), phrase


def test_install_batch_agent_writes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alla_skill_lib import batch_task
    from alla_skill_lib import workspace as ws

    monkeypatch.setattr(ws, "SKILL_DIR", Path(cli.__file__).resolve().parents[2])
    target = batch_task.install_batch_agent(tmp_path)
    assert target == tmp_path / ".qwen" / "agents" / "alla-batch.md"
    assert target.read_text(encoding="utf-8") == batch_task.batch_agent_source().read_text(
        encoding="utf-8")
    assert batch_task.install_batch_agent(tmp_path) is None  # уже установлен — не пишет
    target.write_text("старая версия", encoding="utf-8")
    assert batch_task.install_batch_agent(tmp_path) == target  # устарел — обновляет

