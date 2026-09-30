"""CLI скилла alla-launch.

* ``prepare <launch_id|URL>`` — получить прогон из TestOps, кластеризовать
  падения, сопоставить с базой знаний проекта и историей, разложить задания;
  неоконченный разбор того же прогона продолжает (``--fresh`` — начать заново);
* ``next [run_dir] [--workers N | --serial]`` — конечный автомат: смотрит на файлы в
  папке разбора и печатает, что агенту делать дальше; когда кластеров без разбора много
  (``PARALLEL_MIN_PENDING`` и больше), раздаёт их пакетами субагентам (``analyze_batch``);
* ``verify NN [NN…] --run DIR`` — только читающая проверка разборов (для субагентов пакета);
* ``skip NN`` — пропустить кластер по просьбе пользователя;
* ``remember NN`` / ``reject NN <id>`` — обратная связь в базу знаний проекта;
* ``apply NN [--yes --diff ХЭШ] [--repeat]`` / ``revert NN`` — показать, применить или откатить правку автотеста;
* ``check`` — проверить окружение и доступ к TestOps; ``clean`` — удалить старые разборы.

Первая строка вывода всегда ``STATUS: <статус>``:
analyze | analyze_batch | fix | propose | summary | done | diff | applied | reverted | saved |
ok | ready | error.
Код возврата 0 всегда, кроме ``error``: статус с инструкцией — не авария.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from alla_core.config import Settings
from alla_core.exceptions import AllaError, ConfigurationError
from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import (
    EXPECTED_FORMAT,
    ClusterAnalysis,
    parse_analysis,
    parse_summary,
    validate_analysis,
)
from alla_skill_lib.batch_task import render_batch_task
from alla_skill_lib.cluster_task import (
    build_cluster_task,
    failed_prepare_analysis,
    has_evidence,
    no_evidence_analysis,
    skipped_analysis,
    project_frames,
    select_log_and_trace,
)
from alla_skill_lib.code_hints import ProjectIndex, hints_for_cluster
from alla_skill_lib.errors import fetch_error_hint
from alla_skill_lib.feedback import FEEDBACK_FORMAT, find_entry, remember, reject
from alla_skill_lib.history import append_run, load_history, recurrence, run_records
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
    ApplyResult,
    Proposal,
    ProposalFiles,
    apply_proposal,
    applied_state,
    parse_proposal,
    revert_proposal,
    validate_proposal,
)
from alla_skill_lib.report import build_summary_task, render_green_report, render_report

logger = logging.getLogger(__name__)

MAX_FIX_ATTEMPTS = 3
MAX_PROPOSALS = 5
# Много неразобранных кластеров раздаются субагентам пакетами (SKILL.md, STATUS: analyze_batch).
PARALLEL_MIN_PENDING = 10  # с такого числа кластеров без разбора включается пакетный режим
BATCH_SIZE = 6  # кластеров в пакете одного субагента
DEFAULT_WORKERS = 4  # пакетов (субагентов) за одну волну; 1 — кластеры по одному
MAX_WORKERS = 8
# Модель, которая не меняет файл, но снова зовёт next: после стольких вызовов
# подряд без правки попытка засчитывается, и разбор не зависает навсегда.
UNCHANGED_CALLS_PER_ATTEMPT = 3
_LAUNCH_URL_RE = re.compile(r"/launch(?:es)?/(\d+)")
REPORT_BEGIN = "===ОТЧЁТ==="
REPORT_END = "===КОНЕЦ==="
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
    try:
        return _dispatch(argv)
    except Exception as exc:  # без STATUS модель не поймёт, что делать дальше
        logger.exception("Необработанная ошибка скилла")
        print("STATUS: error")
        print(f"Внутренняя ошибка скилла: {type(exc).__name__}: {exc}")
        print(
            "Подробности (traceback) — в stderr. Покажи это пользователю и остановись; "
            "команду вслепую не повторяй."
        )
        return 1


def _exit_code(status: str) -> int:
    """Только ``error`` — авария; остальные статусы несут инструкцию для агента."""
    return 1 if status == "error" else 0


def _dispatch(argv: list[str] | None) -> int:
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
        return cmd_prepare(args.launch_id, project_root, reports_dir, fresh=args.fresh)
    if args.command == "next":
        return cmd_next(
            args.run_dir or args.run, reports_dir, workers=1 if args.serial else args.workers
        )
    if args.command == "verify":
        return cmd_verify(args.run, args.clusters, reports_dir)
    if args.command == "check":
        return cmd_check(project_root, reports_dir)
    if args.command == "clean":
        return cmd_clean(reports_dir, args.older_than_days, dry_run=args.dry_run)
    if args.command == "remember":
        return _cluster_command(args, reports_dir, lambda paths, run, entry: remember(
            paths, run, entry, args.entry, date.today(), from_analysis=args.from_analysis
        ))
    if args.command == "reject":
        return _cluster_command(args, reports_dir, lambda paths, run, entry: reject(
            paths, run, entry, args.entry_id
        ))
    if args.command == "skip":
        return _cluster_command(args, reports_dir, lambda paths, run, entry: _skip(
            paths, entry, args.reason
        ))
    if args.command == "apply":
        return _cluster_command(args, reports_dir, lambda paths, run, entry: _apply(
            paths, run, entry, confirm=args.yes, diff_hash=args.diff_hash, repeat=args.repeat
        ))
    if args.command == "revert":
        return _cluster_command(args, reports_dir, lambda paths, run, entry: _revert(
            paths, run, entry
        ))
    raise AssertionError(f"неизвестная команда {args.command}")


class _Parser(argparse.ArgumentParser):
    """Ошибка аргументов тоже начинается с ``STATUS:`` — так её видит агент."""

    def error(self, message: str) -> Any:
        print("STATUS: error")
        print(f"Неверные аргументы команды: {message}")
        print(self.format_usage().strip())
        raise SystemExit(2)


def _launch_id(text: str) -> int:
    """Номер запуска: ``12345``, ``#12345`` или ссылка ``…/launch/12345``."""
    value = text.strip().lstrip("#")
    if value.isdigit():
        return int(value)
    match = _LAUNCH_URL_RE.search(value)
    if match:
        return int(match.group(1))
    raise argparse.ArgumentTypeError(
        f"не вижу номера запуска в «{text}»: нужен ID числом или ссылка вида …/launch/12345"
    )


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
    parser = _Parser(prog="alla_skill.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", parents=[common], help="получить прогон из TestOps и подготовить задания"
    )
    prepare.add_argument(
        "launch_id", type=_launch_id, help="ID прогона (launch) в Allure TestOps или ссылка на него"
    )
    prepare.add_argument(
        "--fresh",
        action="store_true",
        help="начать разбор заново, даже если есть неоконченный разбор этого прогона",
    )
    step = commands.add_parser("next", parents=[common], help="следующий шаг разбора")
    step.add_argument("run_dir", nargs="?", help="папка разбора (по умолчанию последняя)")
    step.add_argument("--run", help="то же, что позиционная папка разбора")
    mode = step.add_mutually_exclusive_group()
    mode.add_argument(
        "--workers", type=int,
        help=f"сколько субагентов разбирают кластеры параллельно (1–{MAX_WORKERS}; по умолчанию "
             f"{DEFAULT_WORKERS}); значение запоминается для этого разбора",
    )
    mode.add_argument(
        "--serial", action="store_true",
        help="разбирать кластеры по одному, без субагентов (то же, что --workers 1)",
    )
    check_run = commands.add_parser(
        "verify", parents=[common],
        help="проверить формат разборов кластеров, ничего не меняя (для субагентов пакета)",
    )
    check_run.add_argument("--run", required=True, help="папка разбора")
    check_run.add_argument("clusters", nargs="+", help="номера проблем из пакета: 3 или 03")
    commands.add_parser(
        "check", parents=[common], help="проверить окружение, настройки и доступ к TestOps"
    )
    cleaner = commands.add_parser("clean", parents=[common], help="удалить старые папки разборов")
    cleaner.add_argument(
        "--older-than-days", type=int, default=14, help="старше скольки дней (по умолчанию 14)"
    )
    cleaner.add_argument("--dry-run", action="store_true", help="только показать, что будет удалено")

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
    skipper = commands.add_parser(
        "skip", parents=[common, run_option],
        help="пропустить кластер без разбора (только по просьбе пользователя)",
    )
    skipper.add_argument("--reason", default="", help="почему пропущен (попадёт в отчёт)")
    apply = commands.add_parser(
        "apply", parents=[common, run_option], help="показать или применить правку автотеста",
    )
    apply.add_argument("--yes", action="store_true", help="применить (только после согласия пользователя)")
    apply.add_argument(
        "--diff", dest="diff_hash",
        help="хэш diff, который только что показала команда без --yes (применяется ровно он)",
    )
    apply.add_argument(
        "--repeat", action="store_true",
        help="повторить правку, которая уже применялась и состояние которой неизвестно "
             "(только по явной просьбе пользователя)",
    )
    commands.add_parser(
        "revert", parents=[common, run_option],
        help="вернуть файл, изменённый командой apply, если его после этого не меняли",
    )
    return parser


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def _progress(message: str) -> None:
    """Стадии выгрузки — в stderr, чтобы первой строкой stdout оставался STATUS."""
    print(message, file=sys.stderr, flush=True)


def cmd_prepare(
    launch_id: int,
    project_root: Path,
    reports_dir: Path,
    *,
    fresh: bool = False,
) -> int:
    if not fresh:
        unfinished = ws.find_unfinished_run(reports_dir, launch_id)
        if unfinished is not None:
            return _resume(unfinished, launch_id, reports_dir)

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
        data = asyncio.run(collect_launch(launch_id, settings, progress=_progress))
    except (AllaError, httpx.HTTPError) as exc:
        return _fetch_failed(launch_id, exc, settings, env_file)
    except Exception as exc:  # неожиданный ответ API и т.п. — traceback в stderr
        logger.exception("Сбой при получении прогона")
        print("STATUS: error")
        print(f"Сбой при получении прогона #{launch_id}: {type(exc).__name__}: {exc}")
        return 1

    if data.triage.total_results == 0:
        print("STATUS: error")
        print(
            f"В прогоне #{launch_id} нет ни одного результата: он пуст или ещё не начался. "
            "Проверь номер запуска и проект."
        )
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
    for warning in run["warnings"]:
        print(f"Внимание: {warning}")
    print(body)
    return 0


def _fetch_failed(
    launch_id: int,
    exc: BaseException,
    settings: Settings,
    env_file: Path,
) -> int:
    print("STATUS: error")
    print(f"Не удалось получить прогон #{launch_id} из TestOps: {exc}")
    hint = fetch_error_hint(exc, settings, env_file)
    if hint:
        print(hint)
    return 1


def _resume(paths: ws.RunPaths, launch_id: int, reports_dir: Path) -> int:
    """Продолжить неоконченный разбор вместо нового: выгрузка не повторяется."""
    ws.remember_last_run(reports_dir, paths)
    run = ws.read_json(paths.run_json)
    status, body = next_step(paths)
    name = f" «{run['launch_name']}»" if run.get("launch_name") else ""
    print(f"STATUS: {status}")
    print(
        f"Продолжаю неоконченный разбор прогона #{launch_id}{name} "
        f"(создан {run.get('created_at', '?')}); данные из TestOps заново не запрашиваю."
    )
    print(f"Начать заново: {ws.skill_command('prepare', str(launch_id), '--fresh')}")
    print(f"Папка разбора: {paths.root}")
    print(body)
    return _exit_code(status)


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
    local_warnings: list[str] = []

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
            "fingerprint": "",
            "kb": [],
            "history": None,
        }
        entries.append(entry)
        if auto:
            ws.write_text(paths.analysis(file_id), no_evidence_analysis())
            continue

        try:
            message, trace, representative_log = cluster_evidence(cluster, tests_by_id)
            evidence = "\n".join(part for part in (message, trace, representative_log) if part)
            ws.write_text(paths.evidence(file_id), evidence)
            signature = cluster_signature(cluster, tests_by_id)
            kb_matches = match_cluster(kb_records, signature, evidence)
            entry.update({
                "signature": signature,
                "fingerprint": default_fingerprint(message, trace, representative_log),
                "kb": kb_matches,
                "history": recurrence(
                    history,
                    launch_id=triage.launch_id,
                    signature=signature,
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
        except Exception as exc:  # один битый кластер не должен ронять весь prepare
            logger.exception("Не удалось подготовить кластер %s", file_id)
            local_warnings.append(
                f"Проблема {position}: задание не подготовлено ({type(exc).__name__}: {exc}) — "
                "причина не определена."
            )
            entry.update(
                auto=True, signature=None, fingerprint="", kb=[], history=None
            )
            paths.evidence(file_id).unlink(missing_ok=True)
            paths.cluster_task(file_id).unlink(missing_ok=True)
            ws.write_text(paths.analysis(file_id), failed_prepare_analysis(type(exc).__name__))

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
        "warnings": [*data.warnings, *kb_warnings, *local_warnings],
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


def cmd_next(run_dir: str | None, reports_dir: Path, workers: int | None = None) -> int:
    try:
        paths = ws.resolve_run(run_dir, reports_dir)
    except ws.RunNotFoundError as exc:
        print("STATUS: error")
        print(exc)
        return 1
    if workers is not None:
        if not 1 <= workers <= MAX_WORKERS:
            print("STATUS: error")
            print(f"--workers: нужно число от 1 до {MAX_WORKERS} (1 — по одному, без субагентов).")
            return 1
        state = _read_state(paths)
        state["workers"] = workers
        ws.write_json(paths.state_json, state)
    status, body = next_step(paths)
    run = ws.read_json(paths.run_json)
    name = f" «{run['launch_name']}»" if run.get("launch_name") else ""
    source = "" if run_dir else " · взят последний разбор — если работаешь с другим, укажи его папку"
    print(f"STATUS: {status}")
    # Каждый вывод называет прогон: после сжатия контекста легко продолжить чужой.
    print(f"Прогон #{run['launch_id']}{name} · папка {paths.root}{source}")
    print(body)
    return _exit_code(status)


def next_step(paths: ws.RunPaths) -> tuple[str, str]:
    """Определить следующий шаг по состоянию файлов. Идемпотентно."""
    run = ws.read_json(paths.run_json)
    if not run["clusters"]:
        console, full = render_green_report(run, paths)
        ws.write_text(paths.report, full)
        return "done", _done_body(console, paths, {})

    project_root = Path(run["project_root"])
    state = _read_state(paths)
    entries = run["clusters"]
    total = len(entries)
    manual_total = sum(1 for item in entries if not item["auto"])
    pending = [
        item["file_id"] for item in entries
        if not item["auto"] and not _has_text(paths.analysis(item["file_id"]))
    ]
    workers = int(state.get("workers", DEFAULT_WORKERS))
    if workers > 1 and len(pending) >= PARALLEL_MIN_PENDING:
        return "analyze_batch", _batch_body(paths, run, pending, manual_total, workers)
    analyses: dict[str, ClusterAnalysis] = {}
    flagged: set[str] = set()
    proposals: dict[str, Proposal] = {}
    not_proposed: dict[str, str] = {}
    notes: list[str] = []
    candidates = 0

    for position, entry in enumerate(entries, start=1):
        file_id = entry["file_id"]
        analysis_path = paths.analysis(file_id)
        text = ws.read_text(analysis_path) if analysis_path.is_file() else ""
        if not text.strip():
            done = sum(
                1 for item in entries
                if not item["auto"] and _has_text(paths.analysis(item["file_id"]))
            )
            return "analyze", _analyze_body(paths, entry, position, total, done, manual_total)

        analysis, errors = _check_analysis(text, entry, project_root)
        if errors:
            attempt, unchanged = _register_invalid(state, file_id, text, paths)
            if attempt < MAX_FIX_ATTEMPTS:
                return "fix", _fix_body(
                    paths, entry, position, total, errors, attempt, analysis, unchanged
                )
            flagged.add(file_id)
        analyses[file_id] = analysis

        if (
            file_id not in flagged
            and not entry["auto"]
            and analysis.category == "тест"
            and analysis.code
        ):
            candidates += 1
            if candidates > MAX_PROPOSALS:
                not_proposed[file_id] = (
                    f"лимит — не больше {MAX_PROPOSALS} предложений правок на один разбор"
                )
                continue
            outcome = _proposal_step(paths, state, entry, position, total, project_root)
            if isinstance(outcome, tuple):
                return outcome
            if outcome is None:
                not_proposed[file_id] = (
                    f"предложение правки не прошло проверку за {MAX_FIX_ATTEMPTS} "
                    "попытки и отброшено"
                )
            else:
                proposals[file_id] = outcome

    skipped = [file_id for file_id in state.get("skipped", []) if file_id in analyses]
    if skipped:
        notes.append(
            "Пропущено без разбора по просьбе пользователя: "
            + ", ".join(str(int(file_id)) for file_id in skipped)
            + "."
        )

    summary = ws.read_text(paths.summary) if paths.summary.is_file() else ""
    if not summary.strip():
        ws.write_text(paths.summary_task, build_summary_task(run, analyses, flagged, paths))
        return "summary", _summary_body(paths, total)

    if not state.get("history_written"):
        append_run(paths.reports_dir, run_records(run, analyses, flagged, paths.root.name))
        state["history_written"] = True
        ws.write_json(paths.state_json, state)

    fixes = {file_id: p for file_id, p in proposals.items() if p.is_fix}
    states = {
        file_id: applied_state(p, project_root, _proposal_files(paths, file_id))
        for file_id, p in fixes.items()
    }
    console, full = render_report(
        run, analyses, flagged, summary, paths, proposals, states, notes, not_proposed
    )
    ws.write_text(paths.report, full)
    # Правку с неизвестным состоянием apply не применит — модели её показывать не нужно.
    offered = {file_id: p for file_id, p in fixes.items() if states[file_id] != "unknown"}
    return "done", _done_body(console, paths, offered, feedback=True)


def _read_state(paths: ws.RunPaths) -> dict[str, Any]:
    state = ws.read_json(paths.state_json) if paths.state_json.is_file() else {}
    state.setdefault("attempts", {})
    return state


def _check_analysis(
    text: str,
    entry: dict[str, Any],
    project_root: Path,
) -> tuple[ClusterAnalysis, list[str]]:
    """Разобрать текст разбора кластера и проверить его; пустой список ошибок — принят."""
    analysis = parse_analysis(text)
    offered = frozenset(match["id"] for match in entry.get("kb", []))
    return analysis, validate_analysis(analysis, project_root, offered)


def plan_batches(pending: list[str], size: int, workers: int) -> list[list[str]]:
    """Пакеты для одной волны: первые ``workers`` кусков по ``size`` кластеров, по порядку номеров.

    Считается заново из оставшихся кластеров при каждом ``next`` — состояния пакетов
    не хранится. Пусто, если пакетный режим выключен (``workers <= 1``).
    """
    if workers <= 1 or size < 1:
        return []
    return [pending[start:start + size] for start in range(0, len(pending), size)][:workers]


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
    text = ws.read_text(path) if path.is_file() else ""
    if not text.strip():
        return "propose", _propose_body(paths, entry, position, total)
    proposal = parse_proposal(text)
    errors = validate_proposal(proposal, project_root)
    # Применённая (или применённая и потом изменённая) правка перепроверки БЫЛО не проходит:
    # файл уже другой, и модель не должна переписывать из-за этого предложение.
    if not errors or applied_state(
        proposal, project_root, _proposal_files(paths, file_id)
    ) in ("applied", "unknown"):
        return proposal
    attempt, unchanged = _register_invalid(state, f"proposal-{file_id}", text, paths)
    if attempt < MAX_FIX_ATTEMPTS:
        return "fix", "\n".join([
            f"{_cluster_caption(entry, position, total)} — предложение правки не прошло "
            f"проверку (попытка {attempt} из {MAX_FIX_ATTEMPTS}):",
            *(f"- {error}" for error in errors),
            *([UNCHANGED_NOTE] if unchanged >= 2 else []),
            f"Исправь файл: {path}",
            "Если уверенности нет — запиши «РЕШЕНИЕ: не трогать» и «ПОЧЕМУ: …».",
            "Формат:",
            PROPOSAL_FORMAT,
            f"Затем выполни: {paths.next_command()}",
        ])
    return None


def _proposal_files(paths: ws.RunPaths, file_id: str) -> ProposalFiles:
    return ProposalFiles(
        record=paths.proposal_record(file_id),
        backup=paths.proposal_backup(file_id),
        patch=paths.proposal_patch(file_id),
    )


def _has_text(path: Path) -> bool:
    return path.is_file() and bool(ws.read_text(path).strip())


def _register_invalid(
    state: dict[str, Any],
    key: str,
    text: str,
    paths: ws.RunPaths,
) -> tuple[int, int]:
    """Посчитать попытку исправления: (номер попытки, вызовов ``next`` на этой версии).

    Новая версия файла — новая попытка. Тот же файл при повторных вызовах
    попыткой не считается, но только пока модель не зовёт ``next`` снова и
    снова, ничего не меняя: тогда каждый третий вызов засчитывается, и разбор
    доходит до пометки «формат нарушен», а не крутится вечно.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    record = dict(state["attempts"].get(key, {"count": 0, "hash": None, "calls": 0}))
    if record["hash"] != digest:
        record = {"count": record["count"] + 1, "hash": digest, "calls": 1}
    else:
        calls = int(record.get("calls", 1)) + 1
        if calls >= UNCHANGED_CALLS_PER_ATTEMPT:
            record["count"] += 1
            calls = 1
        record["calls"] = calls
    state["attempts"][key] = record
    ws.write_json(paths.state_json, state)
    return int(record["count"]), int(record["calls"])


UNCHANGED_NOTE = (
    "Файл не изменился с прошлого вызова next. Сначала исправь его (write_file), "
    "потом вызывай next; повторы без правки засчитываются как попытки."
)


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
    manual_total: int,
) -> str:
    automatic = total - manual_total
    progress = f"Готово разборов: {done} из {manual_total}"
    if automatic:
        progress += f" (ещё {automatic} без данных об ошибке разобраны автоматически)"
    return "\n".join([
        f"{_cluster_caption(entry, position, total)}. {progress}.",
        f"1. Прочитай задание: {paths.cluster_task(entry['file_id'])}",
        f"2. Запиши разбор в файл: {paths.analysis(entry['file_id'])}",
        f"3. Выполни: {paths.next_command()}",
    ])


def _batch_body(
    paths: ws.RunPaths,
    run: dict[str, Any],
    pending: list[str],
    manual_total: int,
    workers: int,
) -> str:
    by_id = {entry["file_id"]: entry for entry in run["clusters"]}
    batches = plan_batches(pending, BATCH_SIZE, workers)
    paths.batch(1).parent.mkdir(parents=True, exist_ok=True)
    taken = sum(len(ids) for ids in batches)
    lines = [
        f"Кластеров без разбора много: {len(pending)} из {manual_total}. Разбери их параллельно, "
        "раздав пакеты субагентам; сам эти кластеры не разбирай, скрипты-обёртки не пиши.",
        f"В этой волне: пакетов — {len(batches)}, кластеров — {taken}"
        + (f"; остальные ({len(pending) - taken}) — в следующей." if taken < len(pending) else "."),
        "1. Запусти субагентов инструментом agent — по одному на пакет, ВСЕ вызовы в одном "
        "сообщении, без subagent_type, с run_in_background: false. prompt каждого — дословно:",
    ]
    for number, ids in enumerate(batches, start=1):
        ws.write_text(
            paths.batch(number),
            render_batch_task(paths, number, [by_id[file_id] for file_id in ids], run["launch_id"]),
        )
        lines += [
            f"   Пакет {number} (кластеры {', '.join(ids)}):",
            "   «Ты — субагент разбора кластеров alla. Прочитай файл "
            f"{paths.batch(number)} и выполни его инструкции целиком. Больше ничего не делай. "
            "В ответе — одна строка.»",
        ]
    lines += [
        "2. Дождись, пока завершатся все субагенты. Их ответы не пересказывай и не перепроверяй: "
        "проверку сделает next.",
        f"3. Выполни: {paths.next_command()}",
        "Если инструмента agent нет или запустить субагентов не получилось — не пиши свои циклы, "
        "а переключись на разбор по одному: "
        f"{ws.skill_command('next', str(paths.root), '--serial')}",
    ]
    return "\n".join(lines)


def _fix_body(
    paths: ws.RunPaths,
    entry: dict[str, Any],
    position: int,
    total: int,
    errors: list[str],
    attempt: int,
    analysis: ClusterAnalysis,
    unchanged: int,
) -> str:
    return "\n".join([
        f"{_cluster_caption(entry, position, total)} — разбор не прошёл проверку "
        f"(попытка {attempt} из {MAX_FIX_ATTEMPTS}):",
        *(f"- {error}" for error in errors),
        parse_summary(analysis),
        *([UNCHANGED_NOTE] if unchanged >= 2 else []),
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
        "Выведи пользователю краткий разбор между маркерами дословно, без пересказа и "
        "собственных выводов. В конце него — ссылка на файл с полным разбором, оставь её как "
        "есть; подробности в чат не переписывай:",
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
            "и спроси, применить ли. Только после явного «да» выполни команду с --yes --diff, "
            "которую напечатает скрипт под diff. Тесты не запускай."
        )
    if feedback:
        run = str(paths.root)
        lines += [
            "- Если пользователь назовёт причину или рецепт для проблемы N (или подтвердит твой "
            "разбор) — сохрани их. Файл обратной связи пиши со слов пользователя, не додумывай; "
            "формат:",
            *(f"    {line}" for line in FEEDBACK_FORMAT.splitlines()),
            "  Команды для ЭТОГО разбора (всегда с этим --run):",
            f"  файл обратной связи: {paths.root / 'feedback'}/NN.md",
            f"  {ws.skill_command('remember', 'N', '--run', run)}",
            f"  разбор подтверждён как есть: {ws.skill_command('remember', 'N', '--run', run, '--from-analysis')}",
            f"  известная проблема не подходит: {ws.skill_command('reject', 'N', '<id>', '--run', run)}",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def cmd_verify(run_dir: str | None, clusters: list[str], reports_dir: Path) -> int:
    """Проверить разборы кластеров, ничего не записывая.

    Для субагентов пакета: ``next`` они вызывать не должны, потому что он пишет
    ``state.json`` и считает попытки исправления — параллельно это гонка.
    """
    try:
        paths = ws.resolve_run(run_dir, reports_dir)
    except ws.RunNotFoundError as exc:
        print("STATUS: error")
        print(exc)
        return 1
    run = ws.read_json(paths.run_json)
    entries = [find_entry(run, cluster) for cluster in clusters]
    unknown = [cluster for cluster, entry in zip(clusters, entries) if entry is None]
    if unknown:
        print("STATUS: error")
        print(f"В разборе {paths.root} нет проблем: {', '.join(unknown)}.")
        return 1
    project_root = Path(run["project_root"])
    lines: list[str] = []
    failed = 0
    for entry in entries:
        assert entry is not None
        file_id = entry["file_id"]
        path = paths.analysis(file_id)
        text = ws.read_text(path) if path.is_file() else ""
        if not text.strip():
            failed += 1
            lines.append(f"Кластер {file_id}: файл разбора пуст или не создан — запиши {path}")
            continue
        analysis, errors = _check_analysis(text, entry, project_root)
        if errors:
            failed += 1
            lines += [
                f"Кластер {file_id}: разбор не прошёл проверку:",
                *(f"- {error}" for error in errors),
                parse_summary(analysis),
                f"Исправь файл: {path}",
            ]
        else:
            lines.append(f"Кластер {file_id}: принят.")
    print("STATUS: fix" if failed else "STATUS: ok")
    print("\n".join(lines))
    if failed:
        print("Ожидаемый формат:")
        print(EXPECTED_FORMAT)
        print("После исправления повтори ту же команду проверки.")
    return 0


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
    return _exit_code(status)


def _apply(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entry: dict[str, Any],
    *,
    confirm: bool,
    diff_hash: str | None,
    repeat: bool = False,
) -> tuple[str, str]:
    file_id = entry["file_id"]
    path = paths.proposal(file_id)
    if not path.is_file():
        return "error", f"Для проблемы №{int(file_id)} нет предложения правки."
    result = apply_proposal(
        parse_proposal(ws.read_text(path)),
        Path(run["project_root"]),
        confirm=confirm,
        diff_hash=diff_hash,
        files=_proposal_files(paths, file_id),
        repeat=repeat,
    )
    if result.status == "diff":
        return "diff", result.text + _apply_hint(paths, file_id, result, repeat)
    if result.changed:
        return "applied", (
            result.text
            + "\nОткатить: " + ws.skill_command("revert", file_id, "--run", str(paths.root))
            + "\nТесты не запускай: предложи пользователю запустить исправленный тест."
        )
    return result.status, result.text


def _apply_hint(paths: ws.RunPaths, file_id: str, result: ApplyResult, repeat: bool = False) -> str:
    command = ws.skill_command(
        "apply", file_id, "--run", str(paths.root), "--yes", "--diff", str(result.diff_hash),
        *(["--repeat"] if repeat else []),
    )
    return (
        "\nПокажи этот diff пользователю (и строки «Проверь:», если есть). Применить: "
        f"{command} — только после явного «да»."
    )


def _revert(paths: ws.RunPaths, run: dict[str, Any], entry: dict[str, Any]) -> tuple[str, str]:
    result = revert_proposal(Path(run["project_root"]), _proposal_files(paths, entry["file_id"]))
    return result.status, result.text


def _skip(paths: ws.RunPaths, entry: dict[str, Any], reason: str) -> tuple[str, str]:
    """Записать заглушку разбора: кластер пропущен по просьбе пользователя."""
    file_id = entry["file_id"]
    if entry.get("auto"):
        return "error", f"Проблема №{int(file_id)} без данных об ошибке уже разобрана автоматически."
    ws.write_text(paths.analysis(file_id), skipped_analysis(reason))
    state = _read_state(paths)
    state["skipped"] = sorted({*state.get("skipped", []), file_id})
    ws.write_json(paths.state_json, state)
    return "saved", "\n".join([
        f"Кластер №{int(file_id)} пропущен и попадёт в отчёт как «неизвестно».",
        f"Продолжи разбор: {paths.next_command()}",
    ])


# ---------------------------------------------------------------------------
# check / clean
# ---------------------------------------------------------------------------


def cmd_check(project_root: Path, reports_dir: Path) -> int:
    """Проверить окружение и доступ к TestOps, ничего не создавая."""
    env_file = ws.SKILL_DIR / ".env"
    lines = [
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"Проект автотестов: {project_root}",
        f"Файл настроек: {env_file} — "
        + ("есть" if env_file.is_file() else "нет (значения берутся из переменных окружения)"),
    ]
    try:
        settings = Settings.load(env_file=env_file)
    except ConfigurationError as exc:
        print("STATUS: error")
        print(*lines, sep="\n")
        print(f"Ошибка конфигурации: {exc}")
        print(f"Заполни {env_file} по образцу .env.example. Содержимое .env не читай и не выводи.")
        return 2
    lines += [
        f"ALLURE_ENDPOINT: {settings.endpoint}",
        "ALLURE_TOKEN: задан (значение не показывается)",
        f"Проверка TLS: {'включена' if settings.ssl_verify else 'отключена (ALLURE_SSL_VERIFY=false)'}",
    ]
    kb_records, kb_warnings = ProjectKB(find_kb_dir(project_root)).load()
    lines.append(f"База знаний: {find_kb_dir(project_root)} — записей {len(kb_records)}")
    lines += [f"Внимание: {warning}" for warning in kb_warnings]
    try:
        asyncio.run(_ping_testops(settings))
    except (AllaError, httpx.HTTPError) as exc:
        print("STATUS: error")
        print(*lines, sep="\n")
        print(f"TestOps недоступен или токен не принят: {exc}")
        hint = fetch_error_hint(exc, settings, env_file)
        if hint:
            print(hint)
        return 1
    print("STATUS: ready")
    print(*lines, sep="\n")
    print("Доступ к TestOps: токен принят.")
    return 0


async def _ping_testops(settings: Settings) -> None:
    from alla_core.clients.auth import AllureAuthManager

    auth = AllureAuthManager(
        endpoint=settings.endpoint,
        api_token=settings.token,
        timeout=settings.request_timeout,
        ssl_verify=settings.ssl_verify,
    )
    await auth.get_auth_header()


def cmd_clean(reports_dir: Path, older_than_days: int, *, dry_run: bool) -> int:
    """Удалить папки разборов старше N дней; ``history.jsonl`` и ``.last_run`` остаются."""
    if not reports_dir.is_dir():
        print("STATUS: done")
        print(f"Папки отчётов {reports_dir} нет — удалять нечего.")
        return 0
    limit = time.time() - max(older_than_days, 0) * 86400
    removed: list[Path] = []
    for path in sorted(reports_dir.iterdir()):
        if not (path / "run.json").is_file():
            continue  # не папка разбора alla-launch
        if (path / "run.json").stat().st_mtime < limit:
            removed.append(path)
    for path in removed:
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
    verb = "Будут удалены" if dry_run else "Удалены"
    print("STATUS: done")
    print(f"{verb} папки разборов старше {older_than_days} дн.: {len(removed)}")
    print(*(f"- {path}" for path in removed), sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
