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
* ``rerun [N…] [--yes --plan ХЭШ | --wait]`` — перезапустить тесты проблем локально (Maven)
  фоновым заданием: нестабильный тест или нет (``rerun.py``); без номеров — тесты, похожие на
  нестабильные (``flaky.py``), которые ``done`` предложил после отчёта;
* ``check`` — проверить окружение и доступ к TestOps; ``clean`` — удалить старые разборы;
* ``extensions [list|enable|disable|run|action]`` — расширения скилла (``extensions.py``):
  показать, включить, выключить, повторить хук, выполнить действие расширения;
* ``review [--run DIR] [--no-model]`` — проверка законченного разбора для пилота (только по
  явной просьбе): факты из папки и журнала сеанса Qwen, оценка диагнозов моделью с чистым
  контекстом, ``review/report.md`` и сводка без данных прогона ``review/pilot-summary.md``.

Первая строка вывода всегда ``STATUS: <статус>``:
analyze | analyze_batch | fix | propose | summary | done | diff | applied | reverted | saved |
rerun | running | rerun_done | ok | ready | reviewed | error.
Код возврата 0 всегда, кроме ``error``: статус с инструкцией — не авария.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timezone
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from alla_core.config import Settings
from alla_core.exceptions import AllaError, ConfigurationError
from alla_core.models.clustering import FailureCluster
from alla_core.models.testops import FailedTestSummary
from alla_skill_lib import extensions as ext
from alla_skill_lib import flaky, jobs, rerun, resolve
from alla_skill_lib import workspace as ws
from alla_skill_lib.agent_rules import (
    ANALYSIS_FORMAT_REF,
    FEEDBACK_FORMAT_REF,
    MAX_PROJECT_FILES,
    PROBLEM_PREFIX,
    PROBLEM_REPORT_REF,
    PROPOSAL_FORMAT_REF,
    RECORD_PROBLEM,
    RELATED_FORMAT_REF,
    SUMMARY_FORMAT_REF,
    record_problem,
    reference_line,
)
from alla_skill_lib.analysis_format import (
    EXPECTED_FORMAT,
    ClusterAnalysis,
    parse_analysis,
    parse_summary,
)
from alla_skill_lib.batch_task import (
    BATCH_AGENT,
    MAX_VERIFY_ROUNDS,
    render_batch_task,
)
from alla_skill_lib.cluster_task import (
    TaskImage,
    build_cluster_task_with_sources,
    failed_prepare_analysis,
    has_evidence,
    no_evidence_analysis,
    skipped_analysis,
    project_frames,
    select_log_and_trace,
)
from alla_skill_lib.code_hints import ProjectIndex, hints_for_cluster
from alla_skill_lib.errors import USER_ACTION_NOTE, fetch_error_hint
from alla_skill_lib.feedback import FEEDBACK_FORMAT, find_entry, remember, remember_many, reject
from alla_skill_lib.history import append_changed_run, load_history, recurrence, run_records
from alla_skill_lib.images import place_images
from alla_skill_lib.known_issues import (
    KB_AUTO_NOT_PROPOSED,
    KB_AUTO_STATE,
    KnownIssues,
    KnownRecord,
    auto_resolution,
    current_kb_matches,
    known_auto_valid,
    known_issues,
    sync_known_auto,
)
from alla_skill_lib.kb import (
    KBRecord,
    ProjectKB,
    cluster_evidence,
    cluster_signature,
    cluster_tests,
    default_fingerprint,
    discover_kb_dirs,
    find_kb_dir,
    kb_dir_for,
    kb_label,
    match_cluster,
)
from alla_skill_lib.modules import ModuleResolver, resolve_run_modules
from alla_skill_lib.signature import cluster_log_errors, log_errors_digest
from alla_skill_lib.proposals import (
    ApplyResult,
    Proposal,
    ProposalFiles,
    apply_proposal,
    applied_state,
    parse_proposal,
    proposal_eligibility_errors,
    recorded_applied_state,
    revert_proposal,
    validate_proposal,
)
from alla_skill_lib.problem_state import CurrentState, confirmations, current_state
from alla_skill_lib.qwen_files import (
    install_batch_agent,
    install_commands,
    install_extension_agent,
    install_extension_command,
    remove_extension_agent,
    remove_extension_command,
)
from alla_skill_lib.related import (
    RELATED_FORMAT,
    Related,
    build_relate_task,
    candidate_stats,
    parse_related,
    relate_data,
    relation_scope,
    related_groups,
    validate_related,
)
from alla_skill_lib.session_log import note_session
from alla_skill_lib.report import (
    build_summary_data,
    build_summary_task,
    known_summary,
    render_green_report,
    render_report,
)
from alla_skill_lib.sources import (
    check_entry_analysis,
    example_blocks,
    observed,
    registry,
    write_registry,
)

if TYPE_CHECKING:
    from alla_skill_lib.pipeline import LaunchData

logger = logging.getLogger(__name__)

MAX_FIX_ATTEMPTS = 3
MAX_PROPOSALS = 5
# Долгие команды (prepare, review с моделью) — фоновым заданием (jobs.py): Qwen обрывает
# команду shell через 120 с. Синхронно — на Windows (нет fcntl, os.kill завершает процесс) и в
# тестах с подменами в памяти процесса.
RUN_IN_BACKGROUND = os.name != "nt"
# Папка задания, если команда сама идёт фоновым заданием (``--job``): туда пишется её стадия.
_JOB_DIR: Path | None = None
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
Одно предложение — одна правка: один файл и один блок БЫЛО/СТАЛО. Места рядом в
одном файле объедини в один блок; если исправлять нужно несколько файлов или
далёкие места, частичную правку не предлагай — «не трогать» и все места в ПОЧЕМУ.
Файлы проекта сейчас НЕ меняй: правку применит команда apply после согласия
пользователя."""
LAST_ATTEMPT_ANALYSIS = (
    "Это последняя попытка: если и она не пройдёт проверку, разбор останется как есть, "
    "а в отчёте у проблемы будет пометка «формат нарушен»."
)
LAST_ATTEMPT_PROPOSAL = (
    "Это последняя попытка: если и она не пройдёт проверку, предложение правки "
    "будет отброшено, а в отчёте останется пометка."
)
LAST_ATTEMPT_RELATED = (
    "Это последняя попытка: если и она не пройдёт проверку, похожие проблемы "
    "не объединятся."
)
RELATED_DROPPED_NOTE = (
    "Похожие проблемы не сравнены: решение агента (related.md) не прошло проверку за "
    f"{MAX_FIX_ATTEMPTS} попытки — проблемы показаны по отдельности."
)


def format_mismatch_note(paths: ws.RunPaths) -> str:
    return (
        "Если файл, по-твоему, уже соответствует справочнику, а проверка его отклоняет, — это "
        f"неполадка скилла: {record_problem(paths)}. Файл всё равно перепиши: замечания к форме "
        "исправь, а факты не подгоняй — цитату, которую проверка не находит, замени другой "
        "дословной строкой из того же куска или убери. Затем next; без правки next не вызывай. "
        "Не прошла и третья версия — разбор примут как есть с пометкой «формат нарушен»."
    )


def related_mismatch_note(paths: ws.RunPaths) -> str:
    return (
        "Сначала исправь файл по замечаниям выше — решения по проблемам сохрани. Только если "
        "файл уже соответствует справочнику, а проверка всё равно его отклоняет, — это неполадка "
        f"скилла: {record_problem(paths)}; тогда перепиши файл одной строкой «РАЗНЫЕ: …» со всеми "
        "номерами проблем из групп задания — проблемы не объединятся, но отчёт соберётся. "
        "Затем next; без правки next не вызывай."
    )


def proposal_mismatch_note(paths: ws.RunPaths) -> str:
    return (
        "Если файл, по-твоему, уже соответствует справочнику, а проверка его отклоняет, — это "
        f"неполадка скилла: {record_problem(paths)}. Перепиши файл как «РЕШЕНИЕ: не трогать», "
        "а в «ПОЧЕМУ:» назови место и правку, которую хотел предложить; если файл уже «не "
        "трогать» — исправь в нём то, на что указывают замечания. Затем next; без правки next "
        "не вызывай."
    )


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
            "Подробности (traceback) — в stderr. Пользователю покажи только строку «Внутренняя "
            "ошибка скилла: …» и остановись; команду вслепую не повторяй."
        )
        print(
            f"Это ошибка самого скилла (SKILL.md, «Проблемы скилла»): если папка разбора известна, "
            f"{RECORD_PROBLEM}. Эти указания пользователю не показывай. Файлы скилла не правь и "
            "обходных скриптов не пиши."
        )
        return 1


def _same_command(argv: list[str]) -> str:
    """Та же команда для повтора после ``running``: без ``--fresh`` (иначе разбор начался бы
    заново) и без служебного ``--job``."""
    kept: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--job":
            skip = True
        elif arg != "--fresh" and not arg.startswith("--job="):
            kept.append(arg)
    return ws.skill_command(*kept)


def _exit_code(status: str) -> int:
    """Только ``error`` — авария; остальные статусы несут инструкцию для агента."""
    return 1 if status == "error" else 0


def _dispatch(argv: list[str] | None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if argv[:1] == ["extensions"] and "--" in argv:
        # Всё после «--» — аргументы скрипта действия расширения, а не скилла.
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1:]
    args = _build_parser().parse_args(argv)
    args.extra = extra
    global _JOB_DIR
    _JOB_DIR = Path(args.job) if args.job else None
    project_root = (
        Path(args.project_root).resolve() if args.project_root else ws.detect_project_root()
    )
    reports_dir = (
        Path(args.reports_dir).resolve()
        if args.reports_dir
        else project_root / ws.REPORTS_DIRNAME
    )
    if args.command == "prepare":
        return cmd_prepare(args.launch_id, project_root, reports_dir, fresh=args.fresh,
                           in_job=_JOB_DIR is not None, repeat=_same_command(argv))
    if args.command == "next":
        return cmd_next(
            args.run_dir or args.run, reports_dir, workers=1 if args.serial else args.workers
        )
    if args.command == "verify":
        return cmd_verify(args.run, args.clusters, reports_dir)
    if args.command == "review":
        return cmd_review(args.run_dir or args.run, reports_dir, use_model=not args.no_model,
                          in_job=_JOB_DIR is not None)
    if args.command == "check":
        return cmd_check(project_root, reports_dir)
    if args.command == "clean":
        return cmd_clean(reports_dir, args.older_than_days, dry_run=args.dry_run)
    if args.command == "remember":
        if len(args.clusters) > 1:
            return _remember_many(args, reports_dir)
        args.cluster = args.clusters[0]
        return _cluster_command(args, reports_dir, lambda paths, run, entry: remember(
            paths, run, entry, args.entry, date.today(), from_analysis=args.from_analysis,
            from_extension=args.from_extension,
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
    if args.command == "extensions":
        return cmd_extensions(args, project_root, reports_dir)
    if args.command == "rerun":
        return cmd_rerun(args, reports_dir)
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
    common = _Parser(add_help=False)
    common.add_argument(
        "--project-root",
        help="корень проекта автотестов (по умолчанию определяется по .qwen/skills)",
    )
    common.add_argument(
        "--reports-dir",
        help="папка отчётов (по умолчанию <project>/alla-reports)",
    )
    # Служебный: команда идёт фоновым заданием этой папки (jobs.py), а не запускает его.
    common.add_argument("--job", help=argparse.SUPPRESS)
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
    review = commands.add_parser(
        "review", parents=[common],
        help="проверить законченный разбор (пилот): отчёт о ходе и качестве и сводка без данных",
    )
    review.add_argument("run_dir", nargs="?", help="папка разбора (по умолчанию последняя)")
    review.add_argument("--run", help="то же, что позиционная папка разбора")
    review.add_argument(
        "--no-model", action="store_true",
        help="без оценки диагнозов моделью: только факты из папки разбора и журнала сеанса",
    )
    cleaner = commands.add_parser("clean", parents=[common], help="удалить старые папки разборов")
    cleaner.add_argument(
        "--older-than-days", type=int, default=14, help="старше скольки дней (по умолчанию 14)"
    )
    cleaner.add_argument("--dry-run", action="store_true", help="только показать, что будет удалено")

    # Папка разбора обязательна: после нового prepare «последний» разбор — уже
    # другой прогон, и обратная связь или правка ушли бы не туда.
    run_option = _Parser(add_help=False)
    run_option.add_argument(
        "--run", required=True, help="папка разбора, к отчёту которого относится команда"
    )
    keep = commands.add_parser(
        "remember", parents=[common, run_option],
        help="сохранить причину и рецепт из обратной связи в базу знаний проекта",
    )
    keep.add_argument(
        "clusters", nargs="+", metavar="cluster",
        help="номер проблемы из отчёта: 3 или 03; несколько — только с --from-analysis",
    )
    run_option.add_argument("cluster", help="номер проблемы из отчёта: 3 или 03")
    keep.add_argument("--entry", help="id существующей записи, которую нужно обновить")
    source = keep.add_mutually_exclusive_group()
    source.add_argument(
        "--from-analysis",
        action="store_true",
        help="сохранить разбор модели как есть (пользователь подтвердил его без правок)",
    )
    source.add_argument(
        "--from-extension", metavar="ИМЯ",
        help="сохранить вывод расширения по проблеме (пользователь подтвердил его) — в базу "
             "знаний и в отчёт",
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
    again_tests = commands.add_parser(
        "rerun", parents=[common],
        help="перезапустить упавшие тесты проблем локально (Maven): нестабильные или нет",
    )
    again_tests.add_argument("clusters", nargs="*", metavar="N", help="номера проблем")
    again_tests.add_argument("--run", required=True, help="папка разбора")
    mode_rerun = again_tests.add_mutually_exclusive_group()
    mode_rerun.add_argument(
        "--yes", action="store_true",
        help="запустить (только после согласия пользователя); нужен --plan",
    )
    mode_rerun.add_argument(
        "--wait", action="store_true", help="показать ход или итог идущего перезапуска",
    )
    mode_rerun.add_argument(
        "--remember", action="store_true",
        help="сохранить итог: отметки в базе знаний и статус passed в TestOps (только после "
             "согласия пользователя); нужен --result",
    )
    again_tests.add_argument(
        "--plan", dest="plan_hash",
        help="хэш плана, который только что показала команда без --yes (запускается ровно он)",
    )
    again_tests.add_argument(
        "--result", dest="result_id", help="id итога перезапуска из ответа rerun_done",
    )
    extensions = commands.add_parser(
        "extensions", parents=[common], help="расширения скилла: список, включение, повтор, действия",
    )
    ext_commands = extensions.add_subparsers(dest="ext_command")
    ext_commands.add_parser("list", parents=[common], help="показать расширения и их состояние")
    for verb, text in (("enable", "включить"), ("disable", "выключить")):
        toggle = ext_commands.add_parser(verb, parents=[common], help=f"{text} расширение в проекте")
        toggle.add_argument("name", help="имя расширения")
    again = ext_commands.add_parser(
        "run", parents=[common],
        help="запустить расширение по просьбе пользователя (on_request) или повторить его хук: "
             "с номером проблемы — after_apply, без — after_report",
    )
    again.add_argument("name", help="имя расширения")
    again.add_argument("clusters", nargs="*", metavar="N", help="номера проблем")
    again.add_argument("--run", required=True, help="папка разбора")
    again.add_argument("--param", action="append", default=[], metavar="КЛЮЧ=ЗНАЧЕНИЕ",
                       help="параметр запроса для on_request (можно несколько)")
    again.add_argument("--text-file", action="append", nargs=2, default=[],
                       metavar=("KEY", "PATH"), help="полный UTF-8 текст из out_dir/*.txt")
    reconciliation = ext_commands.add_parser("reconcile", parents=[common],
                                             help="сверить запрос после потери ответа или clean")
    reconciliation.add_argument("name")
    reconciliation.add_argument("--request-id", required=True)
    action = ext_commands.add_parser(
        "action", parents=[common],
        help="выполнить действие расширения (из его задания); аргументы скрипта — после --",
    )
    action.add_argument("name", help="имя расширения")
    action.add_argument("action", help="имя действия")
    action.add_argument("--context", help="файл задания; без него — постоянный контекст")
    action.add_argument("--run", help="папка разбора: свежий контекст действия по ней")
    return parser


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def _progress(message: str) -> None:
    """Стадии выгрузки — в stderr, чтобы первой строкой stdout оставался STATUS; у фонового
    задания — ещё и в его папку (ответ ``running`` называет стадию)."""
    print(message, file=sys.stderr, flush=True)
    jobs.note_progress(_JOB_DIR, message)


def _finish_job(
    files: jobs.JobFiles,
    started: float,
    running: Callable[[jobs.JobState], str],
    repeat: str,
) -> int:
    """Дождаться фонового задания команды до ``jobs.WAIT_BUDGET`` от её начала и ответить:
    итог задания как есть, ``running`` (``running(state)`` — текст) или ошибка."""
    state = jobs.wait(files, jobs.WAIT_BUDGET - (time.monotonic() - started))
    if state.state == "done":
        output, code = jobs.deliver(files)
        print(output, end="" if output.endswith("\n") else "\n")
        return code
    if state.state == "running":
        print("STATUS: running")
        print(running(state))
        return 0
    jobs.mark_delivered(files)
    print("STATUS: error")
    print(f"Фоновый процесс команды завершился без итога. Подробности для пользователя — в файле "
          f"{files.log} (сам его не читай). Выполни команду ниже один раз; ответит этой же "
          "ошибкой — покажи этот текст пользователю и остановись.")
    print(repeat)
    return 1


def cmd_prepare(
    launch_id: int,
    project_root: Path,
    reports_dir: Path,
    *,
    fresh: bool = False,
    in_job: bool = False,
    repeat: str | None = None,
) -> int:
    """Выгрузить прогон и подготовить задания.

    Выгрузка большого прогона дольше, чем Qwen даёт команде (120 с), поэтому она идёт
    фоновым заданием (:mod:`alla_skill_lib.jobs`): команда ждёт до ``jobs.WAIT_BUDGET`` и
    отвечает ``running``, а повтор подключается к идущей выгрузке. ``in_job`` — это и есть
    фоновое задание: выгрузка здесь, синхронно (как и при ``RUN_IN_BACKGROUND = False``).
    """
    started = time.monotonic()
    installed = install_batch_agent(project_root)
    if installed is not None:
        _progress(f"Установлен субагент пакетов {installed} (Qwen Code увидит его со следующего сеанса).")
    if install_commands(project_root):
        _progress("Установлены команды /alla:* (Qwen Code увидит их со следующего сеанса).")
    _install_extension_agents(project_root)
    if RUN_IN_BACKGROUND and not in_job:
        repeat = repeat or ws.skill_command("prepare", str(launch_id))
        slot = jobs.job_slot(reports_dir, f"prepare-{launch_id}")
        with jobs.start_lock(slot):
            files = slot.current()
            # Идущая выгрузка этого прогона (или законченная, чей итог ещё не показан) — её
            # итог, даже с --fresh: повтор после running не должен начинать её заново.
            if files is None or not jobs.job_state(files).active:
                if not fresh:
                    unfinished = ws.find_unfinished_run(reports_dir, launch_id)
                    if unfinished is not None:
                        return _resume(unfinished, launch_id, reports_dir)
                if isinstance(_prepare_settings(), int):
                    return 2
                files = jobs.start(slot, ["prepare", str(launch_id), "--fresh",
                                          "--project-root", str(project_root),
                                          "--reports-dir", str(reports_dir)])
        return _finish_job(files, started,
                           lambda state: _prepare_running(launch_id, state, str(repeat)),
                           str(repeat))

    if not fresh:
        unfinished = ws.find_unfinished_run(reports_dir, launch_id)
        if unfinished is not None:
            return _resume(unfinished, launch_id, reports_dir)

    env_file = ws.SKILL_DIR / ".env"
    settings = _prepare_settings()
    if isinstance(settings, int):
        return settings

    # Кластеризация тянет numpy/scipy/sklearn (~0,5 с) — грузим только здесь, а не в каждом next.
    from alla_skill_lib.pipeline import collect_launch

    # Скриншоты скачиваются до папки разбора (её ещё нет) во временную папку; в папку
    # разбора переносятся только те, что попали в задания.
    with tempfile.TemporaryDirectory(prefix="alla-images-") as staging:
        try:
            data = asyncio.run(collect_launch(launch_id, settings, progress=_progress,
                                              images_dir=Path(staging)))
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
    _note_session(paths)

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


def _prepare_settings() -> Settings | int:
    """Настройки TestOps; неверные — ответ ``error`` напечатан, код возврата."""
    env_file = ws.SKILL_DIR / ".env"
    try:
        return Settings.load(env_file=env_file)
    except ConfigurationError as exc:
        print("STATUS: error")
        print(f"Ошибка конфигурации: {exc}")
        print(
            f"Пользователю нужно заполнить {env_file} по образцу .env.example или задать "
            "переменные окружения ALLURE_ENDPOINT и ALLURE_TOKEN. " + USER_ACTION_NOTE
        )
        return 2


def _prepare_running(launch_id: int, state: jobs.JobState, repeat: str) -> str:
    """Ответ ``running``: команда — последней строкой, без скобок вокруг (её копируют)."""
    stage = (state.progress or "подключаюсь к TestOps").rstrip(".…")
    return "\n".join([
        f"Прогон #{launch_id} большой: выгрузка из TestOps идёт в фоне уже {jobs.minutes(state)} "
        f"мин (сейчас: {stage}).",
        "Повторяй команду ниже, пока первая строка ответа — STATUS: running, сколько бы раз ни "
        "понадобилось: текст ответа меняется, но это ещё не итог. Она ждёт итога до "
        f"{int(jobs.WAIT_BUDGET)} с; --fresh не добавляй — повтор присоединяется к идущей "
        "выгрузке. Если ещё не говорил пользователю, скажи одной фразой, что прогон большой и "
        "данные выгружаются, и в том же ходе, ничего не спрашивая, выполни:",
        repeat,
    ])


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
        print(USER_ACTION_NOTE)
    return 1


def _resume(paths: ws.RunPaths, launch_id: int, reports_dir: Path) -> int:
    """Продолжить неоконченный разбор вместо нового: выгрузка не повторяется."""
    run = ws.read_json(paths.run_json)
    ws.remember_last_run(reports_dir, paths)
    _note_session(paths)
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
    history = load_history(paths.reports_dir)
    width = max(2, len(str(len(clusters))))
    entries: list[dict[str, Any]] = []
    local_warnings: list[str] = []

    # База знаний ведётся по модулям: кластер видит записи только своего модуля.
    resolver = ModuleResolver(project_root, index)
    modules = resolve_run_modules({
        str(position).zfill(width): resolver.cluster_module(
            _member_full_names(cluster, tests_by_id)
        )
        for position, cluster in enumerate(clusters, start=1)
    })
    kb_cache: dict[Path, list[KBRecord]] = {}
    kb_warnings: list[str] = []

    def load_kb(directory: Path) -> list[KBRecord]:
        if directory not in kb_cache:
            kb_cache[directory], warnings = ProjectKB(
                directory, kb_label(project_root, directory)
            ).load()
            kb_warnings.extend(warnings)
        return kb_cache[directory]

    for position, cluster in enumerate(clusters, start=1):
        file_id = str(position).zfill(width)
        module = modules[file_id]
        cluster_kb_dir = kb_dir_for(project_root, module)
        log_snippet, full_trace = select_log_and_trace(cluster, tests_by_id)
        auto = not has_evidence(cluster, log_snippet)
        label = cluster.label
        representative = tests_by_id.get(cluster.representative_test_id or -1)
        if auto and representative is not None:
            # Ядро подписывает такие кластеры «Тест: <id>» — имя теста понятнее.
            label = f"{representative.name} — нет данных об ошибке"
        entry: dict[str, Any] = {
            "file_id": file_id,
            "cluster_id": cluster.cluster_id,
            "label": label,
            "member_count": cluster.member_count,
            "auto": auto,
            "module": module,
            "kb_dir": str(cluster_kb_dir),
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
            kb_matches = match_cluster(load_kb(cluster_kb_dir), signature, evidence,
                                       cluster_tests(cluster, tests_by_id))
            entry.update({
                "signature": signature,
                # Какой лог видел бы инженер: remember сохраняет отпечаток в запись, разбор без
                # модели требует того же (known_issues.auto_resolution).
                "log_digest": log_errors_digest(cluster_log_errors(cluster, tests_by_id)),
                "fingerprint": default_fingerprint(message, trace, representative_log),
                "kb": kb_matches,
                "history": recurrence(
                    history,
                    launch_id=triage.launch_id,
                    signature=signature,
                    kb_ids={match["id"] for match in kb_matches},
                    module=module,
                ),
            })

            full_names = list(islice(_member_full_names(cluster, tests_by_id), 5))
            frames = project_frames(full_trace)
            staged = {test_id: data.images[test_id] for test_id in cluster.member_test_ids
                      if test_id in data.images}
            placed = place_images(staged, paths.images(file_id))
            hints = hints_for_cluster(index, [name for name in full_names if name], frames)
            task_images = {test_id: TaskImage(
                test_result_id=test_id,
                path=placed[test_id],
                name=image.name,
                step=image.step,
                attachment_id=image.attachment_id,
            ) for test_id, image in staged.items() if test_id in placed}

            task_args: dict[str, Any] = {
                "cluster": cluster,
                "position": position,
                "total": len(clusters),
                "launch_id": triage.launch_id,
                "answer_path": str(paths.analysis(file_id)),
                "next_command": paths.next_command(),
                "tests_by_id": tests_by_id,
                "log_snippet": log_snippet,
                "full_trace": full_trace,
                "frames": frames,
                "hints": hints,
                "settings": settings,
                "recurrence": entry["history"],
                "images": task_images,
            }
            task = build_cluster_task_with_sources(**task_args, kb_matches=kb_matches)
            sources = registry(
                task.sources, tests_by_id,
                files={source_id: image.path.relative_to(paths.root).as_posix()
                       for source_id, image in task.images.items()},
                steps={source_id: image.step for source_id, image in task.images.items()},
                attachment_ids={source_id: image.attachment_id
                                for source_id, image in task.images.items()},
            )
            write_registry(paths.sources(file_id), sources)
            # Чьи данные видела модель; при нескольких блоках нужна «СОГЛАСОВАННОСТЬ». Куски
            # данных и примеры от записей базы знаний в задании не зависят.
            entry["examples"] = task.examples
            entry["example_blocks"] = task.blocks
            entry["log_not_found"] = task.log_not_found
            known_auto = auto_resolution(kb_matches, load_kb(cluster_kb_dir), cluster,
                                         tests_by_id, sources, entry["log_digest"])
            if known_auto is not None and known_auto_valid(load_kb(cluster_kb_dir), known_auto,
                                                           entry, project_root, paths):
                # Разбор пишет скрипт (next). Задание — запасной путь, если инженер отвергнет
                # запись: тогда оно не должно называть её точным совпадением.
                entry["kb_auto"] = known_auto
                task = build_cluster_task_with_sources(
                    **task_args,
                    kb_matches=[match for match in kb_matches if match["id"] != known_auto["id"]],
                )
            shown_paths = {image.path for image in task.images.values()}
            for path in placed.values():  # скачанный, но не показанный — не нужен
                if path not in shown_paths:
                    path.unlink(missing_ok=True)
            if placed and not shown_paths:
                shutil.rmtree(paths.images(file_id), ignore_errors=True)
            ws.write_text(paths.cluster_task(file_id), task.text)
            if task.images:
                entry["images"] = [{
                    "source_id": source_id,
                    "file": image.path.relative_to(paths.root).as_posix(),
                    "test_result_id": image.test_result_id,
                    "name": image.name,
                    "step": image.step,
                } for source_id, image in task.images.items()]
        except Exception as exc:  # один битый кластер не должен ронять весь prepare
            logger.exception("Не удалось подготовить кластер %s", file_id)
            local_warnings.append(
                f"Проблема {position}: задание не подготовлено ({type(exc).__name__}: {exc}) — "
                "причина не определена."
            )
            entry.update(
                auto=True, signature=None, fingerprint="", kb=[], history=None
            )
            entry.pop("kb_auto", None)
            entry.pop("log_digest", None)
            paths.evidence(file_id).unlink(missing_ok=True)
            paths.sources(file_id).unlink(missing_ok=True)
            paths.cluster_task(file_id).unlink(missing_ok=True)
            shutil.rmtree(paths.images(file_id), ignore_errors=True)
            entry.pop("images", None)
            ws.write_text(paths.analysis(file_id), failed_prepare_analysis(type(exc).__name__))

    run = {
        "schema": ws.RUN_SCHEMA,
        "launch_id": triage.launch_id,
        "launch_name": triage.launch_name,
        "launch_url": f"{settings.endpoint.rstrip('/')}/launch/{triage.launch_id}",
        "launch_created": _launch_created(triage.launch_created_ms),
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


def _launch_created(created_ms: int | None) -> str | None:
    """Время запуска прогона из TestOps (миллисекунды эпохи) — ISO 8601 в UTC."""
    if created_ms is None:
        return None
    return datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _member_full_names(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
) -> Iterator[str]:
    """``full_name`` участников кластера, представитель первым (пустые — как есть)."""
    member_ids = sorted(
        cluster.member_test_ids,
        key=lambda test_id: test_id != cluster.representative_test_id,
    )
    return (tests_by_id[test_id].full_name or "" for test_id in member_ids if test_id in tests_by_id)


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def cmd_review(
    run_dir: str | None,
    reports_dir: Path,
    *,
    use_model: bool = True,
    in_job: bool = False,
) -> int:
    """Проверка законченного разбора. Сеанс не запоминается: проверка — не часть разбора.

    Оценка моделью идёт минуты — фоновым заданием, как выгрузка в ``prepare``.
    """
    from alla_skill_lib import review

    started = time.monotonic()
    try:
        paths = ws.resolve_run(run_dir, reports_dir)
    except ws.RunNotFoundError as exc:
        print("STATUS: error")
        print(exc)
        return 1
    if use_model and RUN_IN_BACKGROUND and not in_job:
        # Повтор — с этой папкой: review без неё взял бы .last_run, а его может сменить prepare.
        repeat = ws.skill_command("review", "--run", str(paths.root))
        # Задание — в папке отчётов самого разбора: --reports-dir мог быть и не указан.
        slot = jobs.job_slot(paths.reports_dir, f"review-{paths.root.name}")
        with jobs.start_lock(slot):
            files = slot.current()
            if files is None or not jobs.job_state(files).active:
                files = jobs.start(slot, ["review", "--run", str(paths.root),
                                          "--reports-dir", str(paths.reports_dir)])
        return _finish_job(files, started, lambda state: _review_running(state, repeat), repeat)
    facts = review.collect_facts(paths)
    if use_model:
        _progress("Оценка диагнозов моделью с чистым контекстом — до нескольких минут…")
    facts["assessment"] = review.assess(paths, facts) if use_model else None
    facts["grades"] = review.grades(facts)
    report, summary = review.write_review(paths, facts)
    run = facts["run"]
    name = f" «{run['launch_name']}»" if run["launch_name"] else ""
    print("STATUS: reviewed")
    print(f"Прогон #{run['launch_id']}{name} · папка {paths.root}")
    print("===ОТЧЁТ===")
    print(review.render_console(facts, report, summary))
    print("===КОНЕЦ===")
    print("Выведи пользователю текст между ===ОТЧЁТ=== и ===КОНЕЦ=== дословно; разбор заново не начинай.")
    return 0


def _review_running(state: jobs.JobState, repeat: str) -> str:
    stage = (state.progress or "собираю факты разбора").rstrip(".…")
    return "\n".join([
        f"Проверка разбора идёт в фоне уже {jobs.minutes(state)} мин (сейчас: {stage}).",
        "Повторяй команду ниже, пока первая строка ответа — STATUS: running, сколько бы раз ни "
        "понадобилось: текст ответа меняется, но это ещё не итог. Она ждёт итога до "
        f"{int(jobs.WAIT_BUDGET)} с. Пользователю пока ничего не пиши и ничего не спрашивай — "
        "в том же ходе выполни:",
        repeat,
    ])


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
        # явный запрос числа субагентов: выданные раньше пакеты можно раздать заново
        state.pop("batched", None)
        state.pop("wave", None)
        ws.write_json(paths.state_json, state)
    _note_session(paths)
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
    """Определить следующий шаг по состоянию файлов.

    Всё берётся с диска, кроме учёта пакетов, уже выданных субагентам
    (``state.json``): кластер раздаётся пакетом не больше одного раза, поэтому
    неудачная волна не повторяется бесконечно.
    """
    notices: list[str] = []
    status, body = _next_step(paths, notices)
    if status == "done":
        body = _with_report_hooks(paths, body)
    return status, "\n".join([*notices, body]) if notices else body


def _next_step(paths: ws.RunPaths, notices: list[str]) -> tuple[str, str]:
    run = ws.read_json(paths.run_json)
    if not run["clusters"]:
        console, full = render_green_report(run, paths)
        ws.write_text(paths.report, full)
        return "done", _done_body(console, paths, {})

    project_root = Path(run["project_root"])
    state = _read_state(paths)
    entries = run["clusters"]
    total = len(entries)
    # Точно узнанные проблемы разбирает скрипт по текущим записям базы знаний — до подсчёта
    # неразобранных: после reject такая проблема становится обычной и ждёт разбора модели.
    before = state.get(KB_AUTO_STATE)
    known_auto, auto_notes = sync_known_auto(paths, run, state)
    if state.get(KB_AUTO_STATE) != before:
        ws.write_json(paths.state_json, state)
    manual_total = sum(1 for item in entries
                       if not item["auto"] and item["file_id"] not in known_auto)
    pending = [
        item["file_id"] for item in entries
        if not item["auto"] and not _has_text(paths.analysis(item["file_id"]))
    ]
    workers = _settle_last_wave(paths, state, pending, notices)
    handed_out = set(state.get("batched", []))
    fresh = [file_id for file_id in pending if file_id not in handed_out]
    if workers > 1 and len(fresh) >= PARALLEL_MIN_PENDING:
        batches = plan_batches(fresh, BATCH_SIZE, workers)
        wave = [file_id for batch in batches for file_id in batch]
        state["batched"] = sorted(handed_out | set(wave))
        state["wave"] = wave
        ws.write_json(paths.state_json, state)
        return "analyze_batch", _batch_body(paths, run, batches, len(fresh), manual_total)
    # Общее состояние (известные проблемы, связи) — только после проверки всех разборов:
    # посреди разбора next его не собирает.
    analyses: dict[str, ClusterAnalysis] = {}
    flagged: set[str] = set()
    proposals: dict[str, Proposal] = {}
    not_proposed: dict[str, str] = {}
    notes: list[str] = list(auto_notes)
    candidates = 0

    for position, entry in enumerate(entries, start=1):
        file_id = entry["file_id"]
        analysis_path = paths.analysis(file_id)
        text = ws.read_text(analysis_path) if analysis_path.is_file() else ""
        if not text.strip():
            done = sum(
                1 for item in entries
                if not item["auto"] and item["file_id"] not in known_auto
                and _has_text(paths.analysis(item["file_id"]))
            )
            return "analyze", _analyze_body(paths, entry, position, total, done, manual_total,
                                            len(known_auto))

        analysis, errors = _check_analysis(text, entry, project_root, paths)
        if errors:
            attempt, unchanged = _register_invalid(state, file_id, text, paths)
            if attempt < MAX_FIX_ATTEMPTS:
                return "fix", _fix_body(
                    paths, entry, position, total, errors, attempt, analysis, unchanged
                )
            flagged.add(file_id)
        analyses[file_id] = analysis

        if file_id in known_auto:
            if analysis.category == "тест":
                # Рецепт известной проблемы уже в записи: правку моделью не просим.
                not_proposed[file_id] = KB_AUTO_NOT_PROPOSED
            continue
        if (
            file_id not in flagged
            and not entry["auto"]
            and analysis.category == "тест"
            and analysis.code
        ):
            if analysis.consistency_kind == "different":
                # Одна общая правка для неоднородной группы не предлагается.
                not_proposed[file_id] = MIXED_GROUP_NOT_PROPOSED
                continue
            if _withdrawn_fix(paths, run, entry, analysis, project_root):
                # Разбор опирался на отвергнутую запись: правку не просим и не предлагаем,
                # пока причина не подтверждена. Уже применённая остаётся — её можно откатить.
                not_proposed[file_id] = REJECTED_NOT_PROPOSED
                continue
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

    lost = [file_id for file_id, analysis in analyses.items()
            if analysis.sources_lost and analysis.observations]
    if lost:
        notes.append(
            "Цитаты в разборе проблем " + ", ".join(str(int(file_id)) for file_id in lost)
            + " не сверены с данными: файл источников задания не найден или повреждён."
        )
    skipped = [file_id for file_id in state.get("skipped", []) if file_id in analyses]
    if skipped:
        notes.append(
            "Пропущено без разбора по просьбе пользователя: "
            + ", ".join(str(int(file_id)) for file_id in skipped)
            + "."
        )

    current = current_state(paths, run=run, state=state, analyses=analyses, flagged=flagged,
                            known_auto=known_auto)
    known = current.known
    notes += known.notes
    confirmed = current.confirmed
    related_outcome = _related_step(paths, state, current)
    if isinstance(related_outcome, tuple):
        return related_outcome
    related = related_outcome or Related()
    if related_outcome is None:
        notes.append(RELATED_DROPPED_NOTE)
    fixes = {file_id: p for file_id, p in proposals.items() if p.is_fix}
    states = {
        file_id: applied_state(p, project_root, _proposal_files(paths, file_id))
        for file_id, p in fixes.items()
    }
    # Проблемы с правкой агента в отчёте не объединяются: у каждой своя команда apply.
    separate = {file_id for file_id, state in states.items() if state != "unknown"}
    summary_data = build_summary_data(run, analyses, flagged, known, related, separate, confirmed)
    # Разобранные по базе знаний — набором: в данных хвоста сводки они только числом, а сводку
    # прогона, где все проблемы известны, пишет скрипт — по этому набору (ревью Codex).
    summary_hash = hashlib.sha256(
        "\n".join([summary_data, *sorted(known.auto)]).encode("utf-8")).hexdigest()
    previous_hash = state.get("summary_data_hash")
    summary = ws.read_text(paths.summary) if paths.summary.is_file() else ""
    if previous_hash != summary_hash:  # сводка не к этим данным (или без отметки) — заново
        paths.summary.unlink(missing_ok=True)
        summary = ""
    if known.auto and all(entry["auto"] or entry["file_id"] in known.auto for entry in entries):
        # Все проблемы с данными — известные: причины назвал инженер, сводку пишет скрипт — из
        # текущих записей и при каждом next: рецепт записи вне первых групп в данные сводки не
        # входит, и правку его первого шага хэш бы не заметил (ревью Codex).
        script = known_summary(run, known)
        if summary != script:
            ws.write_text(paths.summary, script)
            summary = script
        paths.summary_task.unlink(missing_ok=True)
        if previous_hash != summary_hash:
            state["summary_data_hash"] = summary_hash
            ws.write_json(paths.state_json, state)
    if not summary.strip():
        ws.write_text(
            paths.summary_task,
            build_summary_task(run, analyses, flagged, paths, data=summary_data, known=known),
        )
        state["summary_data_hash"] = summary_hash
        ws.write_json(paths.state_json, state)
        return "summary", _summary_body(paths, total)

    known_refs = {file_id: ref.id for file_id, ref in known.refs.items()}
    append_changed_run(
        paths.reports_dir, run_records(run, analyses, flagged, paths.root.name, known_refs)
    )

    console, full = render_report(
        run, analyses, flagged, summary, paths, proposals, states, notes, not_proposed, known,
        related, confirmed, rerun.RerunView.of(rerun.job_state(rerun.job_files(paths))),
        flaky.marked_by_problem(run, flaky.marks_for_run(run)),
    )
    ws.write_text(paths.report, full)
    # Уже применённую или неоднозначную правку повторно не предлагаем.
    offered = {file_id: p for file_id, p in fixes.items() if states[file_id] == "not_applied"}
    offer = _rerun_offer(paths, run, analyses, flagged, set(confirmed), states)
    kb_offer = _kb_offer(paths, run, analyses, flagged, known, set(confirmed), related, separate)
    return "done", _done_body(console, paths, offered, feedback=True, rerun_offer=offer,
                              kb_offer=kb_offer)


REJECTED_NOT_PROPOSED = (
    "разбор опирался на запись базы знаний, которую пользователь отверг для этой проблемы, — "
    "правка снята, пока причина не подтверждена"
)
MIXED_GROUP_NOT_PROPOSED = (
    "в группе, похоже, несколько проблем — одна правка на все тесты не предлагается"
)


def _note_session(paths: ws.RunPaths) -> None:
    """Запомнить сеанс Qwen Code, из которого идёт разбор: по нему ``review`` найдёт журнал."""
    state = _read_state(paths)
    if note_session(state):
        ws.write_json(paths.state_json, state)


def _read_state(paths: ws.RunPaths) -> dict[str, Any]:
    loaded: Any = {}
    if paths.state_json.is_file():
        try:
            loaded = ws.read_json(paths.state_json)
        except (OSError, ValueError):  # пустой или оборванный файл после сбоя записи
            loaded = None
        if not isinstance(loaded, dict):
            logger.warning("%s пуст или повреждён — счётчики попыток начаты заново", paths.state_json)
            loaded = {}
    state: dict[str, Any] = loaded
    state.setdefault("attempts", {})
    return state


def _check_analysis(
    text: str,
    entry: dict[str, Any],
    project_root: Path,
    paths: ws.RunPaths,
) -> tuple[ClusterAnalysis, list[str]]:
    """Разобрать текст разбора кластера и проверить его; пустой список ошибок — принят."""
    return check_entry_analysis(text, entry, project_root, paths)


def _settle_last_wave(
    paths: ws.RunPaths,
    state: dict[str, Any],
    pending: list[str],
    notices: list[str],
) -> int:
    """Учесть итог прошлой волны и вернуть число субагентов для следующей.

    Кластеры прошлой волны, которые остались без разбора, повторно пакетами не
    выдаются: их разберёт обычный ``analyze`` по одному. Если не записан ни один
    разбор волны, субагенты, скорее всего, не работают (не запускались, нет прав
    на запись) — пакетный режим выключается до явного ``next --workers N``.
    """
    workers = int(state.get("workers", DEFAULT_WORKERS))
    wave = state.pop("wave", [])
    if not wave:
        return workers
    missing = [file_id for file_id in wave if file_id in set(pending)]
    if missing:
        listed = ", ".join(missing)
        if len(missing) == len(wave):
            workers = state["workers"] = 1
            notices.append(
                f"Прошлая волна не записала ни одного разбора (кластеры {listed}). Субагентов "
                "больше не запускай: эти кластеры пойдут по одному. Если субагенты просто не "
                "успели стартовать, `next --workers N` выдаст пакеты заново."
            )
        else:
            notices.append(
                f"Субагенты прошлой волны не записали разборы кластеров {listed}: повторно "
                "пакетами они не выдаются, эти кластеры пойдут по одному."
            )
    ws.write_json(paths.state_json, state)
    return workers


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
    if not errors:
        return proposal
    files = _proposal_files(paths, file_id)
    # Новые ограничения не мешают старой записи apply, но найденное СТАЛО
    # без совпадающей записи не разрешает правку сборки или конфигурации.
    previous_state = (
        recorded_applied_state(proposal, project_root, files)
        if proposal_eligibility_errors(proposal, project_root)
        else applied_state(proposal, project_root, files)
    )
    if previous_state in ("applied", "unknown"):
        return proposal
    attempt, unchanged = _register_invalid(state, f"proposal-{file_id}", text, paths)
    if attempt < MAX_FIX_ATTEMPTS:
        return "fix", "\n".join([
            f"{_cluster_caption(entry, position, total)} — предложение правки не прошло "
            f"проверку (попытка {attempt} из {MAX_FIX_ATTEMPTS}):",
            *(f"- {error}" for error in errors),
            *([UNCHANGED_NOTE] if unchanged >= 2 else []),
            *([LAST_ATTEMPT_PROPOSAL] if attempt == MAX_FIX_ATTEMPTS - 1 else []),
            f"Исправь файл: {path}",
            "Если уверенности нет — запиши «РЕШЕНИЕ: не трогать» и «ПОЧЕМУ: …».",
            "Формат:",
            PROPOSAL_FORMAT,
            reference_line(PROPOSAL_FORMAT_REF),
            proposal_mismatch_note(paths),
            f"Затем выполни: {paths.next_command()}",
        ])
    return None


def _related_step(
    paths: ws.RunPaths,
    state: dict[str, Any],
    current: CurrentState,
) -> tuple[str, str] | Related | None:
    """Шаг «похожие проблемы»: (статус, текст), подтверждённые группы или None (отброшено).

    Нет кандидатов — шага нет. Решение модели действует, пока не изменились данные
    групп (``state.related_data_hash``): иначе ``related.md`` удаляется и просится заново.
    """
    run, analyses, current_kb = current.run, current.analyses, current.current_matches
    # Подтверждённые инженером проблемы не сравниваются: их причина известна (до задания —
    # иначе после remember --from-extension next снова просил бы сравнить и их).
    groups = current.candidates
    if not groups:
        paths.related.unlink(missing_ok=True)
        # Все поля — независимо: иначе снимок восстановил бы исчезнувшие группы.
        stale = [state.pop(key, None) for key in ("related_data_hash", "related", "related_scopes")]
        if any(value is not None for value in stale):
            ws.write_json(paths.state_json, state)
        return Related()
    data = relate_data(run, groups, analyses, current_kb)
    digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
    if state.get("related_data_hash") != digest:
        paths.related.unlink(missing_ok=True)
        state["attempts"].pop("related", None)
        state["related_data_hash"] = digest
        state["related"] = candidate_stats(groups)
        ws.write_json(paths.state_json, state)
    text = ws.read_text(paths.related) if paths.related.is_file() else ""
    if not text.strip():
        ws.write_text(paths.relate_task, build_relate_task(run, data, paths))
        return "relate", _relate_body(paths, len(groups))
    decision = parse_related(text)
    errors = validate_related(decision, groups)
    if errors:
        attempt, unchanged = _register_invalid(state, "related", text, paths)
        if attempt >= MAX_FIX_ATTEMPTS:
            return None
        return "fix", "\n".join([
            f"Решение по похожим проблемам не прошло проверку (попытка {attempt} из "
            f"{MAX_FIX_ATTEMPTS}):",
            *(f"- {error}" for error in errors),
            *([UNCHANGED_NOTE] if unchanged >= 2 else []),
            *([LAST_ATTEMPT_RELATED] if attempt == MAX_FIX_ATTEMPTS - 1 else []),
            f"Задание: {paths.relate_task}",
            f"Исправь файл: {paths.related}",
            "Формат:",
            RELATED_FORMAT,
            reference_line(RELATED_FORMAT_REF),
            related_mismatch_note(paths),
            f"Затем выполни: {paths.next_command()}",
        ])
    related = related_groups(decision, run)
    scopes = [{"numbers": [int(n) for n in group.file_ids], "cause": group.cause,
               "fingerprint": relation_scope(analyses, group.file_ids, current_kb)}
              for group in related.groups]
    if state.get("related_scopes") != scopes:
        state["related_scopes"] = scopes
        ws.write_json(paths.state_json, state)
    decided = {"confirmed_groups": len(related.groups),
               "confirmed_problems": sum(len(group.file_ids) for group in related.groups)}
    stats = state.get("related") or {}
    if any(stats.get(key) != value for key, value in decided.items()):
        state["related"] = {**stats, **decided}
        ws.write_json(paths.state_json, state)
    return current.related


def _relate_body(paths: ws.RunPaths, groups: int) -> str:
    return "\n".join([
        f"Все кластеры разобраны. Среди них есть похожие: групп для сравнения — {groups}. "
        "Реши, у каких проблем одна причина.",
        f"1. Прочитай задание: {paths.relate_task}",
        f"2. Запиши решение в файл: {paths.related}",
        reference_line(RELATED_FORMAT_REF, "   Формат с примером"),
        f"3. Выполни: {paths.next_command()}",
    ])


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
    known_auto: int = 0,
) -> str:
    no_data = total - manual_total - known_auto
    progress = f"Готово разборов: {done} из {manual_total}"
    automatic = [
        *([f"{no_data} без данных об ошибке"] if no_data else []),
        *([f"{known_auto} по базе знаний"] if known_auto else []),
    ]
    if automatic:
        progress += f" (ещё {' и '.join(automatic)} разобраны автоматически)"
    return "\n".join([
        f"{_cluster_caption(entry, position, total)}. {progress}.",
        f"1. Прочитай задание: {paths.cluster_task(entry['file_id'])}",
        f"2. Запиши разбор в файл: {paths.analysis(entry['file_id'])}",
        reference_line(ANALYSIS_FORMAT_REF, "   Формат с примерами (прочитай один раз)"),
        f"3. Выполни: {paths.next_command()}",
    ])


def _batch_body(
    paths: ws.RunPaths,
    run: dict[str, Any],
    batches: list[list[str]],
    fresh_count: int,
    manual_total: int,
) -> str:
    by_id = {entry["file_id"]: entry for entry in run["clusters"]}
    paths.batch(1).parent.mkdir(parents=True, exist_ok=True)
    taken = sum(len(ids) for ids in batches)
    lines = [
        f"Кластеров без разбора много: {fresh_count} из {manual_total}. Разбери их параллельно, "
        "раздав пакеты субагентам; сам эти кластеры не разбирай и скрипты-обёртки не пиши.",
        f"В этой волне: пакетов — {len(batches)}, кластеров — {taken}"
        + (f"; остальные ({fresh_count - taken}) — в следующей." if taken < fresh_count else "."),
        "1. Запусти субагентов инструментом agent — по одному на пакет, ВСЕ вызовы в одном "
        f"сообщении, с subagent_type: {BATCH_AGENT} и run_in_background: false (agent ответил, "
        "что такого типа нет, — те же вызовы без subagent_type). prompt каждого — дословно:",
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
        "2. Дождись всех субагентов. Их ответы не пересказывай и не перепроверяй: проверит next; "
        f"строки «{PROBLEM_PREFIX} …» никуда не переноси. "
        "Пакет повторно не выдаётся: что не записано, next даст по одному.",
        f"3. Выполни: {paths.next_command()}",
        "Нет инструмента agent или субагенты не запустились — не пиши свои циклы, а переключись на "
        f"разбор по одному: {ws.skill_command('next', str(paths.root), '--serial')}",
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
        parse_summary(analysis, observed(entry), example_blocks(entry)),
        *([UNCHANGED_NOTE] if unchanged >= 2 else []),
        *([LAST_ATTEMPT_ANALYSIS] if attempt == MAX_FIX_ATTEMPTS - 1 else []),
        f"Исправь файл: {paths.analysis(entry['file_id'])}",
        f"Задание кластера: {paths.cluster_task(entry['file_id'])}",
        "Ожидаемый формат:",
        EXPECTED_FORMAT,
        reference_line(ANALYSIS_FORMAT_REF),
        format_mismatch_note(paths),
        f"Затем выполни: {paths.next_command()}",
    ])


def _propose_body(paths: ws.RunPaths, entry: dict[str, Any], position: int, total: int) -> str:
    return "\n".join([
        f"{_cluster_caption(entry, position, total)}: по разбору виноват автотест. "
        "Реши, можно ли исправить его код.",
        PROPOSAL_RULES,
        f"1. Посмотри код из строки «КОД:» разбора (не больше {MAX_PROJECT_FILES} файлов, "
        f"только чтение): {paths.analysis(entry['file_id'])}",
        f"2. Запиши решение в файл: {paths.proposal(entry['file_id'])}",
        "Формат:",
        PROPOSAL_FORMAT,
        "Для «не трогать» достаточно РЕШЕНИЕ и ПОЧЕМУ.",
        reference_line(PROPOSAL_FORMAT_REF),
        f"3. Выполни: {paths.next_command()}",
    ])


def _summary_body(paths: ws.RunPaths, total: int) -> str:
    return "\n".join([
        f"Все кластеры разобраны ({total}). Осталось написать общий анализ прогона.",
        f"1. Прочитай задание: {paths.summary_task}",
        f"2. Запиши общий анализ в файл: {paths.summary}",
        reference_line(SUMMARY_FORMAT_REF, "   Формат итога с примером"),
        f"3. Выполни: {paths.next_command()}",
    ])


def _kb_question(paths: ws.RunPaths, single: list[str], groups: list[list[str]]) -> list[str]:
    """Пункт «После отчёта»: один вопрос, какие разборы верны, и как сохранить ответ.

    Первым из вопросов после отчёта: за цепочкой «правка → проверка перезапуском» он терялся
    (ревью исполнителя). Готовая команда — с местом для номеров, а не со всеми: «бери команды
    как есть» иначе сохранило бы и разбор, который пользователь назвал неверным.
    """
    if not single and not groups:
        return []
    run = str(paths.root)
    numbers = [str(int(file_id)) for file_id in single]
    merged = ["«Проблема " + " и ".join(str(int(file_id)) for file_id in group) + "»"
              for group in groups]
    listed = ", ".join(numbers) + (" и " if numbers and merged else "") + ", ".join(merged)
    lines = [
        "- Сохранить разборы в базу знаний: сразу после краткого разбора, до вопросов о правках "
        "и перезапуске, задай пользователю отдельно один вопрос, дословно: «Какие разборы верны? "
        "Сохраню их в базу знаний — в следующих прогонах эти ошибки разберутся сразу, без "
        f"агента. Проблемы: {listed}. Если где-то причина другая — назовите её». О номерах, о "
        "которых уже спрашивал в этом сеансе, повторно не спрашивай.",
    ]
    if numbers:
        lines.append(
            "  Верны все или названные номера — одна команда: "
            + ws.skill_command("remember", "N1", "N2", "--run", run, "--from-analysis")
            + ", где N1 N2 … — только номера, которые пользователь назвал верными, сколько их "
            "ни есть (номер, который он исключил или назвал неверным, не пиши); «верны все» — "
            f"{' '.join(numbers)}."
        )
    if merged:
        lines.append(
            f"  Верен пункт {merged[0]} (объединил агент) — не --from-analysis: каждому его номеру "
            "свой файл обратной связи с общей причиной и шагом «Что делать» этого пункта, затем "
            "remember N (references/feedback.md, «Порядок»)."
        )
    lines.append(
        "  Причина другая у проблемы N — файл обратной связи с его слов и remember N (ниже); "
        "назвал номер неверным без причины — спроси причину. «Не сохраняй», «потом» — ничего не "
        "выполняй."
    )
    return lines


def _kb_offer(
    paths: ws.RunPaths,
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    known: KnownIssues,
    confirmed: set[str],
    related: Related,
    separate: set[str],
) -> tuple[list[str], list[list[str]]]:
    """Разборы для вопроса после отчёта «какие верны — сохраню в базу знаний».

    Принятые разборы агента с категорией, которые ни одна запись ещё не узнаёт точно (уже
    сохранённые второй раз не спрашиваем); без неоднородной группы (одна причина на смешанные
    тесты), разбора по отвергнутой записи или с другой категорией, чем у его записи, и без
    подтверждённого вывода расширения. Узнанную точно, но с логом, которого запись не видела
    (``confirmed_logs``), спрашиваем, если разбор назвал эту запись: подтверждение добавит
    отпечаток, и в следующий раз проблема разберётся без модели. Возвращает отдельные номера и
    группы «одна причина» (объединил агент): их пользователь видел общей причиной, сохраняются
    они по ``feedback.md``. Проблемы с правкой агента (``separate``) отчёт не объединяет — и
    вопрос называет их отдельно (ревью Codex).
    """
    current = current_kb_matches(run, paths)
    eligible: list[str] = []
    for entry in run["clusters"]:
        file_id = entry["file_id"]
        analysis = analyses.get(file_id)
        if (analysis is None or file_id in flagged or entry["auto"] or file_id in known.auto
                or analysis.category in (None, "неизвестно")
                or analysis.consistency_kind == "different"
                or file_id in known.rejected or file_id in known.mismatched
                or file_id in confirmed):
            continue
        exact = [match for match in current.get(file_id, []) if match["origin"] == "exact"]
        if exact and not _new_log_of(exact, analysis, entry):
            continue
        eligible.append(file_id)
    groups = [[file_id for file_id in group.file_ids
               if file_id in eligible and file_id not in separate]
              for group in related.groups]
    groups = [group for group in groups if len(group) > 1]
    grouped = {file_id for group in groups for file_id in group}
    return [file_id for file_id in eligible if file_id not in grouped], groups


def _new_log_of(exact: list[dict[str, Any]], analysis: ClusterAnalysis, entry: dict[str, Any]) -> bool:
    """Разбор назвал точную запись, а она этот лог проблемы (отпечаток) ещё не видела."""
    match = next((match for match in exact if match["id"] == analysis.kb_ref), None)
    if match is None:
        return False
    try:
        record = ProjectKB(Path(match["kb_dir"])).get(match["id"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return record is not None and entry.get("log_digest") not in record.confirmed_logs


def _done_body(
    console: str,
    paths: ws.RunPaths,
    fixes: dict[str, Proposal],
    feedback: bool = False,
    rerun_offer: str | None = None,
    kb_offer: tuple[list[str], list[list[str]]] = ([], []),
) -> str:
    lines = [
        f"Отчёт сохранён: {paths.report}",
        "Неполадки скилла (SKILL.md, «Проблемы скилла») пользователю не выводи и о них не "
        f"упоминай: если были и ещё не записаны — сначала запиши их в {paths.skill_problems}. "
        + reference_line(PROBLEM_REPORT_REF, "Формат файла"),
        "Выведи пользователю краткий разбор между маркерами дословно, без пересказа и "
        "собственных выводов; значки, пустые строки и отступы сохрани. В конце него — ссылка на файл с полным разбором, оставь её как "
        "есть; подробности в чат не переписывай:",
        REPORT_BEGIN,
        console,
        REPORT_END,
    ]
    if fixes or feedback or rerun_offer:
        lines.append("После отчёта:")
    if feedback:
        lines += _kb_question(paths, *kb_offer)
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
            reference_line(FEEDBACK_FORMAT_REF, "  Правила и пример"),
            "  Команды для ЭТОГО разбора (всегда с этим --run):",
            f"  файл обратной связи: {paths.root / 'feedback'}/NN.md (NN — номер, как в именах "
            "clusters/NN.md, с ведущими нулями)",
            f"  {ws.skill_command('remember', 'N', '--run', run)}",
            f"  разбор подтверждён как есть: {ws.skill_command('remember', 'N', '--run', run, '--from-analysis')}",
            "  вывод расширения подтверждён (ответ субагента расширения по проблеме N просил "
            "подтвердить, пользователь согласился) — вместо --from-analysis: "
            f"{ws.skill_command('remember', 'N', '--run', run, '--from-extension', '<имя расширения>')}",
            f"  известная проблема не подходит: {ws.skill_command('reject', 'N', '<id>', '--run', run)}",
        ]
    if rerun_offer:
        lines.append(rerun_offer)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rerun
# ---------------------------------------------------------------------------


def cmd_rerun(args: argparse.Namespace, reports_dir: Path) -> int:
    """Перезапуск тестов локально: план, запуск после «да», ход и итог (``rerun.py``)."""
    started = time.monotonic()
    try:
        paths = ws.resolve_run(args.run, reports_dir)
    except ws.RunNotFoundError as exc:
        print("STATUS: error")
        print(exc)
        return 1
    run = ws.read_json(paths.run_json)
    status, body = _rerun(paths, run, args, started)
    print(f"STATUS: {status}")
    print(body)
    return _exit_code(status)


def _rerun(
    paths: ws.RunPaths, run: dict[str, Any], args: argparse.Namespace, started: float,
) -> tuple[str, str]:
    files = rerun.job_files(paths)
    state = rerun.job_state(files)
    if args.remember:
        return _rerun_remember(paths, run, state, args.result_id, started)
    if args.wait or (not args.clusters and not args.yes and state.state != "none"):
        # Без номеров после перезапуска — его ход или итог: «итог покажет /alla:rerun».
        if state.state == "none":
            return "ok", "Перезапуска тестов в этом разборе не было. Покажи это пользователю."
        if state.state == "running":
            state = rerun.wait_job(files, rerun.WAIT_BUDGET - (time.monotonic() - started))
        return _rerun_state(paths, run, state)
    if state.state == "running":
        status, body = _rerun_state(paths, run, state)
        return status, ("Новый перезапуск не начат: в этом разборе ещё идёт прежний. Скажи это "
                        "пользователю; повторить его просьбу можно после итога.\n" + body)
    entries = [find_entry(run, cluster) for cluster in args.clusters]
    unknown = [cluster for cluster, entry in zip(args.clusters, entries) if entry is None]
    if unknown:
        return "error", f"В разборе {paths.root} нет проблем: {', '.join(unknown)}."
    settings = _rerun_settings()
    if isinstance(settings, str):
        return "error", settings
    if settings.rerun_max_tests == 0:
        return "ok", ("Перезапуск тестов выключен в настройках скилла (ALLURE_RERUN_MAX_TESTS=0). "
                      "Покажи это пользователю.")
    numbers = [str(int(entry["file_id"])) for entry in entries if entry is not None]
    if numbers:
        picks, signals = _problem_picks(run, [entry for entry in entries if entry is not None])
    else:
        offered = _offered_picks(run, rerun.read_offer(files))
        if not offered:
            return "ok", ("Тестов, похожих на нестабильные, в этом разборе нет (или разбор ещё не "
                          "закончен). Перезапустить тесты конкретной проблемы — "
                          f"{ws.skill_command('rerun', 'N', '--run', str(paths.root))}. Покажи это "
                          "пользователю и спроси номера проблем.")
        picks = [(entry, test) for entry, test, _ in offered]
        signals = {int(test["test_result_id"]): codes for _, test, codes in offered}
    plan = rerun.build_plan(paths, run, picks, attempts=settings.rerun_attempts,
                            max_tests=settings.rerun_max_tests, signals=signals, auto=not numbers)
    if plan.note or not plan.tests:
        return "ok", rerun.render_nothing(plan)
    command = ws.skill_command("rerun", *numbers, "--run", str(paths.root), "--yes",
                               "--plan", plan.digest)
    extra = rerun.maven_args(settings.rerun_maven_args)
    shown = rerun.render_plan(plan, command, has_extra=bool(extra))
    if not args.yes:
        return "rerun", shown
    if args.plan_hash != plan.digest:
        return "rerun", ("План изменился или хэш не тот — ничего не запущено. Покажи пользователю "
                         "новый план и спроси снова.\n" + shown)
    if rerun.lock_busy(rerun.project_lock(Path(run["project_root"]))):
        return "ok", ("В проекте уже идёт перезапуск тестов другого разбора — ничего не запущено. "
                      "Скажи пользователю, что повторить можно после его итога.")
    if rerun.start_job(paths, plan, settings.rerun_timeout, extra) is None:
        status, body = _rerun_state(paths, run, rerun.job_state(files))
        return status, "Новый перезапуск не начат: в этом разборе он уже идёт.\n" + body
    state = rerun.wait_job(files, rerun.WAIT_BUDGET - (time.monotonic() - started))
    return _rerun_state(paths, run, state)


def _problem_picks(
    run: dict[str, Any], entries: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[int, list[str]]]:
    """Тесты названных проблем и их признаки (без категории: разборы команда не читает)."""
    picks = [(entry, test) for entry in entries for test in rerun.failed_tests_of(run, entry)]
    marks = flaky.marks_for_run(run)
    signals = {int(test["test_result_id"]): flaky.signals_of(
                   test, None, marked=int(test["test_result_id"]) in marks)
               for _, test in picks}
    return picks, signals


def _rerun_settings() -> Settings | str:
    """Настройки перезапуска (TestOps не нужен); неверный ``.env`` — текст ошибки."""
    env_file = ws.SKILL_DIR / ".env"
    try:
        return Settings.load(env_file=env_file, require_testops=False)
    except ConfigurationError as exc:
        return f"Настройки скилла ({env_file}) неверны: {exc}. {USER_ACTION_NOTE}"


def _offered_picks(
    run: dict[str, Any], offer: list[tuple[str, int, list[str]]],
) -> list[tuple[dict[str, Any], dict[str, Any], list[str]]]:
    """Кандидаты из ``rerun/offer.json`` — записи и тесты этого разбора."""
    picks = []
    for file_id, test_id, codes in offer:
        entry = next((item for item in run["clusters"] if item["file_id"] == file_id), None)
        test = next((item for item in rerun.failed_tests_of(run, entry)
                     if item.get("test_result_id") == test_id), None) if entry else None
        if entry is not None and test is not None:
            picks.append((entry, test, codes))
    return picks


def _rerun_offer(
    paths: ws.RunPaths,
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    confirmed: set[str],
    states: dict[str, str],
) -> str | None:
    """Пункт «После отчёта» с перезапуском похожих на нестабильные; кандидаты — в ``offer.json``.

    Не предлагается, если перезапуск уже был в этом разборе, выключен или запускать нечего
    (не Maven, нет Maven): иначе «да» пользователя упиралось бы в отказ.
    """
    files = rerun.job_files(paths)
    settings = _rerun_settings()
    if (isinstance(settings, str) or settings.rerun_max_tests == 0
            or rerun.job_state(files).state != "none"):
        files.offer.unlink(missing_ok=True)
        return None
    categories = {file_id: analysis.category for file_id, analysis in analyses.items()
                  if file_id not in flagged}
    # Проблема с правкой агента (предложенной или применённой) — дефект теста; её перезапуск —
    # проверка правки, а применённая после вопроса правка изменила бы план под «да».
    found = flaky.candidates(run, categories, confirmed | set(states), flaky.marks_for_run(run))
    plan = rerun.build_plan(
        paths, run, [(entry, test) for entry, test, _ in found],
        attempts=settings.rerun_attempts, max_tests=settings.rerun_max_tests,
        signals={int(test["test_result_id"]): codes for _, test, codes in found}, auto=True,
    ) if found else None
    if plan is None or plan.note or not plan.tests:
        files.offer.unlink(missing_ok=True)
        return None
    rerun.write_offer(files, [(entry["file_id"], int(test["test_result_id"]), codes)
                              for entry, test, codes in found])
    command = ws.skill_command("rerun", "--run", str(paths.root), "--yes", "--plan", plan.digest)
    return rerun.render_offer(plan, command)


def _rerun_after_apply(paths: ws.RunPaths, run: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """После ``apply``: проверить правку перезапуском тестов проблемы — та же команда ``rerun N``.

    ``None`` — перезапуск выключен, уже идёт или запускать нечего (не Maven, нет Maven): тогда
    ответ, как раньше, просит пользователя запустить тест самому.
    """
    settings = _rerun_settings()
    if (isinstance(settings, str) or settings.rerun_max_tests == 0
            or rerun.job_state(rerun.job_files(paths)).state == "running"):
        return None
    picks, signals = _problem_picks(run, [entry])
    plan = rerun.build_plan(paths, run, picks, attempts=settings.rerun_attempts,
                            max_tests=settings.rerun_max_tests, signals=signals)
    if plan.note or not plan.tests:
        return None
    number = int(entry["file_id"])
    command = ws.skill_command("rerun", str(number), "--run", str(paths.root), "--yes",
                               "--plan", plan.digest)
    return rerun.render_fix_offer(plan, command, number)


def _rerun_state(
    paths: ws.RunPaths, run: dict[str, Any], state: rerun.JobState,
) -> tuple[str, str]:
    files = rerun.job_files(paths)
    if state.state == "running":
        wait = ws.skill_command("rerun", "--run", str(paths.root), "--wait")
        return "running", rerun.render_running(state, rerun.count_wait(files), wait)
    view = rerun.RerunView.of(state)
    if view is None:
        retry = ws.skill_command("rerun", *rerun.plan_problems(state), "--run", str(paths.root))
        return "ok", rerun.render_failed(state, retry)
    # Разбор не закончен — итог попадёт в отчёт, когда next его соберёт.
    refreshed = _refresh_report(paths)
    lines = [
        "Выведи пользователю итог между маркерами дословно, без пересказа; краткий разбор "
        "заново не выводи:",
        rerun.render_result(view, files.logs, paths.report if refreshed else None),
    ]
    assert state.result is not None
    question = _save_question(paths, run, state, *_save_items(run, state))
    if question:
        lines.append(question)
    return "rerun_done", "\n".join(lines)


def _save_parts(keep: flaky.RememberPlan, marks: list[resolve.Mark]) -> dict[str, set[int]]:
    """Операции сохранения: id тестов базы знаний и результатов TestOps по видам."""
    return {"mark": {int(item["test_result_id"]) for item in keep.mark},
            "unmark": {int(item["test_result_id"]) for item in keep.unmark},
            "testops": {value for mark in marks for value in mark.ids}}


def _save_question(
    paths: ws.RunPaths, run: dict[str, Any], state: rerun.JobState,
    keep: flaky.RememberPlan, marks: list[resolve.Mark],
) -> str:
    """Вопрос о сохранении итога с командой; показанный состав — в ``save-offer.json``."""
    assert state.result is not None
    files = rerun.job_files(paths)
    job = str(state.result["job"])
    rerun.write_save_offer(files, job, _save_parts(keep, marks))
    if keep or marks:
        what = []
        if keep.mark:
            names = "; ".join(str(item["full_name"]) for item in keep.mark)
            what.append(f"отметить в базе знаний нестабильными {rerun.tests_phrase(len(keep.mark))} "
                        f"({names})")
        if keep.unmark:
            names = "; ".join(str(item["full_name"]) for item in keep.unmark)
            what.append(f"снять в базе знаний отметку «нестабильный» ({names}): "
                        + ("тест воспроизводится" if len(keep.unmark) == 1
                           else "тесты воспроизводятся") + " каждый раз")
        if marks:
            text = (f"отметить успешными в TestOps (статус passed в прогоне {run['launch_id']}) "
                    f"{rerun.tests_phrase(len(marks))} ({resolve.names(marks)})")
            changed = [mark for mark in marks if mark.changed]
            if changed and len(changed) == len(marks):
                text += (" — " + ("прошёл" if len(marks) == 1 else "прошли")
                         + " на изменённом коде теста, в CI этого изменения ещё нет")
            elif changed:
                text += (f", из них на изменённом коде теста — {resolve.names(changed)}: в CI "
                         "этого изменения ещё нет")
            what.append(text)
        command = ws.skill_command("rerun", "--run", str(paths.root), "--remember", "--result",
                                   str(state.result["job"]))
        several = len(keep.mark) + len(keep.unmark) + len(marks) > 1
        return (
            "После итога задай пользователю один вопрос — сохранить ли итог: " + "; ".join(what)
            + "." + (" Тогда следующие разборы сразу покажут, какие тесты нестабильны."
                     if keep.mark else "")
            + " На «нет» ничего не выполняй."
            + (" Согласие только с частью («в базу знаний да, TestOps не трогай», «только первый "
               "тест») — не «да»: ничего не выполняй и скажи, что команда сохраняет всё сразу."
               if several else "")
            + (" Отметку в TestOps пользователь может выключить сам — ALLURE_RERUN_RESOLVE=false "
               "в .env скилла (.env и переменные окружения сам не меняй); когда он скажет, что "
               "выключил, выполни ту же команду без нового вопроса: она сохранит остальное."
               if keep and marks else "")
            + " Прежние «да» (на перезапуск, на правку) сюда не относятся. Только после явного "
            f"«да» на этот вопрос выполни: {command}"
        )
    return ""


def _save_items(run: dict[str, Any], state: rerun.JobState) -> tuple[flaky.RememberPlan, list[resolve.Mark]]:
    """Что сохранить по итогу: отметки базы знаний и результаты для статуса passed в TestOps."""
    assert state.result is not None and state.plan is not None
    marks = flaky.marks_for_run(run)
    keep = flaky.remember_plan(state.result, state.plan, marks)
    # Уже отмечен по этому прогону — сохранён прежним «да»: вопрос не повторяется.
    keep = flaky.RememberPlan([item for item in keep.mark if marks.get(
        int(item["test_result_id"]), {}).get("launch_id") != run["launch_id"]], keep.unmark)
    settings = _rerun_settings()
    if isinstance(settings, str) or not settings.rerun_resolve:
        return keep, []
    messages = {int(test["test_result_id"]): str(test.get("status_message") or "")
                for test in (run.get("triage") or {}).get("failed_tests", [])}
    return keep, resolve.plan_marks(state.result, state.plan, state.resolved, messages)


def _rerun_remember(
    paths: ws.RunPaths, run: dict[str, Any], state: rerun.JobState, job_id: str | None,
    started: float,
) -> tuple[str, str]:
    """Сохранить итог перезапуска: отметки в базе знаний (``alla-kb/flaky/``) и статус passed
    прошедших тестов в TestOps (``resolve.py``)."""
    if state.state != "done" or state.result is None or state.plan is None \
            or state.result.get("job") != job_id:
        return "error", (f"Итога перезапуска «{job_id}» в разборе {paths.root} нет — сохранять "
                         "нечего. Команду не повторяй; id итога — из ответа rerun_done.")
    keep, resolve_marks = _save_items(run, state)
    if not keep and not resolve_marks:
        return "ok", "Сохранять нечего. Покажи это пользователю."
    # Согласие — на показанный вопрос: больше него (настройки или база знаний изменились после
    # вопроса) команда не делает; меньше — можно (повтор для оставшегося).
    offered = rerun.read_save_offer(rerun.job_files(paths), str(job_id))
    current = _save_parts(keep, resolve_marks)
    if any(not current[name] <= offered[name] for name in rerun.SAVE_PARTS):
        return "ok", ("После вопроса изменилось, что сохранится (настройки скилла или база "
                      "знаний) — ничего не сохранено. Задай пользователю вопрос заново. "
                      + _save_question(paths, run, state, keep, resolve_marks))
    lines, folders = _remember_flaky(run, state, keep) if keep else ([], set())
    testops, resolved, repeat = _resolve_in_testops(paths, run, state, resolve_marks, started) \
        if resolve_marks else ([], False, "")
    lines += testops
    # Отметки показывает отчёт: после записи он пересобирается (хуки не повторяются).
    if (folders or resolved) and _refresh_report(paths):
        lines.append(f"Полный разбор обновлён: {paths.report}")
    if folders:
        shown = ", ".join(sorted(folders))
        lines.append(f"Скажи это пользователю и напомни закоммитить папку базы знаний {shown} "
                     f"(отметки — в {shown}/{flaky.FLAKY_DIRNAME}/). `next` не выполняй.")
    else:
        lines.append("Скажи это пользователю. `next` не выполняй.")
    if repeat:
        lines.append(repeat)
    return ("saved" if folders or resolved else "ok"), "\n".join(lines)


def _remember_flaky(
    run: dict[str, Any], state: rerun.JobState, keep: flaky.RememberPlan,
) -> tuple[list[str], set[str]]:
    """Отметки «нестабильный» в базе знаний; строки ответа и папки базы знаний."""
    assert state.result is not None
    keys = flaky.keys_of_tests(run)
    entries = {entry["file_id"]: entry for entry in run["clusters"]}
    project_root = Path(run["project_root"])
    today = date.today().isoformat()
    marked: list[str] = []
    unmarked: list[str] = []
    folders: set[str] = set()
    for item in [*keep.mark, *keep.unmark]:
        entry = entries.get(str(item.get("file_id")))
        key = keys.get(int(item["test_result_id"]))
        if entry is None or not key:
            continue
        store = flaky.FlakyStore(Path(entry["kb_dir"]))
        if item in keep.mark:
            store.mark(key, {"launch_id": run["launch_id"], "passed_on": item.get("attempt"),
                             "attempts": state.result.get("attempts"),
                             "signals": item.get("signals") or []}, today)
            marked.append(str(item["full_name"]))
        elif store.unmark(key):
            unmarked.append(str(item["full_name"]))
        folders.add(kb_label(project_root, Path(entry["kb_dir"])))
    if not folders:
        return [], folders  # тестов разбора не нашлось — в базу знаний ничего не записано
    lines = ["Сохранено в базу знаний."]
    if marked:
        lines.append("Отмечены нестабильными: " + "; ".join(marked) + ".")
    if unmarked:
        lines.append("Сняты отметки (тест воспроизводится): " + "; ".join(unmarked) + ".")
    return lines, folders


def _resolve_in_testops(
    paths: ws.RunPaths, run: dict[str, Any], state: rerun.JobState, marks: list[resolve.Mark],
    started: float,
) -> tuple[list[str], bool, str]:
    """Статус passed прошедшим тестам в TestOps.

    Возвращает строки ответа, отмечен ли хоть один результат, и строку повтора — если не всё
    успело уйти за один ответ (Qwen обрывает команду через 120 с): повтор той же команды
    отправит только остальное.
    """
    assert state.result is not None
    env_file = ws.SKILL_DIR / ".env"
    try:
        settings = Settings.load(env_file=env_file)
    except ConfigurationError as exc:
        return [f"В TestOps не отмечены ({resolve.names(marks)}): настройки TestOps в {env_file} "
                f"неверны: {exc}. {USER_ACTION_NOTE}"], False, ""
    # id результатов — этого TestOps: адрес в .env сменился — запрос задел бы чужие результаты.
    if not str(run.get("launch_url") or "").startswith(settings.endpoint.rstrip("/") + "/launch/"):
        return [f"В TestOps не отмечены ({resolve.names(marks)}): прогон разбирался на другом "
                f"TestOps, чем ALLURE_ENDPOINT сейчас ({settings.endpoint}). Отметить можно, "
                "вернув прежний адрес."], False, ""
    files = rerun.job_files(paths)
    job = str(state.result["job"])
    today = date.today().isoformat()

    def keep(test_id: int) -> None:  # сразу: оборванная команда не теряет отмеченное
        rerun.add_resolved(files, job, {test_id: today})

    outcome = asyncio.run(resolve.send(
        settings, marks, env_file, today,
        deadline=started + rerun.WAIT_BUDGET, on_done=keep))
    command = ws.skill_command("rerun", "--run", str(paths.root), "--remember", "--result", job)
    lines = []
    done = outcome.done_marks(marks)
    if done:
        lines.append(f"Отмечены успешными в TestOps (статус passed в прогоне {run['launch_id']}): "
                     f"{resolve.names(done)}.")
    partly = outcome.partly(marks)
    if partly:
        lines.append("Отмечены в TestOps частично (у метода с параметрами — не все варианты): "
                     + "; ".join(f"{mark.full_name} — passed у {count} из {len(mark.variants)}"
                                 for mark, count in partly) + ".")
    # Автоматический повтор — только без отказов: тогда каждый круг отмечает хоть один
    # результат и повторы конечны; отказы съели бы срок каждого круга заново (ревью Codex).
    if outcome.left and outcome.failed:
        lines.append(f"Не успели уйти в TestOps за один ответ: {resolve.names(outcome.left)}.")
    if outcome.failed:
        lines.append("Не отмечены в TestOps: "
                     + "; ".join(f"{mark.full_name} — {reason}" for mark, reason in outcome.failed)
                     + ". Сам команду не повторяй: когда пользователь устранит причину, повторить "
                     f"можно той же командой ({command}) — она отправит неотмеченное, уже "
                     f"отмеченные не тронет. {USER_ACTION_NOTE}")
    repeat = ""
    if outcome.left and not outcome.failed:
        repeat = (f"Не успели уйти в TestOps за один ответ: {resolve.names(outcome.left)}. "
                  "Согласие пользователя на них уже есть: скажи ему, что отправка продолжается, и "
                  "сразу, в том же ходе, выполни ту же команду ещё раз — она отправит только "
                  f"остальное: {command}")
    return lines, bool(outcome.done), repeat


def _refresh_report(paths: ws.RunPaths) -> bool:
    """Пересобрать ``report.md`` с итогом перезапуска; ``False`` — разбор не закончен.

    Это не новый отчёт: хуки ``after_report`` не повторяются — отметка расширения, сработавшего
    на прежний отчёт, переносится на новый.
    """
    if not paths.report.is_file():
        return False
    before = hashlib.sha256(paths.report.read_bytes()).hexdigest()
    status, _ = _next_step(paths, [])
    if status != "done":
        return False
    after = hashlib.sha256(paths.report.read_bytes()).hexdigest()
    state = _read_state(paths)
    fired = dict(state.get("extension_reports") or {})
    if after != before and before in fired.values():
        state["extension_reports"] = {name: after if digest == before else digest
                                      for name, digest in fired.items()}
        ws.write_json(paths.state_json, state)
    return True


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
        analysis, errors = _check_analysis(text, entry, project_root, paths)
        if errors:
            failed += 1
            lines += [
                f"Кластер {file_id}: разбор не прошёл проверку:",
                *(f"- {error}" for error in errors),
                parse_summary(analysis, observed(entry), example_blocks(entry)),
                f"Исправь файл: {path}",
                f"Задание кластера (данные и куски S…): {paths.cluster_task(file_id)}",
            ]
        else:
            lines.append(f"Кластер {file_id}: принят.")
    if failed:
        lines.append(
            "Разбор, по-твоему, уже верен, а проверка его отклоняет? Факты не подгоняй (цитату, "
            "которую проверка не находит, замени другой дословной строкой того же куска или "
            f"убери) и допиши в конец ответа «{PROBLEM_PREFIX} <где и что не так>». После "
            f"{MAX_VERIFY_ROUNDS} проверок оставь разбор как есть."
        )
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


def _remember_many(args: argparse.Namespace, reports_dir: Path) -> int:
    """``remember N N… --from-analysis``: пользователь подтвердил разборы нескольких проблем."""
    if not args.from_analysis or args.entry:
        print("STATUS: error")
        print("Несколько номеров — только с --from-analysis и без --entry: подтверждённые "
              "пользователем разборы. Обратную связь своими словами сохраняй по одной проблеме: "
              "remember N --run <папка разбора>.")
        return 1
    try:
        paths = ws.resolve_run(args.run, reports_dir)
    except ws.RunNotFoundError as exc:
        print("STATUS: error")
        print(exc)
        return 1
    run = ws.read_json(paths.run_json)
    entries = [find_entry(run, number) for number in dict.fromkeys(args.clusters)]
    missing = [number for number, entry in zip(dict.fromkeys(args.clusters), entries, strict=True)
               if entry is None]
    if missing:
        print("STATUS: error")
        print(f"В разборе {paths.root} нет проблем №{', '.join(missing)}.")
        return 1
    status, body = remember_many(paths, run, [entry for entry in entries if entry is not None],
                                 date.today())
    print(f"STATUS: {status}")
    print(body)
    return _exit_code(status)


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
    proposal = parse_proposal(ws.read_text(path))
    project_root = Path(run["project_root"])
    files = _proposal_files(paths, file_id)
    rejected = _rejected_record(paths, run, entry)
    # Отказ раньше любого применения, и повторного (--repeat при состоянии unknown) тоже:
    # причина правки не подтверждена. Уже стоящую правку apply и так не пишет заново.
    if rejected is not None and applied_state(proposal, project_root, files) != "applied":
        return "error", (
            f"Правка для проблемы №{int(file_id)} снята: разбор опирался на запись базы знаний "
            f"{rejected.id}, которую пользователь отверг для этой проблемы. Правку не применяй; "
            "если пользователь назовёт настоящую причину — сохрани её в базу знаний."
        )
    result = apply_proposal(
        proposal,
        project_root,
        confirm=confirm,
        diff_hash=diff_hash,
        files=files,
        repeat=repeat,
    )
    if result.status == "diff":
        return "diff", result.text + _apply_hint(paths, file_id, result, repeat)
    if result.changed:
        # Порядок как в SKILL.md (раздел 4): правка применена → расширения → предложить тест.
        hooks = ext.block(ext.fire("after_apply", paths, run, {
            "problem": ext.problem_context(paths, entry, _load_apply_record(paths, file_id), run),
        }))
        return "applied", "\n".join([
            result.text,
            "Откатить: " + ws.skill_command("revert", file_id, "--run", str(paths.root)),
            *([hooks] if hooks else []),
            _rerun_after_apply(paths, run, entry)
            or "Тесты не запускай: предложи пользователю запустить исправленный тест.",
        ])
    return result.status, result.text


def _rejected_record(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entry: dict[str, Any],
    analysis: ClusterAnalysis | None = None,
) -> KnownRecord | None:
    """Запись базы знаний, на которую опирался разбор и которую потом отвергли (``reject``)."""
    if analysis is None:
        path = paths.analysis(entry["file_id"])
        if not path.is_file():
            return None
        analysis = parse_analysis(ws.read_text(path))
    if not analysis.kb_ref:
        return None
    known = known_issues({**run, "clusters": [entry]}, {entry["file_id"]: analysis}, set(), paths)
    return known.rejected.get(entry["file_id"])


def _withdrawn_fix(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entry: dict[str, Any],
    analysis: ClusterAnalysis,
    project_root: Path,
) -> bool:
    """Правку не просить и не предлагать: разбор опирался на отвергнутую запись, а правка
    (если она есть) ещё не стоит в файле."""
    if _rejected_record(paths, run, entry, analysis) is None:
        return False
    path = paths.proposal(entry["file_id"])
    if not path.is_file():
        return True
    proposal = parse_proposal(ws.read_text(path))
    files = _proposal_files(paths, entry["file_id"])
    return not proposal.is_fix or applied_state(proposal, project_root, files) == "not_applied"


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
    record = _load_apply_record(paths, entry["file_id"])  # revert удалит отметку
    result = revert_proposal(Path(run["project_root"]), _proposal_files(paths, entry["file_id"]))
    if result.status != "reverted":
        return result.status, result.text
    hooks = ext.block(ext.fire("after_revert", paths, run, {
        "problem": ext.problem_context(paths, entry, record, run),
    }))
    return result.status, result.text + ("\n" + hooks if hooks else "")


def _load_apply_record(paths: ws.RunPaths, file_id: str) -> dict[str, Any] | None:
    """Отметка ``apply`` (что, где и с какими хэшами применено) для контекста расширений."""
    path = paths.proposal_record(file_id)
    try:
        data = ws.read_json(path) if path.is_file() else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _skip(paths: ws.RunPaths, entry: dict[str, Any], reason: str) -> tuple[str, str]:
    """Записать заглушку разбора: кластер пропущен по просьбе пользователя."""
    file_id = entry["file_id"]
    if entry.get("auto"):
        return "error", f"Проблема №{int(file_id)} без данных об ошибке уже разобрана автоматически."
    ws.write_text(paths.analysis(file_id), skipped_analysis(reason))
    paths.summary.unlink(missing_ok=True)
    state = _read_state(paths)
    state["skipped"] = sorted({*state.get("skipped", []), file_id})
    ws.write_json(paths.state_json, state)
    return "saved", "\n".join([
        f"Кластер №{int(file_id)} пропущен и попадёт в отчёт как «неизвестно».",
        f"Продолжи разбор: {paths.next_command()}",
    ])


# ---------------------------------------------------------------------------
# extensions
# ---------------------------------------------------------------------------


def cmd_extensions(args: argparse.Namespace, project_root: Path, reports_dir: Path) -> int:
    command = args.ext_command or "list"
    try:
        if command == "list":
            status, body = "ok", _extensions_list(project_root)
        elif command in ("enable", "disable"):
            status, body = "ok", _extensions_toggle(project_root, args.name, command == "enable")
        elif command == "run":
            status, body = "ok", _extensions_run(
                args.name, args.clusters, args.run, reports_dir, ext.parse_params(args.param),
                args.text_file,
            )
        elif command == "reconcile":
            if not args.reports_dir:
                raise ext.ExtensionError("Для reconcile явно укажи --reports-dir")
            status, body = ext.run_action(project_root, reports_dir, args.name, "reconcile",
                                          None, ["--request", args.request_id])
        else:
            run_dir: Path | None = None
            if args.run:
                # Каталог отчётов — у самой папки разбора: модель передаёт только --run.
                run_paths = ws.resolve_run(args.run, reports_dir)
                run_dir = run_paths.root
                if not args.reports_dir:
                    reports_dir = run_paths.reports_dir
            elif args.context is None and not args.reports_dir:
                raise ext.ExtensionError(
                    "Для действия без --context и --run явно укажи --reports-dir")
            status, body = ext.run_action(
                project_root, reports_dir, args.name, args.action, args.context, args.extra,
                run_dir=run_dir,
            )
    except (ext.ExtensionError, ws.RunNotFoundError, OSError, ValueError) as exc:
        status, body = "error", str(exc)
    print(f"STATUS: {status}")
    if body:
        print(body)
    return _exit_code(status)


def _extensions_list(project_root: Path) -> str:
    catalog = ext.discover(project_root)
    enabled: set[str] = set()
    config_error = ""
    try:
        enabled = set(ext.load_config(project_root).enabled)
    except ext.ExtensionError as exc:
        config_error = str(exc)
    lines = [f"Расширения проекта (включаются в {ext.config_path(project_root)}):"]
    for name, extension in sorted(catalog.extensions.items()):
        state = "включено" if name in enabled else "выключено"
        origin = "встроенное" if extension.bundled else "своё"
        if extension.overrides_bundled:
            origin += ", заменяет встроенное"
        about = f". {extension.description}" if extension.description else ""
        commands = "".join(f" Команда: /alla:{command}." for command in extension.commands)
        lines.append(f"- {name} — {state} ({origin}): {extension.title}{about}{commands}")
    for name, error in sorted(catalog.errors.items()):
        lines.append(f"- {name} — не работает: {error}")
    for name in sorted(enabled - set(catalog.extensions) - set(catalog.errors)):
        lines.append(f"- {name} — включено, но такого расширения нет")
    if not catalog.extensions and not catalog.errors:
        lines.append("- расширений нет")
    if config_error:
        lines.append(f"Ошибка настроек: {config_error}")
    lines += [
        f"Включить: {ws.skill_command('extensions', 'enable', '<имя>')}",
        f"Выключить: {ws.skill_command('extensions', 'disable', '<имя>')}",
        f"Свои расширения кладут в {ext.project_dir(project_root)}/<имя>/; как их писать — "
        f"{ext.bundled_dir() / 'README.md'}.",
    ]
    return "\n".join(lines)


def _extensions_toggle(project_root: Path, name: str, on: bool) -> str:
    catalog = ext.discover(project_root)
    extension = catalog.extensions.get(name)
    settings: dict[str, Any] = {}
    if on:
        if extension is None:
            if name in catalog.errors:
                raise ext.ExtensionError(f"Расширение {name} не работает: {catalog.errors[name]}")
            raise ext.ExtensionError(
                f"Расширения {name} нет. Список: {ws.skill_command('extensions', 'list')}"
            )
        # Неверные настройки в config.toml видны сразу, а не при первом событии.
        config = ext.load_config(project_root)
        settings = ext.resolved_settings(extension, config)
        conflict = ext.command_conflict(extension, catalog, config)
        if conflict:  # до записи config.toml и файлов команд: чужая команда не затирается
            raise ext.ExtensionError(f"Расширение {name} не включено: {conflict}")
    changed = ext.set_enabled(project_root, name, on)
    verb = "включено" if on else "выключено"
    lines = [
        f"Расширение {name} {verb} в {ext.config_path(project_root)}." if changed
        else f"Расширение {name} уже {verb}."
    ]
    if extension is not None and extension.agent is not None:
        if on:
            install_extension_agent(project_root, extension.agent, extension.agent_name)
            lines.append(
                f"Субагент {extension.agent_name} установлен в .qwen/agents/: Qwen Code увидит его "
                "после перезапуска сеанса (до того скилл запускает задание без subagent_type)."
            )
        elif remove_extension_agent(project_root, extension.agent, extension.agent_name) == "kept":
            lines.append(
                f"Файл субагента .qwen/agents/{extension.agent_name}.md изменён вручную — не удаляю."
            )
    if extension is not None and extension.commands:
        names = ", ".join(f"/alla:{command}" for command in extension.commands)
        if on:
            for command, source in extension.commands.items():
                install_extension_command(project_root, source, command)
            lines.append(
                f"Команды {names} установлены в .qwen/commands/alla/: Qwen Code увидит их после "
                "перезапуска сеанса."
            )
        else:
            kept = [f"/alla:{command}" for command, source in extension.commands.items()
                    if remove_extension_command(project_root, source, command) == "kept"]
            if kept:
                lines.append(f"Команды {', '.join(kept)} изменены вручную — не удаляю.")
    if on and settings:
        lines.append(
            f"Настройки (меняются в таблице [{name}] файла {ext.CONFIG_FILE}): "
            # Как в TOML: «remote = "origin"», «confirm = false».
            + ", ".join(f"{key} = {json.dumps(value, ensure_ascii=False)}"
                        for key, value in settings.items())
        )
    return "\n".join(lines)


def _extensions_run(
    name: str,
    clusters: list[str],
    run_dir: str,
    reports_dir: Path,
    params: dict[str, list[str]],
    text_files: list[list[str]] | None = None,
) -> str:
    """Запустить расширение по просьбе пользователя (``on_request``) или повторить его хук."""
    paths = ws.resolve_run(run_dir, reports_dir)
    run = ws.read_json(paths.run_json)
    project_root = Path(run["project_root"])
    extension, _ = ext.find_active(project_root, name)
    if extension.hooks_for(ext.MANY_REQUEST_EVENT):
        from alla_skill_lib import extension_data

        # Существование и пригодность номеров оценивает расширение по снимку:
        # отклонённый номер не должен скрывать остальные выбранные проблемы.
        try:
            selected = sorted({int(number) for number in clusters})
        except ValueError as exc:
            raise ext.ExtensionError("Номера проблем должны быть целыми числами") from exc
        if any(number < 1 for number in selected):
            raise ext.ExtensionError("Номера проблем должны быть положительными")
        directory = paths.extension_dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        texts = extension_data.text_files(text_files or [], directory)
        if set(texts) & set(params):
            raise ext.ExtensionError("Один ключ нельзя передать через --param и --text-file")
        return ext.block(ext.fire(ext.MANY_REQUEST_EVENT, paths, run, {
            "selected_numbers": selected, "params": params, "text_params": texts,
        }, only={name}))
    if text_files:
        raise ext.ExtensionError("--text-file поддерживается только событием on_request_many")
    if extension.hooks_for(ext.REQUEST_EVENT):
        return _extensions_request(extension, clusters, paths, run, params)
    if params:
        raise ext.ExtensionError(
            f"У расширения {name} нет хука {ext.REQUEST_EVENT} — параметры --param ему не передать."
        )
    if len(clusters) > 1:
        raise ext.ExtensionError("Повтор хука — для одной проблемы за раз.")
    cluster = clusters[0] if clusters else None
    if cluster is None:
        status, body = _next_step(paths, [])
        if status != "done":
            raise ext.ExtensionError(
                f"Разбор {paths.root} ещё не закончен — отчёта нет. Продолжи: {paths.next_command()}"
            )
        event = "after_report"
        lines = ext.fire(event, paths, run, _report_data(paths, run, body), only={name})
    else:
        entry = find_entry(run, cluster)
        if entry is None:
            raise ext.ExtensionError(f"В разборе {paths.root} нет проблемы №{cluster}.")
        file_id = entry["file_id"]
        proposal_path = paths.proposal(file_id)
        proposal = parse_proposal(ws.read_text(proposal_path)) if proposal_path.is_file() else None
        if proposal is None or not proposal.is_fix or applied_state(
            proposal, project_root, _proposal_files(paths, file_id)
        ) != "applied":
            raise ext.ExtensionError(
                f"Правка для проблемы №{int(file_id)} сейчас не применена — повторять нечего."
            )
        event = "after_apply"
        lines = ext.fire(event, paths, run, {
            "problem": ext.problem_context(paths, entry, _load_apply_record(paths, file_id), run),
        }, only={name})
    return ext.block(lines) or f"У расширения {name} нет хука на событие {event}."


def _extensions_request(
    extension: ext.Extension,
    clusters: list[str],
    paths: ws.RunPaths,
    run: dict[str, Any],
    params: dict[str, list[str]],
) -> str:
    """Событие ``on_request``: по пункту блока «Расширения:» на каждую названную проблему."""
    if not clusters:
        raise ext.ExtensionError(
            f"Назови номер проблемы из отчёта: {ws.skill_command('extensions', 'run', extension.name, 'N', '--run', str(paths.root))}"
        )
    entries = []
    for cluster in dict.fromkeys(clusters):
        entry = find_entry(run, cluster)
        if entry is None:
            raise ext.ExtensionError(f"В разборе {paths.root} нет проблемы №{cluster}.")
        entries.append(entry)
    lines: list[str] = []
    deadline = ext.hook_deadline()  # один бюджет на все проблемы команды
    for entry in entries:
        lines += ext.fire(ext.REQUEST_EVENT, paths, run, {
            "problem": ext.problem_context(
                paths, entry, _load_apply_record(paths, entry["file_id"]), run),
            "params": params,
        }, only={extension.name}, deadline=deadline)
    return ext.block(lines)


def _install_extension_agents(project_root: Path) -> None:
    """Субагенты и слэш-команды включённых расширений — в ``.qwen/`` проекта."""
    for extension, _ in ext.active(project_root).extensions:
        if extension.agent is not None and install_extension_agent(
            project_root, extension.agent, extension.agent_name
        ):
            _progress(
                f"Установлен субагент расширения {extension.agent_name} (Qwen Code увидит его со "
                "следующего сеанса)."
            )
        for name, source in extension.commands.items():
            if install_extension_command(project_root, source, name):
                _progress(
                    f"Установлена команда /alla:{name} расширения {extension.name} (Qwen Code "
                    "увидит её со следующего сеанса)."
                )


def _with_report_hooks(paths: ws.RunPaths, body: str) -> str:
    """Хуки ``after_report`` и неисправности включённых расширений — один раз на содержимое
    отчёта: повторный ``next`` с тем же отчётом их не повторяет."""
    run = ws.read_json(paths.run_json)
    current = ext.active(Path(run["project_root"]))
    names = [extension.name for extension, _ in current.extensions
             if extension.hooks_for("after_report")]
    if not (names or current.problems) or not paths.report.is_file():
        return body
    digest = hashlib.sha256(paths.report.read_bytes()).hexdigest()
    state = _read_state(paths)
    lines: list[str] = []
    # Сломанное расширение исключено из активных, и его хук молча не сработал бы: сказать.
    problems = (
        hashlib.sha256("\n".join([digest, *current.problems]).encode("utf-8")).hexdigest()
        if current.problems else None
    )
    changed = False
    if problems is not None and state.get("extension_problems") != problems:
        lines += ext.problem_lines(current.problems)
        state["extension_problems"] = problems
        changed = True
    fired = dict(state.get("extension_reports") or {})
    pending = [name for name in names if fired.get(name) != digest]
    if pending:
        state["extension_reports"] = {**fired, **dict.fromkeys(pending, digest)}
        changed = True
    # Отметку пишем и при пустом выводе (молчаливый хук тоже сработал и повторяться не должен),
    # и до запуска хуков: команду, оборванную на зависшем хуке, следующий next не повторяет
    # хук снова и снова, а показывает отчёт.
    if changed:
        ws.write_json(paths.state_json, state)
    if pending:
        lines += ext.fire("after_report", paths, run, _report_data(paths, run, body), only=pending)
    return f"{body}\n{ext.block(lines)}" if lines else body


def _report_data(paths: ws.RunPaths, run: dict[str, Any], body: str) -> dict[str, Any]:
    """Поля события ``after_report``: краткий разбор и список проблем."""
    _, _, rest = body.partition(REPORT_BEGIN + "\n")
    console, _, _ = rest.partition("\n" + REPORT_END)
    problems = []
    confirmed = confirmations(paths, run)
    for entry in run["clusters"]:
        path = paths.analysis(entry["file_id"])
        analysis = parse_analysis(ws.read_text(path)) if path.is_file() else None
        sure = confirmed.get(entry["file_id"])
        problems.append({
            "number": int(entry["file_id"]),
            "label": " ".join(str(entry.get("label", "")).split()),
            "tests": entry.get("member_count"),
            "category": sure["category"] if sure else analysis.category if analysis else None,
            "cause": sure["cause"] if sure else analysis.cause if analysis else "",
            "confirmed": sure is not None,
        })
    return {"console": console, "problems": problems}


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
        print(f"Пользователю нужно заполнить {env_file} по образцу .env.example. " + USER_ACTION_NOTE)
        return 2
    lines += [
        f"ALLURE_ENDPOINT: {settings.endpoint}",
        "ALLURE_TOKEN: задан (значение не показывается)",
        f"Проверка TLS: {'включена' if settings.ssl_verify else 'отключена (ALLURE_SSL_VERIFY=false)'}",
    ]
    # Корневая база показывается всегда; модульные (<модуль>/alla-kb) — если они есть.
    for kb_dir in sorted({find_kb_dir(project_root), *discover_kb_dirs(project_root)}):
        kb_records, kb_warnings = ProjectKB(kb_dir, kb_label(project_root, kb_dir)).load()
        lines.append(f"База знаний: {kb_dir} — записей {len(kb_records)}")
        lines += [f"Внимание: {warning}" for warning in kb_warnings]
    extensions = ext.active(project_root)
    if extensions.extensions:
        lines.append("Расширения: " + ", ".join(item.name for item, _ in extensions.extensions))
    lines += [f"Внимание: {problem}" for problem in extensions.problems]
    try:
        asyncio.run(_ping_testops(settings))
    except (AllaError, httpx.HTTPError) as exc:
        print("STATUS: error")
        print(*lines, sep="\n")
        print(f"TestOps недоступен или токен не принят: {exc}")
        hint = fetch_error_hint(exc, settings, env_file)
        if hint:
            print(hint)
            print(USER_ACTION_NOTE)
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
        retries=settings.http_retries,
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
