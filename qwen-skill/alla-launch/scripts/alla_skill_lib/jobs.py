"""Команда скрипта как фоновое задание: Qwen Code обрывает команду shell через 120 с.

``prepare`` большого прогона выгружает данные из TestOps минуты, ``review`` ждёт оценку
моделью. Такая команда запускает саму себя отдельным процессом (``python -m
alla_skill_lib.jobs <папка> <аргументы>``, новая сессия: Qwen её не оборвёт) и ждёт итога не
дольше ``WAIT_BUDGET``. Не дождалась — отвечает ``STATUS: running``, и повтор той же команды
подключается к идущему заданию, а не начинает заново. Итог — вывод дочерней команды как есть.

Задания одной команды — ``<папка отчётов>/.jobs/<ключ>/``: ``start.lock``, ``current`` (имя
папки текущего запуска) и папка на каждый запуск (``<время>-<id>``): ``pid`` (pid и время),
``output.txt`` (stdout команды), ``exit`` (код, пишется последним), ``progress.txt`` (стадия),
``worker.log`` (stderr), ``delivered`` (итог уже показан). Новый запуск не трогает папку
прежнего: вызов, который ещё ждал прежний, заберёт его итог.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from alla_skill_lib import workspace as ws

WAIT_BUDGET = 75.0  # секунд на один ответ: Qwen обрывает команду через 120 с
CEILING_S = 3600  # задание дольше — процесс уже не наш или завис
KEEP_S = 86400  # папки прежних запусков старше суток удаляет следующий запуск
JOBS_DIRNAME = ".jobs"
POLL_S = 0.5


@dataclass(frozen=True)
class JobFiles:
    root: Path

    @property
    def pid(self) -> Path:
        return self.root / "pid"

    @property
    def output(self) -> Path:
        return self.root / "output.txt"

    @property
    def exit(self) -> Path:
        return self.root / "exit"

    @property
    def progress(self) -> Path:
        return self.root / "progress.txt"

    @property
    def log(self) -> Path:
        return self.root / "worker.log"

    @property
    def delivered(self) -> Path:
        return self.root / "delivered"


@dataclass(frozen=True)
class JobSlot:
    """Задания одной команды (``prepare-<id>``, ``review-<разбор>``): текущий запуск и замок."""

    root: Path

    def current(self) -> JobFiles | None:
        """Текущий запуск; ``None`` — запусков не было."""
        try:
            name = (self.root / "current").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return JobFiles(self.root / name) if name and (self.root / name).is_dir() else None


def job_slot(reports_dir: Path, key: str) -> JobSlot:
    ws.ensure_reports_dir(reports_dir)
    return JobSlot(reports_dir / JOBS_DIRNAME / key)


@dataclass(frozen=True)
class JobState:
    """``none`` — задания нет, ``running``, ``done``, ``lost`` — процесс завершился без итога.

    ``delivered`` — итог законченного задания уже показан: новый вызов команды начинает своё,
    а вызов, который ждал это задание, всё равно получает его итог (ждать могут двое).
    """

    state: str
    started: float | None = None
    progress: str = ""
    delivered: bool = False

    @property
    def active(self) -> bool:
        """Новый вызов подключается к заданию, а не начинает своё."""
        return self.state == "running" or (self.state in ("done", "lost") and not self.delivered)


def job_state(files: JobFiles) -> JobState:
    started = _started(files)
    delivered = files.delivered.is_file()
    if files.exit.is_file():
        return JobState("done", started, delivered=delivered)
    if started is None:
        return JobState("none")
    progress = _read(files.progress)
    if _alive(files, started):
        return JobState("running", started, progress)
    return JobState("lost", started, progress, delivered=delivered)


@contextlib.contextmanager
def start_lock(slot: JobSlot) -> Iterator[None]:
    """Решение «подключиться или запустить» — по одному: два одновременных вызова не
    запускают два процесса."""
    import fcntl

    slot.root.mkdir(parents=True, exist_ok=True)
    with open(slot.root / "start.lock", "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def start(slot: JobSlot, args: list[str]) -> JobFiles:
    """Запустить команду скрипта ``args`` фоновым процессом (под :func:`start_lock`); папка
    запуска — новая, она же становится текущей.

    Рабочая папка — та же, что у команды: относительные пути окружения (``SSL_CERT_FILE``
    сертификата прокси) должны значить то же самое. Модуль — через ``PYTHONPATH``, а ``-P`` не
    ставит рабочую папку в начало ``sys.path``: файл проекта ``json.py`` не подменит модуль.
    """
    _drop_old(slot)
    files = JobFiles(slot.root / f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}")
    files.root.mkdir(parents=True)
    scripts = str(Path(__file__).resolve().parents[1])
    path = os.pathsep.join(part for part in (scripts, os.environ.get("PYTHONPATH")) if part)
    with open(files.output, "wb") as out, open(files.log, "wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-P", "-m", "alla_skill_lib.jobs", str(files.root), *args],
            stdin=subprocess.DEVNULL, stdout=out, stderr=log,
            env={**os.environ, "PYTHONPATH": path, "PYTHONIOENCODING": "utf-8"},
            start_new_session=True,
        )
    files.pid.write_text(f"{process.pid} {time.time():.0f}", encoding="utf-8")
    ws.write_atomic(slot.root / "current", files.root.name)
    return files


def _drop_old(slot: JobSlot) -> None:
    """Папки прежних запусков старше суток (не текущую): их итог давно никто не ждёт."""
    current = slot.current()
    limit = time.time() - KEEP_S
    for folder in slot.root.iterdir() if slot.root.is_dir() else ():
        if not folder.is_dir() or (current is not None and folder == current.root):
            continue
        with contextlib.suppress(OSError):
            if folder.stat().st_mtime < limit:
                shutil.rmtree(folder, ignore_errors=True)


def wait(files: JobFiles, budget: float) -> JobState:
    """Ждать итога до ``budget`` секунд; состояние на момент выхода."""
    deadline = time.monotonic() + budget
    while True:
        state = job_state(files)
        if state.state != "running" or time.monotonic() >= deadline:
            return state
        time.sleep(POLL_S)


def deliver(files: JobFiles) -> tuple[str, int]:
    """Вывод и код законченной команды; итог отмечается показанным."""
    output = files.output.read_text(encoding="utf-8", errors="replace")
    try:
        code = int(files.exit.read_text(encoding="utf-8").strip() or 1)
    except (OSError, ValueError):
        code = 1
    mark_delivered(files)
    return output, code


def mark_delivered(files: JobFiles) -> None:
    files.delivered.write_text("", encoding="utf-8")


def minutes(state: JobState) -> int:
    return max(0, round((time.time() - (state.started or time.time())) / 60))


def note_progress(job_dir: Path | None, message: str) -> None:
    """Стадия команды — для ответа ``running`` (``job_dir`` — папка задания или ``None``)."""
    if job_dir is not None:
        with contextlib.suppress(OSError):
            ws.write_atomic(JobFiles(job_dir).progress, message)


def _started(files: JobFiles) -> float | None:
    try:
        _pid, started = files.pid.read_text(encoding="utf-8").split()
        return float(started)
    except (OSError, ValueError):
        return None


def _alive(files: JobFiles, started: float) -> bool:
    if time.time() - started > CEILING_S:
        return False
    try:
        pid = int(files.pid.read_text(encoding="utf-8").split()[0])
        os.kill(pid, 0)
    except (OSError, ValueError, IndexError) as exc:
        return isinstance(exc, PermissionError)
    # Завершившийся дочерний процесс этой же команды — зомби до wait: kill его не отличит.
    with contextlib.suppress(ChildProcessError, OSError):
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    return True


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _run_child(job_dir: Path, args: list[str]) -> int:
    """Дочерний процесс: команда скрипта с выводом в файлы задания; код — в ``exit`` последним."""
    code = 1
    try:
        from alla_skill_lib import cli

        code = cli.main([*args, "--job", str(job_dir)])
    except SystemExit as exc:  # ошибка аргументов (argparse)
        code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout.flush()
        ws.write_atomic(JobFiles(job_dir).exit, str(code))
    return code


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m alla_skill_lib.jobs <job_dir> <аргументы команды>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(_run_child(Path(sys.argv[1]), sys.argv[2:]))
