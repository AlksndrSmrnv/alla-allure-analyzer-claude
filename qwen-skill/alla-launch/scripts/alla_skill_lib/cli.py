"""CLI скилла alla-launch.

* ``prepare <launch_id>`` — получить прогон из TestOps, кластеризовать падения,
  сопоставить с базой знаний проекта и историей, разложить задания;
* ``next [run_dir]`` — конечный автомат: смотрит на файлы в папке разбора и
  печатает, что агенту делать дальше;
* ``remember NN`` / ``reject NN <id>`` — обратная связь в базу знаний проекта;
* ``apply NN [--yes]`` — показать или применить предложенную правку автотеста.

Первая строка вывода всегда ``STATUS: <статус>``:
analyze | fix | propose | summary | done | diff | applied | saved | error.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from datetime import date, datetime
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
from alla_skill_lib.feedback import find_entry, remember, reject
from alla_skill_lib.history import append_run, load_history, loose_key, recurrence, run_records
from alla_skill_lib.kb import (
    ProjectKB,
    cluster_evidence,
    cluster_signature,
    default_fingerprint,
    find_kb_dir,
    match_cluster,
)
from alla_skill_lib.pipeline import LaunchData, collect_launch
from alla_skill_lib.proposals import (
    Proposal,
    apply_proposal,
    is_applied,
    parse_proposal,
    validate_proposal,
)
from alla_skill_lib.report import build_summary_task, render_green_report, render_report

logger = logging.getLogger(__name__)

MAX_FIX_ATTEMPTS = 3
MAX_PROPOSALS = 5
REPORT_BEGIN = "===ОТЧЁТ==="
REPORT_END = "===КОНЕЦ==="
EXPECTED_FORMAT = """\
ЧТО СЛОМАЛОСЬ: <1–2 предложения>
ПРИЧИНА: <тест|приложение|окружение|данные|неизвестно> — <обоснование>
КАК ИСПРАВИТЬ:
1. <шаг>
КОД: <путь от корня проекта>:<строка> — <что там>   (необязательно)
БАЗА ЗНАНИЙ: <id записи> | нет   (только если задание предлагало записи)"""
PROPOSAL_FORMAT = """\
РЕШЕНИЕ: исправить | не трогать
ФАЙЛ: <путь от корня проекта>:<строка>
БЫЛО:
<строки из файла дословно, с отступами>
СТАЛО:
<эти же строки после правки>
ПОЧЕМУ: <факт из данных и кода, почему это дефект автотеста>"""
PROPOSAL_RULES = """\
Предлагай правку, только если ОДНОВРЕМЕННО:
- в коде автотеста виден конкретный дефект (устаревший локатор, путь, поле или
  ожидаемое значение без признаков бага приложения; нет ожидания асинхронного
  результата; неверная подготовка или очистка данных в самом тесте; ошибка в
  шаге или хелпере автотестов);
- в логе приложения нет ошибки, которая объясняет падение;
- правка меняет только код автотестов.
Нельзя: ослаблять или удалять проверки, отключать тест, глушить исключения,
добавлять sleep или увеличивать таймауты. Сомневаешься — «не трогать».
Файлы проекта сейчас НЕ меняй: правку применит команда apply после согласия
пользователя."""


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
    if args.command == "next":
        return cmd_next(args.run_dir, reports_dir)
    if args.command == "remember":
        return _cluster_command(args, reports_dir, lambda paths, run, entry: remember(
            paths, run, entry, args.entry, date.today(), from_analysis=args.from_analysis
        ))
    if args.command == "reject":
        return _cluster_command(args, reports_dir, lambda paths, run, entry: reject(
            paths, run, entry, args.entry_id
        ))
    if args.command == "apply":
        return _cluster_command(args, reports_dir, lambda paths, run, entry: _apply(
            paths, run, entry, confirm=args.yes
        ))
    raise AssertionError(f"неизвестная команда {args.command}")


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

    # Папка разбора обязательна: после нового prepare «последний» разбор — уже
    # другой прогон, и обратная связь или правка ушли бы не туда.
    run_option = argparse.ArgumentParser(add_help=False)
    run_option.add_argument(
        "--run", required=True, help="папка разбора, к отчёту которого относится команда"
    )
    run_option.add_argument("cluster", help="номер проблемы из отчёта: 3 или 03")
    keep = commands.add_parser(
        "remember", parents=[common, run_option],
        help="сохранить причину и рецепт из обратной связи в базу знаний проекта",
    )
    keep.add_argument("--entry", help="id существующей записи, которую нужно обновить")
    keep.add_argument(
        "--from-analysis",
        action="store_true",
        help="сохранить разбор модели как есть (пользователь подтвердил его без правок)",
    )
    drop = commands.add_parser(
        "reject", parents=[common, run_option],
        help="запомнить, что запись базы знаний к этой ошибке не относится",
    )
    drop.add_argument("entry_id", help="id записи базы знаний")
    apply = commands.add_parser(
        "apply", parents=[common, run_option], help="показать или применить правку автотеста",
    )
    apply.add_argument("--yes", action="store_true", help="применить (только после согласия пользователя)")
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
    known = sum(1 for entry in run["clusters"] if entry.get("kb"))
    print(f"STATUS: {status}")
    print(
        f"Прогон #{launch_id}{name}: тестов {counts['total']}, "
        f"активных падений {counts['active_failures']}, кластеров {len(run['clusters'])}"
        + (f", из них с записями базы знаний: {known}." if known else ".")
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
    kb_dir = find_kb_dir(project_root)
    kb_records, kb_warnings = ProjectKB(kb_dir).load()
    history = load_history(paths.reports_dir)
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
        entry: dict[str, Any] = {
            "file_id": file_id,
            "cluster_id": cluster.cluster_id,
            "label": label,
            "member_count": cluster.member_count,
            "auto": auto,
            "signature": None,
            "loose_key": None,
            "fingerprint": "",
            "kb": [],
            "history": None,
        }
        entries.append(entry)
        if auto:
            ws.write_text(paths.analysis(file_id), no_evidence_analysis())
            continue

        message, trace, representative_log = cluster_evidence(cluster, tests_by_id)
        evidence = "\n".join(part for part in (message, trace, representative_log) if part)
        ws.write_text(paths.evidence(file_id), evidence)
        signature = cluster_signature(cluster, tests_by_id)
        kb_matches = match_cluster(kb_records, signature, evidence)
        loose = loose_key(message, trace)
        entry.update({
            "signature": signature,
            "loose_key": loose,
            "fingerprint": default_fingerprint(message, trace, representative_log),
            "kb": kb_matches,
            "history": recurrence(
                history,
                launch_id=triage.launch_id,
                signature=signature,
                loose=loose,
                kb_ids={match["id"] for match in kb_matches},
            ),
        })

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
            kb_matches=kb_matches,
            recurrence=entry["history"],
        )
        ws.write_text(paths.cluster_task(file_id), task)

    run = {
        "schema": ws.RUN_SCHEMA,
        "launch_id": triage.launch_id,
        "launch_name": triage.launch_name,
        "launch_url": f"{settings.endpoint.rstrip('/')}/launch/{triage.launch_id}",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(project_root),
        "kb_dir": str(kb_dir),
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
        "warnings": [*data.warnings, *kb_warnings],
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
        return "done", _done_body(console, paths, {})

    schema = int(run.get("schema", 1))
    project_root = Path(run["project_root"])
    state = ws.read_json(paths.state_json) if paths.state_json.is_file() else {}
    state.setdefault("attempts", {})
    entries = run["clusters"]
    total = len(entries)
    analyses: dict[str, ClusterAnalysis] = {}
    flagged: set[str] = set()
    proposals: dict[str, Proposal] = {}
    candidates = 0

    for position, entry in enumerate(entries, start=1):
        file_id = entry["file_id"]
        analysis_path = paths.analysis(file_id)
        text = analysis_path.read_text(encoding="utf-8") if analysis_path.is_file() else ""
        if not text.strip():
            done = sum(1 for item in entries if _has_text(paths.analysis(item["file_id"])))
            return "analyze", _analyze_body(paths, entry, position, total, done)

        analysis = parse_analysis(text)
        offered = frozenset(match["id"] for match in entry.get("kb", []))
        errors = validate_analysis(analysis, project_root, offered)
        if errors:
            attempt = _register_invalid(state, file_id, text, paths)
            if attempt < MAX_FIX_ATTEMPTS:
                return "fix", _fix_body(paths, entry, position, total, errors, attempt)
            flagged.add(file_id)
        analyses[file_id] = analysis

        if (
            schema >= 2
            and file_id not in flagged
            and not entry["auto"]
            and analysis.category == "тест"
            and analysis.code
        ):
            candidates += 1
            if candidates <= MAX_PROPOSALS:
                outcome = _proposal_step(paths, state, entry, position, total, project_root)
                if isinstance(outcome, tuple):
                    return outcome
                if outcome is not None:
                    proposals[file_id] = outcome

    summary = paths.summary.read_text(encoding="utf-8") if paths.summary.is_file() else ""
    if not summary.strip():
        ws.write_text(paths.summary_task, build_summary_task(run, analyses, flagged, paths))
        return "summary", _summary_body(paths, total)

    if schema >= 2 and not state.get("history_written"):
        append_run(paths.reports_dir, run_records(run, analyses, flagged, paths.root.name))
        state["history_written"] = True
        ws.write_json(paths.state_json, state)

    fixes = {file_id: p for file_id, p in proposals.items() if p.is_fix}
    applied = {file_id for file_id, p in fixes.items() if is_applied(p, project_root)}
    console, full = render_report(run, analyses, flagged, summary, paths, fixes, applied)
    ws.write_text(paths.report, full)
    return "done", _done_body(console, paths, fixes, schema >= 2)


def _proposal_step(
    paths: ws.RunPaths,
    state: dict[str, Any],
    entry: dict[str, Any],
    position: int,
    total: int,
    project_root: Path,
) -> tuple[str, str] | Proposal | None:
    """Шаг предложения правки: (статус, текст), принятое предложение или None (отброшено)."""
    file_id = entry["file_id"]
    path = paths.proposal(file_id)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not text.strip():
        return "propose", _propose_body(paths, entry, position, total)
    proposal = parse_proposal(text)
    errors = validate_proposal(proposal, project_root)
    if not errors or is_applied(proposal, project_root):
        return proposal
    attempt = _register_invalid(state, f"proposal-{file_id}", text, paths)
    if attempt < MAX_FIX_ATTEMPTS:
        return "fix", "\n".join([
            f"{_cluster_caption(entry, position, total)} — предложение правки не прошло "
            f"проверку (попытка {attempt} из {MAX_FIX_ATTEMPTS}):",
            *(f"- {error}" for error in errors),
            f"Исправь файл: {path}",
            "Если уверенности нет — запиши «РЕШЕНИЕ: не трогать» и «ПОЧЕМУ: …».",
            "Формат:",
            PROPOSAL_FORMAT,
            f"Затем выполни: {paths.next_command()}",
        ])
    return None


def _has_text(path: Path) -> bool:
    return path.is_file() and bool(path.read_text(encoding="utf-8").strip())


def _register_invalid(
    state: dict[str, Any],
    key: str,
    text: str,
    paths: ws.RunPaths,
) -> int:
    """Посчитать попытку исправления; одна и та же версия файла считается один раз."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    record = state["attempts"].get(key, {"count": 0, "hash": None})
    if record["hash"] != digest:
        record = {"count": record["count"] + 1, "hash": digest}
        state["attempts"][key] = record
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


def _propose_body(paths: ws.RunPaths, entry: dict[str, Any], position: int, total: int) -> str:
    return "\n".join([
        f"{_cluster_caption(entry, position, total)}: по разбору виноват автотест. "
        "Реши, можно ли исправить его код.",
        PROPOSAL_RULES,
        f"1. Посмотри код из строки «КОД:» разбора: {paths.analysis(entry['file_id'])}",
        f"2. Запиши решение в файл: {paths.proposal(entry['file_id'])}",
        "Формат:",
        PROPOSAL_FORMAT,
        "Для «не трогать» достаточно РЕШЕНИЕ и ПОЧЕМУ.",
        f"3. Выполни: {paths.next_command()}",
    ])


def _summary_body(paths: ws.RunPaths, total: int) -> str:
    return "\n".join([
        f"Все кластеры разобраны ({total}). Осталось написать общий анализ прогона.",
        f"1. Прочитай задание: {paths.summary_task}",
        f"2. Запиши общий анализ в файл: {paths.summary}",
        f"3. Выполни: {paths.next_command()}",
    ])


def _done_body(
    console: str,
    paths: ws.RunPaths,
    fixes: dict[str, Proposal],
    feedback: bool = False,
) -> str:
    lines = [
        f"Отчёт сохранён: {paths.report}",
        "Выведи пользователю текст между маркерами дословно, без сокращений:",
        REPORT_BEGIN,
        console,
        REPORT_END,
    ]
    if fixes or feedback:
        lines.append("После отчёта:")
    for file_id in fixes:
        show = ws.skill_command("apply", file_id, "--run", str(paths.root))
        lines.append(
            f"- Правка автотеста для проблемы {int(file_id)}: покажи пользователю diff ({show}) "
            "и спроси, применить ли. Только после явного «да» выполни ту же команду с --yes. "
            "Тесты не запускай."
        )
    if feedback:
        run = str(paths.root)
        lines += [
            "- Если пользователь назовёт причину или рецепт для проблемы N — сохрани их по "
            "разделу «Обратная связь» в SKILL.md. Команды для ЭТОГО разбора (всегда с этим --run):",
            f"  файл обратной связи: {paths.root / 'feedback'}/NN.md",
            f"  {ws.skill_command('remember', 'N', '--run', run)}",
            f"  разбор подтверждён как есть: {ws.skill_command('remember', 'N', '--run', run, '--from-analysis')}",
            f"  известная проблема не подходит: {ws.skill_command('reject', 'N', '<id>', '--run', run)}",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# remember / reject / apply
# ---------------------------------------------------------------------------


def _cluster_command(args: argparse.Namespace, reports_dir: Path, action: Any) -> int:
    """Общая обвязка команд, работающих с одним кластером разбора."""
    try:
        paths = ws.resolve_run(args.run, reports_dir)
    except ws.RunNotFoundError as exc:
        print("STATUS: error")
        print(exc)
        return 1
    run = ws.read_json(paths.run_json)
    entry = find_entry(run, args.cluster)
    if entry is None:
        print("STATUS: error")
        print(f"В разборе {paths.root} нет проблемы №{args.cluster}.")
        return 1
    status, body = action(paths, run, entry)
    print(f"STATUS: {status}")
    print(body)
    return 0 if status in ("saved", "diff", "applied") else 1


def _apply(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entry: dict[str, Any],
    *,
    confirm: bool,
) -> tuple[str, str]:
    path = paths.proposal(entry["file_id"])
    if not path.is_file():
        return "error", f"Для проблемы №{int(entry['file_id'])} нет предложения правки."
    status, body = apply_proposal(
        parse_proposal(path.read_text(encoding="utf-8")),
        Path(run["project_root"]),
        confirm=confirm,
    )
    if status == "diff":
        body += (
            "\nПокажи этот diff пользователю. Применить: "
            + ws.skill_command("apply", entry["file_id"], "--run", str(paths.root), "--yes")
            + " — только после явного «да»."
        )
    return status, body


if __name__ == "__main__":
    sys.exit(main())
