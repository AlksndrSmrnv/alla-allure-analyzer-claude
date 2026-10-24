"""Перезапуск упавших тестов локально (Maven, surefire): нестабильный тест или нет.

Модель тесты и сборку не запускает: план показывает команда ``rerun``, а после «да»
пользователя Maven запускает фоновое задание (``rerun_worker``). Qwen обрывает команду
через 120 с, поэтому ``rerun`` ждёт итога не дольше ``WAIT_BUDGET`` и отвечает ``running``.

Тест запускается до ``rerun_attempts`` раз, до первого успеха. В следующий запуск идут
только тесты, упавшие с той же ошибкой, что в TestOps: другая ошибка локально (нет
настроек, стенд недоступен с машины) о нестабильности не говорит, её повторять незачем.

Файлы задания — ``rerun/`` в папке разбора (одно задание на разбор, новое заменяет
прежнее). Одновременно в проекте идёт не больше одного задания: блокировка
``<папка отчётов>/.rerun.lock`` у фонового процесса.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePath
from typing import Any

from alla_core.utils.text_normalization import repeat_key
from alla_skill_lib import workspace as ws
from alla_skill_lib.code_hints import JvmTest, ProjectIndex, jvm_test_of
from alla_skill_lib.kb import secret_lines
from alla_skill_lib.modules import build_dir, build_tool, maven_reactor

WAIT_BUDGET = 75.0  # секунд на один ответ: Qwen обрывает команду через 120 с
MAX_WAITS = 4  # ответов «идёт» подряд, после которых модель перестаёт ждать сама
BUILD_OVERHEAD_S = 45  # запуск Maven и компиляция — для оценки времени
MESSAGE_CHARS = 300
ERROR_LINES = 5
LOCK_NAME = ".rerun.lock"
KILL_GRACE_S = 10
STALE_MARGIN_S = 300  # задание дольше своего потолка времени — процесс уже не наш
# Окружение Maven без настроек скилла (токен TestOps) и переменных сеанса Qwen.
DROPPED_ENV_PREFIXES = ("ALLURE_", "QWEN_CODE_")
DROPPED_ENV = frozenset({"PYTHONPATH"})
NOT_IN_REACTOR = "Could not find the selected project in the reactor"

OUTCOMES = ("passed", "reproduced", "other", "not_run")
MAX_SHOWN_SKIPPED = 10
MESSAGE_KEY_LINES = 20
MESSAGE_KEEP_CHARS = 4000
MAX_OFFER_NAMES = 5
# Признаки нестабильности теста (отбор — ``flaky.py``): код → как назвать пользователю.
SIGNALS = {
    "kb": "отмечен нестабильным в базе знаний",
    "retry_passed": "в TestOps повтор прошёл",
    "retry_different": "в TestOps повтор упал с другой ошибкой",
    "transient": "ошибка похожа на временный сбой",
}


@dataclass(frozen=True)
class PlannedTest:
    """Тест в плане перезапуска: как его найти в TestOps и как запустить в Maven."""

    test_result_id: int
    file_id: str
    name: str
    full_name: str
    class_name: str
    method: str
    source: str
    module: str  # папка проекта сборки от корня проекта, ``""`` — корень
    message: str  # первая строка ошибки в TestOps
    duration_ms: int | None = None
    changed: str = ""  # «правка агента применена» / «код теста изменён» — проверка правки
    signals: list[str] = field(default_factory=list)  # коды ``SIGNALS``
    source_sha: str = ""  # исходник на момент плана (``source_digest``)


@dataclass(frozen=True)
class SkippedTest:
    file_id: str
    name: str
    reason: str


@dataclass(frozen=True)
class MavenGroup:
    """Один запуск Maven на модуль: откуда, чем и с каким выбором проекта."""

    module: str
    cwd: str  # корень реактора (абсолютный путь)
    tool: list[str]
    select: list[str]  # ``-pl <модуль> -am`` или пусто
    fallback_cwd: str  # папка модуля: запуск без реактора, если модуля нет в ``<modules>``


@dataclass
class Plan:
    tests: list[PlannedTest] = field(default_factory=list)
    skipped: list[SkippedTest] = field(default_factory=list)
    groups: list[MavenGroup] = field(default_factory=list)
    attempts: int = 3
    note: str = ""  # почему перезапустить нельзя вообще (не Maven, нет Maven…)
    project_root: str = ""

    @property
    def digest(self) -> str:
        """Хэш плана: ``--yes --plan`` запускает ровно показанный пользователю план."""
        material = json.dumps(
            {"attempts": self.attempts,
             "tests": [[t.test_result_id, t.class_name, t.method, t.module] for t in self.tests]},
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]

    def estimate_minutes(self) -> int:
        """Верхняя оценка: все тесты падают каждый раз, плюс запуск Maven на модуль."""
        tests = sum((t.duration_ms or 0) for t in self.tests) / 1000
        seconds = (tests + BUILD_OVERHEAD_S * max(1, len(self.groups))) * self.attempts
        return max(1, round(seconds / 60))


# ---------------------------------------------------------------------------
# план
# ---------------------------------------------------------------------------


def failed_tests_of(run: dict[str, Any], entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Тесты проблемы из ``run.json`` (``FailedTestSummary``), представитель первым."""
    clustering = run.get("clustering") or {}
    cluster: dict[str, Any] = next((item for item in clustering.get("clusters", [])
                                    if item.get("cluster_id") == entry.get("cluster_id")), {})
    representative = cluster.get("representative_test_id")
    members = sorted(cluster.get("member_test_ids", []), key=lambda item: item != representative)
    by_id = {test.get("test_result_id"): test
             for test in (run.get("triage") or {}).get("failed_tests", [])}
    return [by_id[test_id] for test_id in members if test_id in by_id]


def build_plan(
    paths: ws.RunPaths,
    run: dict[str, Any],
    picks: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    *,
    attempts: int,
    max_tests: int,
    signals: Mapping[int, list[str]] | None = None,
    auto: bool = False,
) -> Plan:
    """План по парам (запись кластера, тест): что запустить, что нельзя и почему.

    ``auto`` — кандидаты скилла (``flaky.py``): тест с изменённым кодом в план не идёт, его
    перезапуск — проверка правки, а не нестабильности.
    """
    plan = Plan(attempts=attempts)
    if os.name == "nt":
        plan.note = "перезапуск тестов пока работает только на macOS и Linux"
        return plan
    project_root = Path(run["project_root"])
    plan.project_root = str(project_root)
    index = ProjectIndex(project_root)
    seen: set[tuple[str, str]] = set()
    for entry, test in picks:
        file_id = entry["file_id"]
        name = str(test.get("full_name") or test.get("name") or test.get("test_result_id"))
        located = locate(project_root, index, str(test.get("full_name") or ""))
        if isinstance(located, str):
            plan.skipped.append(SkippedTest(file_id, name, located))
            continue
        jvm, module = located
        if (jvm.class_name, jvm.method) in seen:  # параметры одного метода — один запуск
            continue
        changed = _changed(paths, project_root, file_id, jvm.source)
        if auto and changed:
            plan.skipped.append(SkippedTest(
                file_id, name, f"{changed} — перезапуск не скажет о нестабильности"))
            continue
        if len(plan.tests) >= max_tests:
            plan.skipped.append(SkippedTest(file_id, name, f"больше {max_tests} тестов за раз"))
            continue
        seen.add((jvm.class_name, jvm.method))
        plan.tests.append(PlannedTest(
            test_result_id=int(test["test_result_id"]),
            file_id=file_id,
            name=str(test.get("name") or name),
            full_name=name,
            class_name=jvm.class_name,
            method=jvm.method,
            source=jvm.source.as_posix(),
            module=module,
            message=str(test.get("status_message") or "")[:MESSAGE_KEEP_CHARS],
            duration_ms=test.get("duration_ms"),
            changed=changed,
            signals=list((signals or {}).get(int(test["test_result_id"]), [])),
            source_sha=source_digest(project_root, jvm.source.as_posix()),
        ))
    plan.groups = _groups(project_root, plan)
    if plan.tests and not plan.groups:
        plan.note = ("Maven не найден: нет ни mvnw в проекте, ни mvn в PATH. Пользователю нужно "
                     "установить Maven или добавить Maven Wrapper")
        plan.tests = []
    return plan


def locate(project_root: Path, index: ProjectIndex, full_name: str) -> tuple[JvmTest, str] | str:
    """Тест и папка его проекта сборки; не вышло — причина словами."""
    found = jvm_test_of(index, full_name) if full_name else "у теста нет полного имени"
    if isinstance(found, str):
        return found
    if not all(_IDENT_RE.fullmatch(part) for part in [*found.class_name.split("."), found.method]):
        # Имя из TestOps — недоверенные данные: «+», «*», «,» surefire понял бы как шаблон и
        # запустил бы больше, чем показано в плане.
        return "в имени теста есть символы, которые Maven понял бы как шаблон"
    directory = build_dir(project_root, found.source)
    if directory is None:
        return "нет файла сборки (pom.xml)"
    tool = build_tool(project_root, directory)
    if tool == "gradle":
        return "Gradle пока не поддерживается — только Maven"
    if tool != "maven":
        return "нет файла сборки (pom.xml)"
    module = directory.as_posix()
    return found, "" if module == "." else module


def _groups(project_root: Path, plan: Plan) -> list[MavenGroup]:
    groups: list[MavenGroup] = []
    for module in dict.fromkeys(test.module for test in plan.tests):
        module_dir = PurePath(module or ".")
        reactor = maven_reactor(project_root, module_dir)
        tool = maven_tool(project_root, reactor)
        if tool is None:
            return []
        relative = module_dir.relative_to(reactor).as_posix() if module_dir != reactor else ""
        select = ["-pl", relative, "-am"] if relative and relative != "." else []
        groups.append(MavenGroup(
            module=module,
            cwd=str(project_root / reactor),
            tool=tool,
            select=select,
            fallback_cwd=str(project_root / module_dir) if select else "",
        ))
    return groups


def maven_tool(project_root: Path, reactor: PurePath) -> list[str] | None:
    """``mvnw`` реактора, затем корня проекта (без права запуска — через ``sh``), иначе ``mvn``."""
    for folder in dict.fromkeys((project_root / reactor, project_root)):
        wrapper = folder / "mvnw"
        if wrapper.is_file():
            return [str(wrapper)] if os.access(wrapper, os.X_OK) else ["sh", str(wrapper)]
    mvn = shutil.which("mvn")
    return [mvn] if mvn else None


def _changed(paths: ws.RunPaths, project_root: Path, file_id: str, source: PurePath) -> str:
    """Код теста не тот, что в CI: прошедший тест тогда — проверка правки, а не нестабильность."""
    if paths.proposal_record(file_id).is_file():
        return "правка агента применена"
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain", "--", source.as_posix()],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return "код теста изменён" if result.returncode == 0 and result.stdout.strip() else ""


def surefire_filter(tests: Iterable[PlannedTest]) -> str:
    """``-Dtest`` surefire: ``pkg.A#m1+m2,pkg.B#m3``."""
    methods: dict[str, list[str]] = {}
    for test in tests:
        methods.setdefault(test.class_name, [])
        if test.method not in methods[test.class_name]:
            methods[test.class_name].append(test.method)
    return ",".join(f"{cls}#{'+'.join(names)}" for cls, names in methods.items())


def maven_command(group: MavenGroup, tests: Iterable[PlannedTest], suffix: str,
                  extra: list[str], *, fallback: bool = False) -> list[str]:
    return [
        *group.tool, "-B", *([] if fallback else group.select), "test",
        f"-Dtest={surefire_filter(tests)}",
        "-Dsurefire.failIfNoSpecifiedTests=false",
        "-DfailIfNoTests=false",
        "-Dmaven.test.failure.ignore=true",
        f"-Dsurefire.reportNameSuffix={suffix}",
        *extra,
    ]


def maven_args(raw: str) -> list[str]:
    """``ALLURE_RERUN_MAVEN_ARGS`` (профили, свойства стенда) — как в shell, без shell."""
    return shlex.split(raw) if raw.strip() else []


# ---------------------------------------------------------------------------
# отчёты surefire
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    status: str  # passed | failed | skipped
    message: str = ""
    flaky: bool = False  # прошёл при повторе самого surefire (rerunFailingTestsCount)


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUFFIXED_CLASS_RE = re.compile(r"\([^()]*\)$")
_OUR_REPORT_RE = re.compile(r"-alla-[\w-]+-\d+\.xml$")
_METHOD_END_RE = re.compile(r"[(\[]")
# «org.opentest4j.AssertionFailedError: …» в тексте отчёта без message: класса в сообщении
# Allure обычно нет.
_EXCEPTION_PREFIX_RE = re.compile(r"^[\w.$]+(?:Exception|Error|Throwable|Failure)\w*:\s*")


def report_snapshot(module_dir: Path) -> dict[str, int]:
    """Отчёты surefire модуля до запуска: имя → время изменения (нс)."""
    reports = module_dir / "target" / "surefire-reports"
    return {path.name: _mtime_ns(path) for path in reports.glob("TEST-*.xml")}


def read_reports(
    module_dir: Path, suffix: str, before: dict[str, int],
) -> dict[tuple[str, str], list[CaseResult]]:
    """Результаты по (класс, метод) из ``target/surefire-reports`` этого запуска.

    Файлы запуска — ``TEST-<класс>-<суффикс>.xml`` (``-Dsurefire.reportNameSuffix``); если их
    нет (pom задаёт свой суффикс), — ``TEST-*.xml``, которых не было до запуска или которые
    с тех пор изменились (``before`` — ``report_snapshot``). Время «не старше начала» не
    годится: отчёт прошлого запуска секундной давности выдал бы себя за итог сломанной сборки.
    """
    reports = module_dir / "target" / "surefire-reports"
    files = sorted(reports.glob(f"TEST-*-{suffix}.xml"))
    if not files:  # отчёты прежних перезапусков (свой суффикс) — не этого запуска
        files = [path for path in sorted(reports.glob("TEST-*.xml"))
                 if not _OUR_REPORT_RE.search(path.name)
                 and before.get(path.name) != _mtime_ns(path)]
    cases: dict[tuple[str, str], list[CaseResult]] = {}
    for path in files:
        try:
            suite = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        for case in suite.iter("testcase"):
            cls = _SUFFIXED_CLASS_RE.sub("", case.get("classname") or "").strip()
            method = _METHOD_END_RE.split(case.get("name") or "", maxsplit=1)[0].strip()
            cases.setdefault((cls, method), []).append(_case_result(case))
    return cases


def _case_result(case: ElementTree.Element) -> CaseResult:
    for tag in ("failure", "error", "rerunFailure", "rerunError"):
        failed = case.find(tag)
        if failed is not None:
            message = failed.get("message") or _EXCEPTION_PREFIX_RE.sub(
                "", first_line(failed.text or ""))
            return CaseResult("failed", message[:MESSAGE_KEEP_CHARS])
    if case.find("skipped") is not None:
        return CaseResult("skipped")
    flaky = case.find("flakyFailure") is not None or case.find("flakyError") is not None
    return CaseResult("passed", flaky=flaky)


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def source_digest(project_root: Path, source: str) -> str:
    """Хэш исходника теста: изменился во время перезапуска — итог «проверка правки»."""
    try:
        return hashlib.sha256((project_root / source).read_bytes()).hexdigest()
    except OSError:
        return ""


def method_result(cases: dict[tuple[str, str], list[CaseResult]], test: PlannedTest) -> CaseResult | None:
    """Итог метода: упал хоть один вызов — упал; прошёл — только если прошли все; иначе (хоть
    один пропущен) — пропущен: упавший в CI вариант мог и не выполниться. ``None`` — нет в
    отчётах."""
    found = cases.get((test.class_name, test.method))
    if not found:
        return None
    failed = [case for case in found if case.status == "failed"]
    if failed:
        # Вызовы упали по-разному: важен тот, что повторяет падение в CI, а не порядок в XML.
        same = [case for case in failed if same_error(case.message, test.message)]
        return min(same or failed, key=lambda case: case.message)
    if any(case.status == "skipped" for case in found):
        return CaseResult("skipped")
    return CaseResult("passed", flaky=any(case.flaky for case in found))


def same_error(local: str, testops: str) -> bool:
    """Та же ошибка, что в TestOps (``message_key``); в TestOps ошибки нет — да."""
    if not testops.strip():
        return True
    return message_key(local) == message_key(testops)


def message_key(text: str) -> str:
    """Ключ сообщения целиком (до ``MESSAGE_KEY_LINES`` строк): у многострочных ошибок первая
    строка бывает общей («1 expectation failed.» RestAssured), а коды ответа — ниже. Пробелы
    схлопнуты, время, id и длинные числа не различают (``repeat_key``)."""
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    return repeat_key("\n".join(lines[:MESSAGE_KEY_LINES]))


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def safe_text(text: str, limit: int = MESSAGE_CHARS) -> str:
    """Строка для ответа модели и отчёта: без похожего на секрет, не длиннее ``limit``."""
    line = " ".join(text.split())
    if not line or secret_lines(line):
        return ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def build_errors(log: Path) -> list[str]:
    """Первые строки ``[ERROR]`` лога Maven (причина, почему сборка не прошла)."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines: list[str] = []
    for raw in text.splitlines():
        if not raw.startswith("[ERROR]"):
            continue
        line = safe_text(raw.removeprefix("[ERROR]"))
        if line and line not in lines:
            lines.append(line)
        if len(lines) >= ERROR_LINES:
            break
    return lines


# ---------------------------------------------------------------------------
# задание
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobFiles:
    """Файлы задания в ``rerun/`` папки разбора."""

    root: Path

    @property
    def plan(self) -> Path:
        return self.root / "plan.json"

    @property
    def progress(self) -> Path:
        return self.root / "progress.json"

    @property
    def result(self) -> Path:
        return self.root / "result.json"

    @property
    def failed(self) -> Path:
        return self.root / "failed"

    @property
    def pid(self) -> Path:
        return self.root / "pid"

    @property
    def waits(self) -> Path:
        return self.root / "waits"

    @property
    def offer(self) -> Path:
        """Кандидаты скилла с последнего ``done``: по ним ``rerun`` без номеров строит план."""
        return self.root / "offer.json"

    @property
    def worker_log(self) -> Path:
        return self.root / "worker.log"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def maven_log(self, attempt: int, module: str) -> Path:
        name = re.sub(r"[^\w.-]+", "_", module) or "root"
        return self.logs / f"attempt-{attempt}-{name}.log"

    def clear(self) -> None:
        for path in (self.plan, self.progress, self.result, self.failed, self.pid, self.waits,
                     self.worker_log):
            path.unlink(missing_ok=True)
        shutil.rmtree(self.logs, ignore_errors=True)


def job_files(paths: ws.RunPaths) -> JobFiles:
    return JobFiles(paths.rerun_dir)


@dataclass
class JobState:
    state: str  # none | running | done | failed
    plan: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    reason: str = ""


def job_state(files: JobFiles) -> JobState:
    plan = _read(files.plan)
    if plan is None:
        return JobState("none")
    result = _read(files.result)
    if result is not None and result.get("job") == plan.get("job"):
        return JobState("done", plan, _read(files.progress), result)
    if files.failed.is_file():
        reason = files.failed.read_text(encoding="utf-8", errors="replace").strip()
        return JobState("failed", plan, _read(files.progress), reason=reason)
    if _alive(files, int(plan.get("timeout") or 0)):
        return JobState("running", plan, _read(files.progress))
    return JobState("failed", plan, _read(files.progress),
                    reason="фоновый процесс перезапуска прервался")


def start_job(paths: ws.RunPaths, plan: Plan, timeout: int, extra: list[str]) -> str | None:
    """Записать план и запустить фоновый процесс; вернуть id задания.

    Запуски одного разбора — по очереди (``start.lock``): второй одновременный ``--yes`` не
    затирает файлы идущего задания, а получает ``None`` — задание уже идёт.
    """
    files = job_files(paths)
    files.root.mkdir(parents=True, exist_ok=True)
    with _start_lock(files):
        if job_state(files).state == "running":
            return None
        return _start(paths, files, plan, timeout, extra)


@contextlib.contextmanager
def _start_lock(files: JobFiles) -> Iterator[None]:
    import fcntl

    with open(files.root / "start.lock", "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _start(paths: ws.RunPaths, files: JobFiles, plan: Plan, timeout: int, extra: list[str]) -> str:
    files.clear()
    job = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    data = {
        "job": job,
        "hash": plan.digest,
        "attempts": plan.attempts,
        "timeout": timeout,
        "maven_args": extra,
        "lock": str(project_lock(Path(plan.project_root))),
        "project_root": plan.project_root,
        "started": time.time(),
        "tests": [asdict(test) for test in plan.tests],
        "groups": [asdict(group) for group in plan.groups],
    }
    ws.write_json(files.plan, data)
    log = open(files.worker_log, "wb")  # noqa: SIM115 — живёт дольше этого процесса
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "alla_skill_lib.rerun_worker", str(files.root)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=worker_env(), start_new_session=True,
        )
    finally:
        log.close()
    files.pid.write_text(f"{process.pid} {int(time.time())}", encoding="utf-8")
    return job


def worker_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Окружение фонового процесса и Maven: без ``ALLURE_*``, ``QWEN_CODE_*`` и ``PYTHONPATH``."""
    source = dict(os.environ if environ is None else environ)
    return {key: value for key, value in source.items()
            if key not in DROPPED_ENV and not key.startswith(DROPPED_ENV_PREFIXES)}


def wait_job(files: JobFiles, budget: float) -> JobState:
    """Ждать итога до ``budget`` секунд; вернуть состояние на момент выхода."""
    deadline = time.monotonic() + budget
    while True:
        state = job_state(files)
        if state.state != "running" or time.monotonic() >= deadline:
            return state
        time.sleep(0.5)


def count_wait(files: JobFiles) -> int:
    """Ответов «идёт» подряд по этому заданию, включая текущий."""
    try:
        count = int(files.waits.read_text(encoding="utf-8").strip() or 0) + 1
    except (OSError, ValueError):
        count = 1
    files.waits.write_text(str(count), encoding="utf-8")
    return count


def project_lock(project_root: Path) -> Path:
    """Блокировка «один перезапуск на проект»: в папке отчётов по умолчанию этого проекта.

    Не в папке разбора и не в ``--reports-dir``: разборы одного проекта с разными папками
    отчётов иначе гоняли бы Maven по одним исходникам и ``target/`` одновременно.
    """
    folder = project_root / ws.REPORTS_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# Отчёты alla-launch не коммитятся\n*\n", encoding="utf-8")
    return folder / LOCK_NAME


def lock_busy(lock: Path) -> bool:
    """Идёт ли в проекте другое задание (блокировку держит его фоновый процесс)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows: перезапуск не поддерживается
        return False
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


@contextlib.contextmanager
def hold_lock(lock: Path) -> Iterator[bool]:
    """Держать блокировку проекта, пока идёт задание; ``False`` — занята другим."""
    import fcntl

    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _alive(files: JobFiles, timeout: int) -> bool:
    try:
        pid_text, started = files.pid.read_text(encoding="utf-8").split()
        pid = int(pid_text)
    except (OSError, ValueError):
        return False
    if time.time() - int(started) > timeout + STALE_MARGIN_S:  # pid мог достаться другому
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Завершившийся дочерний процесс этой же команды — зомби до wait: kill его не отличит.
    with contextlib.suppress(ChildProcessError, OSError):
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    return True


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# ответы команды
# ---------------------------------------------------------------------------

RESULT_BEGIN = "===ПЕРЕЗАПУСК==="
RESULT_END = "===КОНЕЦ==="


def render_plan(plan: Plan, command: str, *, has_extra: bool) -> str:
    """План для пользователя и готовая команда запуска (``--yes --plan``)."""
    lines = [f"План перезапуска локально (Maven): {_tests(len(plan.tests))}, каждый до "
             f"{plan.attempts} раз — до первого успеха."]
    lines += [f"- {_who(test.file_id, test.full_name)}{_module(test.module)}"
              + (f" — {describe(test.signals)}" if test.signals else "")
              + (f" — проверка правки ({test.changed}): о нестабильности не скажет"
                 if test.changed else "")
              for test in plan.tests]
    lines += _skipped_lines(plan)
    lines.append("Команды Maven:")
    for group in plan.groups:
        tests = [test for test in plan.tests if test.module == group.module]
        shown = shlex.join(maven_command(group, tests, "alla-<задание>-<запуск>", []))
        lines.append(f"  {group.cwd}: {shown}" + (" + ALLURE_RERUN_MAVEN_ARGS" if has_extra else ""))
    lines += [
        f"Оценка: до ~{plan.estimate_minutes()} мин. Тесты идут на этой машине с настройками "
        "проекта и обращаются к тем же сервисам, что при обычном локальном запуске.",
        "Покажи пользователю этот план (тесты, что запустить нельзя, оценку времени) и спроси, "
        f"перезапустить ли. Только после явного «да» выполни: {command}",
    ]
    return "\n".join(lines)


def render_nothing(plan: Plan) -> str:
    lines = [f"Перезапустить нечего: {plan.note}." if plan.note else "Перезапустить нечего."]
    lines += _skipped_lines(plan)
    lines.append("Покажи это пользователю.")
    return "\n".join(lines)


def render_running(state: JobState, waits: int, wait_command: str) -> str:
    progress = state.progress or {}
    plan = state.plan or {}
    minutes = max(0, round((time.time() - float(plan.get("started") or time.time())) / 60))
    if progress:
        line = (f"Перезапуск идёт: запуск {progress.get('attempt')} из {progress.get('attempts')}"
                f"{_module(str(progress.get('module') or ''))}; готово тестов: "
                f"{progress.get('done')} из {progress.get('total')} (прошли: "
                f"{progress.get('passed')}); идёт {minutes} мин.")
    else:
        line = f"Перезапуск начинается (Maven запускается); идёт {minutes} мин."
    if waits < MAX_WAITS:
        return (f"{line}\nПодожди итога: выполни {wait_command} (ждёт до "
                f"{int(WAIT_BUDGET)} с) — и так, пока ответ не сменится. Пользователю пока "
                "ничего не пиши.")
    return (f"{line}\nПерезапуск идёт долго. Скажи пользователю, что он продолжается в фоне, "
            "а ход и итог можно спросить позже: /alla:rerun или «покажи итог перезапуска». Сам "
            "больше не жди и команду не повторяй.")


def render_failed(state: JobState, retry_command: str) -> str:
    return (f"Перезапуск тестов не удался: {state.reason or 'причина неизвестна'}.\n"
            f"Покажи это пользователю. Повторить (покажет план заново): {retry_command}")


def plan_problems(state: JobState) -> list[str]:
    """Номера проблем задания (для команды повтора)."""
    tests = (state.plan or {}).get("tests") or []
    return list(dict.fromkeys(str(int(item["file_id"])) for item in tests))


RESULT_TITLES = {
    "flaky": "Нестабильные — упали в CI, а при перезапуске прошли",
    "fixed": "Прошли на изменённом коде теста — проверка правки, не нестабильность",
    "reproduced": "Воспроизводятся — упали каждый раз с той же ошибкой, что в CI",
    "other": "Падают локально с другой ошибкой — о нестабильности не говорят",
    "not_run": "Не запустились",
}

SHORT_TITLES = {
    "flaky": "нестабильные",
    "fixed": "прошли на изменённом коде",
    "reproduced": "воспроизводятся",
    "other": "падают локально иначе",
    "not_run": "не запустились",
}


@dataclass(frozen=True)
class RerunView:
    """Итог законченного перезапуска для ответа команды и для отчёта."""

    attempts: int
    tests: list[dict[str, Any]]
    build_errors: dict[str, list[str]]

    @classmethod
    def of(cls, state: JobState) -> RerunView | None:
        if state.state != "done" or state.result is None:
            return None
        result = state.result
        return cls(int(result.get("attempts") or 0), list(result.get("tests") or []),
                   dict(result.get("build_errors") or {}))

    @staticmethod
    def group(item: dict[str, Any]) -> str:
        """Группа итога (ключ ``RESULT_TITLES``)."""
        outcome = item.get("outcome")
        if outcome == "passed":
            return "fixed" if item.get("changed") else "flaky"
        return outcome if outcome in ("reproduced", "other") else "not_run"

    @property
    def flaky(self) -> int:
        return sum(1 for item in self.tests if self.group(item) == "flaky")

    def flaky_in(self, file_id: str) -> tuple[int, int]:
        """Сколько тестов проблемы прошло при перезапуске (не проверка правки) и сколько шло."""
        own = [item for item in self.tests if item.get("file_id") == file_id]
        return sum(1 for item in own if self.group(item) == "flaky"), len(own)

    def summary_of(self, file_id: str) -> str:
        """Итог проблемы одной строкой: «2 теста: нестабильные — 1, воспроизводятся — 1»."""
        own = [self.group(item) for item in self.tests if item.get("file_id") == file_id]
        if not own:
            return ""
        parts = [f"{SHORT_TITLES[key]} — {own.count(key)}" for key in RESULT_TITLES if key in own]
        return f"{_tests(len(own))}: " + ", ".join(parts)

    def lines(self) -> list[str]:
        """Итог по группам: заголовок группы с числом и тесты строками ``- …``."""
        groups: dict[str, list[str]] = {key: [] for key in RESULT_TITLES}
        for item in self.tests:
            key = self.group(item)
            groups[key].append(f"- {_who(str(item.get('file_id') or ''), str(item.get('full_name') or ''))}"
                               f" — {self._detail(item)}")
        lines = [f"Перезапуск локально (Maven): {_tests(len(self.tests))}, каждый до "
                 f"{self.attempts} раз."]
        for key, title in RESULT_TITLES.items():
            if groups[key]:
                lines += ["", f"{title} ({len(groups[key])}):", *groups[key]]
        return lines

    def _detail(self, item: dict[str, Any]) -> str:
        outcome = item.get("outcome")
        if outcome == "passed":
            text = f"прошёл на запуске {item.get('attempt')} из {self.attempts}"
            if item.get("surefire_flaky"):
                text += " (surefire повторял его сам)"
            return text + (f"; {item['changed']}" if item.get("changed") else "")
        if outcome == "reproduced":
            text = f"упал {item.get('runs')} из {item.get('runs')}"
            return text + (f" (дальше не проверен: {item['cut']})" if item.get("cut") else "")
        if outcome == "other":
            return f"«{item.get('message') or 'текст ошибки скрыт'}»"
        reason = str(item.get("reason") or "")
        errors = self.build_errors.get(str(item.get("module") or ""), [])
        if errors and reason == "сборка не прошла":
            reason += f": {errors[0]}"
        return reason


def render_result(view: RerunView, logs: Path, report: Path | None = None) -> str:
    """Итог для ответа ``rerun_done``: между маркерами, со ссылками на логи Maven и отчёт."""
    links = [f"Логи Maven: {logs}"]
    if report is not None:
        links.append(f"Полный разбор с итогом: {report}")
    return "\n".join([RESULT_BEGIN, *view.lines(), "", *links, RESULT_END])


def render_offer(plan: Plan, command: str) -> str:
    """Пункт «После отчёта»: предложить перезапуск кандидатов скилла одним вопросом."""
    numbers = ", ".join(dict.fromkeys(str(int(test.file_id)) for test in plan.tests))
    names = [test.full_name for test in plan.tests[:MAX_OFFER_NAMES]]
    if len(plan.tests) > len(names):
        names.append(f"и ещё {len(plan.tests) - len(names)}")
    return (
        f"- Похожи на нестабильные (флаки): {_tests(len(plan.tests))} в проблемах {numbers} "
        f"({'; '.join(names)}). Их можно перезапустить на этой машине (Maven, с настройками "
        f"проекта; каждый до {plan.attempts} раз, до ~{plan.estimate_minutes()} мин). После "
        "ответов о правках спроси пользователя отдельным вопросом, перезапустить ли их: назови "
        "число тестов, проблемы, время и что тесты пойдут на этой машине. Если в этом сеансе он "
        "уже ответил на этот вопрос или сам попросил перезапустить тесты, не спрашивай. Только "
        f"после явного «да» на этот вопрос выполни: {command} — дальше по SKILL.md, раздел 8."
    )


def describe(signals: Iterable[str]) -> str:
    return ", ".join(SIGNALS.get(signal, signal) for signal in signals)


def write_offer(files: JobFiles, picks: Iterable[tuple[str, int, list[str]]]) -> None:
    ws.write_json(files.offer, {"tests": [[file_id, test_id, signals]
                                          for file_id, test_id, signals in picks]})


def read_offer(files: JobFiles) -> list[tuple[str, int, list[str]]]:
    data = _read(files.offer) or {}
    return [(str(item[0]), int(item[1]), [str(code) for code in item[2]])
            for item in data.get("tests") or []]


def _skipped_lines(plan: Plan) -> list[str]:
    if not plan.skipped:
        return []
    shown = plan.skipped[:MAX_SHOWN_SKIPPED]
    lines = ["Запустить нельзя:",
             *(f"- {_who(item.file_id, item.name)} — {item.reason}" for item in shown)]
    if len(plan.skipped) > len(shown):
        lines.append(f"- … и ещё {_tests(len(plan.skipped) - len(shown))}")
    return lines


def _who(file_id: str, name: str) -> str:
    number = int(file_id) if file_id.isdigit() else file_id
    return f"Проблема {number}: {name}"


def _module(module: str) -> str:
    return f" (модуль {module})" if module else ""


def tests_phrase(count: int) -> str:
    """«1 тест», «2 теста», «5 тестов»."""
    return _tests(count)


def _tests(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} тест"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return f"{count} теста"
    return f"{count} тестов"
