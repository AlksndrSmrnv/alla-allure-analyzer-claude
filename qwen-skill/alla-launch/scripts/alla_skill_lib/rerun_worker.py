"""Фоновый процесс перезапуска тестов: Maven по плану ``rerun/plan.json``, итог — ``result.json``.

Запускает его ``rerun.start_job`` отдельной сессией (``python -m alla_skill_lib.rerun_worker
<папка rerun>``): процесс переживает команду скилла, которую Qwen обрывает через 120 с.
Сбой самого процесса — файл ``failed`` с причиной, а не ``result.json``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.rerun import (
    KILL_GRACE_S,
    NOT_IN_REACTOR,
    TESTS_SKIPPED,
    JobFiles,
    MavenGroup,
    PlannedTest,
    build_errors,
    hold_lock,
    maven_command,
    method_result,
    read_reports,
    repeats_ci,
    report_snapshot,
    safe_text,
    source_digest,
    worker_env,
)

TIMEOUT_REASON = "вышел потолок времени ALLURE_RERUN_TIMEOUT"
BUILD_REASON = "сборка не прошла"
EDITED_REASON = "код теста изменён во время перезапуска"
SKIPPED_BY_BUILD_REASON = (
    "Maven пропустил тесты (Tests are skipped): пропуск задан в pom.xml или профиле — "
    "нужное свойство или профиль передайте через ALLURE_RERUN_MAVEN_ARGS")


def main(argv: list[str]) -> int:
    files = JobFiles(Path(argv[0]))
    try:
        plan = json.loads(files.plan.read_text(encoding="utf-8"))
        with hold_lock(Path(plan["lock"])) as held:
            if not held:
                _fail(files, "в проекте уже идёт другой перезапуск тестов — дождись его итога")
                return 1
            result = execute(files, plan)
        ws.write_json(files.result, result)
        return 0
    except Exception as exc:  # итог нужен и при сбое: иначе rerun ждал бы до потолка времени
        _fail(files, f"сбой перезапуска: {type(exc).__name__}: {exc}")
        raise


def execute(files: JobFiles, plan: dict[str, Any]) -> dict[str, Any]:
    """Запуски по модулям, до ``attempts`` раз; в следующий — только упавшие с той же ошибкой."""
    tests = [PlannedTest(**item) for item in plan["tests"]]
    root = Path(plan["project_root"])
    groups = [MavenGroup(**item) for item in plan["groups"]]
    attempts = int(plan["attempts"])
    extra = [str(arg) for arg in plan["maven_args"]]
    deadline = time.monotonic() + int(plan["timeout"])
    outcomes: dict[int, dict[str, Any]] = {}
    runs: Counter[int] = Counter()
    build: dict[str, list[str]] = {}
    fallback: set[str] = set()
    pending = list(tests)
    timed_out = False

    def stop(test: PlannedTest, reason: str) -> None:
        """Тест дальше не проверен: уже воспроизводился — так и есть, но не все запуски."""
        if runs[test.test_result_id]:
            outcomes[test.test_result_id] = _outcome(test, "reproduced", runs, cut=reason)
        else:
            outcomes[test.test_result_id] = _outcome(test, "not_run", runs, reason=reason)

    for attempt in range(1, attempts + 1):
        still: list[PlannedTest] = []
        for group in groups:
            batch = [test for test in pending if test.module == group.module]
            if not batch:
                continue
            if timed_out or deadline - time.monotonic() <= 0:
                timed_out = True
                still += batch
                continue
            _progress(files, plan, attempt, group.module, outcomes)
            suffix = f"alla-{plan['job']}-{attempt}"
            log = files.maven_log(attempt, group.module)
            before = report_snapshot(Path(group.fallback_cwd or group.cwd))
            use_fallback = group.module in fallback
            code, killed = _maven(group, batch, suffix, extra, log, deadline, use_fallback)
            if code and not killed and not use_fallback and group.fallback_cwd and _says(log, NOT_IN_REACTOR):
                # Модуля нет в <modules> реактора — запускаем его как отдельный проект.
                fallback.add(group.module)
                code, killed = _maven(group, batch, suffix, extra, log, deadline, True)
            timed_out = timed_out or killed
            if code and not killed:
                build[group.module] = build_errors(log)
            cases = read_reports(Path(group.fallback_cwd or group.cwd), suffix, before)
            for test in batch:
                found = method_result(cases, test)
                if found is None:
                    stop(test, _missing_reason(killed, code, log))
                    continue
                if found.status == "skipped":
                    stop(test, "пропущен (skipped)")
                    continue
                runs[test.test_result_id] += 1
                if found.status == "passed":
                    edited = source_digest(root, test.source) != test.source_sha
                    outcomes[test.test_result_id] = _outcome(
                        test, "passed", runs, attempt=attempt, surefire_flaky=found.flaky,
                        **({"changed": test.changed or EDITED_REASON} if edited else {}))
                elif not repeats_ci(found.message, test):
                    outcomes[test.test_result_id] = _outcome(
                        test, "other", runs, attempt=attempt, message=safe_text(found.message))
                else:
                    still.append(test)
        pending = still
        if not pending or timed_out:
            break
    for test in pending:  # каждый раз падал с той же ошибкой — или время вышло раньше
        if timed_out:
            stop(test, TIMEOUT_REASON)
        else:
            outcomes[test.test_result_id] = _outcome(test, "reproduced", runs)
    return {
        "job": plan["job"],
        "finished": time.time(),
        "attempts": attempts,
        "timed_out": timed_out,
        "tests": [outcomes[test.test_result_id] for test in tests],
        "build_errors": build,
    }


def _outcome(test: PlannedTest, outcome: str, runs: Counter[int], **extra: Any) -> dict[str, Any]:
    return {
        "test_result_id": test.test_result_id,
        "file_id": test.file_id,
        "full_name": test.full_name,
        "module": test.module,
        "changed": test.changed,
        "outcome": outcome,
        "runs": runs[test.test_result_id],
        **extra,
    }


def _maven(
    group: MavenGroup,
    tests: list[PlannedTest],
    suffix: str,
    extra: list[str],
    log: Path,
    deadline: float,
    fallback: bool,
) -> tuple[int, bool]:
    """Код выхода Maven и «обрезан по времени»; вывод — в ``log``."""
    command = maven_command(group, tests, suffix, extra, fallback=fallback)
    cwd = group.fallback_cwd if fallback else group.cwd
    shown = command[: len(command) - len(extra)]
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        header = f"$ cd {shlex.quote(cwd)} && {shlex.join(shown)}"
        out.write((header + (" <доп. аргументы>" if extra else "") + "\n").encode("utf-8"))
        out.flush()
        try:
            process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                env=worker_env(), start_new_session=True,
            )
        except OSError as exc:
            out.write(f"[ERROR] Maven не запускается: {exc}\n".encode())
            return 127, False
        try:
            return process.wait(timeout=max(1.0, deadline - time.monotonic())), False
        except subprocess.TimeoutExpired:
            _kill(process)
            out.write("[ERROR] Maven остановлен: вышел потолок времени ALLURE_RERUN_TIMEOUT\n"
                      .encode())
            return -1, True


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Остановить Maven со всей группой (форк surefire): сначала мягко, потом жёстко.

    Ждём не только Maven: он может выйти по SIGTERM раньше форка surefire, который сигнал
    игнорирует, — оставшимся в группе после паузы уходит SIGKILL.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < deadline and _group_alive(process):
        time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=KILL_GRACE_S)


def _group_alive(process: subprocess.Popen[bytes]) -> bool:
    process.poll()  # Maven-зомби держал бы группу «живой»
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _missing_reason(killed: bool, code: int, log: Path) -> str:
    """Почему теста нет в отчётах этого запуска."""
    if killed:
        return TIMEOUT_REASON
    if code:
        return BUILD_REASON
    if _says(log, TESTS_SKIPPED):
        return SKIPPED_BY_BUILD_REASON
    return "нет в отчётах surefire"


def _says(log: Path, text: str) -> bool:
    try:
        return text in log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _progress(
    files: JobFiles, plan: dict[str, Any], attempt: int, module: str,
    outcomes: dict[int, dict[str, Any]],
) -> None:
    ws.write_json(files.progress, {
        "job": plan["job"],
        "attempt": attempt,
        "attempts": plan["attempts"],
        "module": module,
        "updated": time.time(),
        "done": len(outcomes),
        "passed": sum(1 for item in outcomes.values() if item["outcome"] == "passed"),
        "total": len(plan["tests"]),
    })


def _fail(files: JobFiles, reason: str) -> None:
    files.failed.write_text(reason + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
