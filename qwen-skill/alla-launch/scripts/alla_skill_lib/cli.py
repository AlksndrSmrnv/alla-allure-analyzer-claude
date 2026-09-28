"""CLI скилла alla-launch: ``prepare <launch_id>`` и ``next [run_dir]``.

``prepare`` получает прогон из TestOps, кластеризует падения и раскладывает
задания по кластерам. ``next`` — конечный автомат: смотрит на файлы в папке
разбора и печатает, что агенту делать дальше. Первая строка вывода всегда
``STATUS: analyze|fix|summary|done|error``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from alla_core.config import Settings
from alla_core.exceptions import AllaError, ConfigurationError
from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import ClusterAnalysis, parse_analysis, validate_analysis
from alla_skill_lib.cluster_task import (
    build_cluster_task,
    has_evidence,
    no_evidence_analysis,
    project_frames,
    select_log_and_trace,
)
from alla_skill_lib.code_hints import ProjectIndex, hints_for_cluster
from alla_skill_lib.pipeline import LaunchData, collect_launch
from alla_skill_lib.report import build_summary_task, render_green_report, render_report

logger = logging.getLogger(__name__)

MAX_FIX_ATTEMPTS = 3
REPORT_BEGIN = "===ОТЧЁТ==="
REPORT_END = "===КОНЕЦ==="
EXPECTED_FORMAT = """\
ЧТО СЛОМАЛОСЬ: <1–2 предложения>
ПРИЧИНА: <тест|приложение|окружение|данные|неизвестно> — <обоснование>
КАК ИСПРАВИТЬ:
1. <шаг>
КОД: <путь от корня проекта>:<строка> — <что там>   (необязательно)"""


def main(argv: list[str] | None = None) -> int:
    ws.configure_stdio()
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _build_parser().parse_args(argv)
    project_root = (
        Path(args.project_root).resolve() if args.project_root else ws.detect_project_root()
    )
    reports_dir = (
        Path(args.reports_dir).resolve()
        if args.reports_dir
        else project_root / ws.REPORTS_DIRNAME
    )
    if args.command == "prepare":
        return cmd_prepare(args.launch_id, project_root, reports_dir)
    return cmd_next(args.run_dir, reports_dir)


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--project-root",
        help="корень проекта автотестов (по умолчанию определяется по .qwen/skills)",
    )
    common.add_argument(
        "--reports-dir",
        help="папка отчётов (по умолчанию <project>/alla-reports)",
    )
    parser = argparse.ArgumentParser(prog="alla_skill.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", parents=[common], help="получить прогон из TestOps и подготовить задания"
    )
    prepare.add_argument("launch_id", type=int, help="ID прогона (launch) в Allure TestOps")
    step = commands.add_parser("next", parents=[common], help="следующий шаг разбора")
    step.add_argument("run_dir", nargs="?", help="папка разбора (по умолчанию последняя)")
    return parser


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def cmd_prepare(launch_id: int, project_root: Path, reports_dir: Path) -> int:
    env_file = ws.SKILL_DIR / ".env"
    try:
        settings = Settings.load(env_file=env_file)
    except ConfigurationError as exc:
        print("STATUS: error")
        print(f"Ошибка конфигурации: {exc}")
        print(
            f"Заполни {env_file} по образцу .env.example или задай переменные "
            "окружения ALLURE_ENDPOINT и ALLURE_TOKEN. Содержимое .env не читай и не выводи."
        )
        return 2

    try:
        data = asyncio.run(collect_launch(launch_id, settings))
    except (AllaError, httpx.HTTPError) as exc:
        print("STATUS: error")
        print(f"Не удалось получить прогон #{launch_id} из TestOps: {exc}")
        return 1
    except Exception as exc:  # неожиданный ответ API и т.п. — traceback в stderr
        logger.exception("Сбой при получении прогона")
        print("STATUS: error")
        print(f"Сбой при получении прогона #{launch_id}: {type(exc).__name__}: {exc}")
        return 1

    paths = ws.create_run_dir(reports_dir, launch_id, datetime.now())
    run = _write_run(paths, data, settings, project_root)
    ws.remember_last_run(reports_dir, paths)

    status, body = next_step(paths)
    counts = run["counts"]
    name = f" «{run['launch_name']}»" if run.get("launch_name") else ""
    print(f"STATUS: {status}")
    print(
        f"Прогон #{launch_id}{name}: тестов {counts['total']}, "
        f"активных падений {counts['active_failures']}, кластеров {len(run['clusters'])}."
    )
    print(f"Папка разбора: {paths.root}")
    print(body)
    return 0


def _write_run(
    paths: ws.RunPaths,
    data: LaunchData,
    settings: Settings,
    project_root: Path,
) -> dict[str, Any]:
    triage = data.triage
    clusters = data.clustering.clusters if data.clustering else []
    tests_by_id = {test.test_result_id: test for test in triage.failed_tests}
    index = ProjectIndex(project_root)
    width = max(2, len(str(len(clusters))))
    entries: list[dict[str, Any]] = []

    for position, cluster in enumerate(clusters, start=1):
        file_id = str(position).zfill(width)
        log_snippet, full_trace = select_log_and_trace(cluster, tests_by_id)
        auto = not has_evidence(cluster, log_snippet)
        label = cluster.label
        representative = tests_by_id.get(cluster.representative_test_id or -1)
        if auto and representative is not None:
            # Сервер подписывает такие кластеры «Тест: <id>» — имя теста понятнее.
            label = f"{representative.name} — нет данных об ошибке"
        entries.append({
            "file_id": file_id,
            "cluster_id": cluster.cluster_id,
            "label": label,
            "member_count": cluster.member_count,
            "auto": auto,
        })
        if auto:
            ws.write_text(paths.analysis(file_id), no_evidence_analysis())
            continue
        member_ids = sorted(
            cluster.member_test_ids,
            key=lambda test_id: test_id != cluster.representative_test_id,
        )
        full_names = [
            tests_by_id[test_id].full_name or ""
            for test_id in member_ids[:5]
            if test_id in tests_by_id
        ]
        frames = project_frames(full_trace)
        task = build_cluster_task(
            cluster=cluster,
            position=position,
            total=len(clusters),
            launch_id=triage.launch_id,
            answer_path=str(paths.analysis(file_id)),
            next_command=paths.next_command(),
            tests_by_id=tests_by_id,
            log_snippet=log_snippet,
            full_trace=full_trace,
            frames=frames,
            hints=hints_for_cluster(index, [name for name in full_names if name], frames),
            settings=settings,
        )
        ws.write_text(paths.cluster_task(file_id), task)

    run = {
        "schema": ws.RUN_SCHEMA,
        "launch_id": triage.launch_id,
        "launch_name": triage.launch_name,
        "launch_url": f"{settings.endpoint.rstrip('/')}/launch/{triage.launch_id}",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(project_root),
        "counts": {
            "total": triage.total_results,
            "passed": triage.passed_count,
            "failed": triage.failed_count,
            "broken": triage.broken_count,
            "skipped": triage.skipped_count,
            "unknown": triage.unknown_count,
            "muted_failures": triage.muted_failure_count,
            "active_failures": triage.active_failure_count,
        },
        "warnings": data.warnings,
        "clusters": entries,
        "triage": triage.model_dump(
            mode="json",
            exclude={
                "failed_tests": {"__all__": {"execution_steps", "log_snippet", "status_trace"}}
            },
        ),
        "clustering": data.clustering.model_dump(mode="json") if data.clustering else None,
    }
    ws.write_json(paths.run_json, run)
    return run


# ---------------------------------------------------------------------------
# next
# ---------------------------------------------------------------------------


def cmd_next(run_dir: str | None, reports_dir: Path) -> int:
    try:
        paths = ws.resolve_run(run_dir, reports_dir)
    except ws.RunNotFoundError as exc:
        print("STATUS: error")
        print(exc)
        return 1
    status, body = next_step(paths)
    print(f"STATUS: {status}")
    print(body)
    return 0


def next_step(paths: ws.RunPaths) -> tuple[str, str]:
    """Определить следующий шаг по состоянию файлов. Идемпотентно."""
    run = ws.read_json(paths.run_json)
    if not run["clusters"]:
        console, full = render_green_report(run, paths)
        ws.write_text(paths.report, full)
        return "done", _done_body(console, paths)

    project_root = Path(run["project_root"])
    state = ws.read_json(paths.state_json) if paths.state_json.is_file() else {"attempts": {}}
    entries = run["clusters"]
    total = len(entries)
    analyses: dict[str, ClusterAnalysis] = {}
    flagged: set[str] = set()

    for position, entry in enumerate(entries, start=1):
        file_id = entry["file_id"]
        analysis_path = paths.analysis(file_id)
        text = analysis_path.read_text(encoding="utf-8") if analysis_path.is_file() else ""
        if not text.strip():
            done = sum(1 for item in entries if _has_text(paths.analysis(item["file_id"])))
            return "analyze", _analyze_body(paths, entry, position, total, done)

        analysis = parse_analysis(text)
        errors = validate_analysis(analysis, project_root)
        if errors:
            attempt = _register_invalid(state, file_id, text, paths)
            if attempt < MAX_FIX_ATTEMPTS:
                return "fix", _fix_body(paths, entry, position, total, errors, attempt)
            flagged.add(file_id)
        analyses[file_id] = analysis

    summary = paths.summary.read_text(encoding="utf-8") if paths.summary.is_file() else ""
    if not summary.strip():
        ws.write_text(paths.summary_task, build_summary_task(run, analyses, paths))
        return "summary", _summary_body(paths, total)

    console, full = render_report(run, analyses, flagged, summary, paths)
    ws.write_text(paths.report, full)
    return "done", _done_body(console, paths)


def _has_text(path: Path) -> bool:
    return path.is_file() and bool(path.read_text(encoding="utf-8").strip())


def _register_invalid(
    state: dict[str, Any],
    file_id: str,
    text: str,
    paths: ws.RunPaths,
) -> int:
    """Посчитать попытку исправления; одна и та же версия файла считается один раз."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    record = state["attempts"].get(file_id, {"count": 0, "hash": None})
    if record["hash"] != digest:
        record = {"count": record["count"] + 1, "hash": digest}
        state["attempts"][file_id] = record
        ws.write_json(paths.state_json, state)
    return int(record["count"])


def _cluster_caption(entry: dict[str, Any], position: int, total: int) -> str:
    label = " ".join(str(entry["label"]).split())
    if len(label) > 120:
        label = label[:119] + "…"
    return f"Кластер {position} из {total}: {label} ({entry['member_count']} тест.)"


def _analyze_body(
    paths: ws.RunPaths,
    entry: dict[str, Any],
    position: int,
    total: int,
    done: int,
) -> str:
    return "\n".join([
        f"{_cluster_caption(entry, position, total)}. Готово разборов: {done} из {total}.",
        f"1. Прочитай задание: {paths.cluster_task(entry['file_id'])}",
        f"2. Запиши разбор в файл: {paths.analysis(entry['file_id'])}",
        f"3. Выполни: {paths.next_command()}",
    ])


def _fix_body(
    paths: ws.RunPaths,
    entry: dict[str, Any],
    position: int,
    total: int,
    errors: list[str],
    attempt: int,
) -> str:
    return "\n".join([
        f"{_cluster_caption(entry, position, total)} — разбор не прошёл проверку "
        f"(попытка {attempt} из {MAX_FIX_ATTEMPTS}):",
        *(f"- {error}" for error in errors),
        f"Исправь файл: {paths.analysis(entry['file_id'])}",
        f"Задание кластера: {paths.cluster_task(entry['file_id'])}",
        "Ожидаемый формат:",
        EXPECTED_FORMAT,
        f"Затем выполни: {paths.next_command()}",
    ])


def _summary_body(paths: ws.RunPaths, total: int) -> str:
    return "\n".join([
        f"Все кластеры разобраны ({total}). Осталось написать общий анализ прогона.",
        f"1. Прочитай задание: {paths.summary_task}",
        f"2. Запиши общий анализ в файл: {paths.summary}",
        f"3. Выполни: {paths.next_command()}",
    ])


def _done_body(console: str, paths: ws.RunPaths) -> str:
    return "\n".join([
        f"Отчёт сохранён: {paths.report}",
        "Выведи пользователю текст между маркерами дословно, без сокращений:",
        REPORT_BEGIN,
        console,
        REPORT_END,
    ])


if __name__ == "__main__":
    sys.exit(main())
