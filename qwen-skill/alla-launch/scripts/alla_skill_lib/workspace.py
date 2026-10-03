"""Рабочая папка разбора: пути, ``run.json``, ``state.json``, ``.last_run``.

Весь прогресс хранится в файлах, поэтому разбор можно продолжить после
сжатия контекста агента или прерывания: ``next`` каждый раз заново смотрит
на диск.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPORTS_DIRNAME = "alla-reports"
LAST_RUN_FILE = ".last_run"
RESUME_WINDOW_HOURS = 24  # неоконченный разбор того же прогона продолжается, а не дублируется
RUN_SCHEMA = 2  # 2: сигнатуры, база знаний, история, предложения правок

logger = logging.getLogger(__name__)

SKILL_DIR = Path(__file__).resolve().parents[2]
ENTRYPOINT = SKILL_DIR / "scripts" / "alla_skill.py"


class RunNotFoundError(Exception):
    """Рабочая папка разбора не найдена или повреждена."""


def detect_project_root(skill_dir: Path | None = None) -> Path:
    """``<project>/.qwen/skills/<name>`` → ``<project>``, иначе текущая папка.

    Личный скилл (``~/.qwen/skills/<name>``) к проекту не привязан — тогда
    корнем считается текущая папка, куда Qwen Code запускает команды.
    """
    skill_dir = SKILL_DIR if skill_dir is None else skill_dir
    if skill_dir.parent.name == "skills" and skill_dir.parent.parent.name == ".qwen":
        candidate = skill_dir.parent.parent.parent.resolve()
        if candidate != Path.home().resolve():
            return candidate
    return Path.cwd().resolve()


def skill_command(*args: str) -> str:
    """Команда запуска скрипта скилла, которую агент может выполнить как есть."""
    parts = ["python" if os.name == "nt" else "python3", str(ENTRYPOINT), *args]
    if os.name == "nt":
        return " ".join(f'"{part}"' if " " in part else part for part in parts)
    return shlex.join(parts)


@dataclass(frozen=True)
class RunPaths:
    """Пути внутри папки одного разбора."""

    root: Path

    @property
    def run_json(self) -> Path:
        return self.root / "run.json"

    @property
    def state_json(self) -> Path:
        return self.root / "state.json"

    @property
    def clusters_dir(self) -> Path:
        return self.root / "clusters"

    @property
    def analyses_dir(self) -> Path:
        return self.root / "analyses"

    @property
    def summary_task(self) -> Path:
        return self.root / "summary_task.md"

    @property
    def summary(self) -> Path:
        return self.root / "summary.md"

    @property
    def report(self) -> Path:
        return self.root / "report.md"

    @property
    def reports_dir(self) -> Path:
        return self.root.parent

    def cluster_task(self, file_id: str) -> Path:
        return self.clusters_dir / f"{file_id}.md"

    def analysis(self, file_id: str) -> Path:
        return self.analyses_dir / f"{file_id}.md"

    def batch(self, number: int) -> Path:
        """Задание субагенту на пакет кластеров (создаётся при первом пакетном ``next``)."""
        return self.root / "batches" / f"{number}.md"

    def evidence(self, file_id: str) -> Path:
        return self.root / "evidence" / f"{file_id}.txt"

    def proposal(self, file_id: str) -> Path:
        return self.root / "proposals" / f"{file_id}.md"

    def proposal_record(self, file_id: str) -> Path:
        """Отметка, что предложение применено командой ``apply --yes``."""
        return self.root / "proposals" / f"{file_id}.applied.json"

    def proposal_backup(self, file_id: str) -> Path:
        """Файл до правки: из него ``revert`` возвращает исходную версию."""
        return self.root / "proposals" / f"{file_id}.orig"

    def proposal_patch(self, file_id: str) -> Path:
        """Показанный пользователю diff (unified, можно применить ``git apply``)."""
        return self.root / "proposals" / f"{file_id}.patch"

    def feedback(self, file_id: str) -> Path:
        return self.root / "feedback" / f"{file_id}.md"

    def next_command(self) -> str:
        return skill_command("next", str(self.root))


def create_run_dir(reports_dir: Path, launch_id: int, now: datetime) -> RunPaths:
    """Создать новую папку ``<launch_id>-<YYYYmmdd-HHMMSS>`` в ``reports_dir``."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    gitignore = reports_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# Отчёты alla-launch не коммитятся\n*\n", encoding="utf-8")

    base = f"{launch_id}-{now.strftime('%Y%m%d-%H%M%S')}"
    root = reports_dir / base
    suffix = 1
    while True:
        try:
            root.mkdir()  # без exist_ok: два prepare в одну секунду не должны делить папку
            break
        except FileExistsError:
            suffix += 1
            root = reports_dir / f"{base}-{suffix}"
    paths = RunPaths(root.resolve())
    paths.clusters_dir.mkdir()
    for name in ("analyses", "evidence", "proposals", "feedback"):
        (paths.root / name).mkdir()
    return paths


def find_unfinished_run(
    reports_dir: Path,
    launch_id: int,
    now: datetime | None = None,
) -> RunPaths | None:
    """Свежий разбор этого прогона без ``report.md`` — его стоит продолжить.

    Повторный ``prepare`` после таймаута или сжатия контекста иначе создаёт
    дубль и бросает уже сделанные разборы.
    """
    if not reports_dir.is_dir():
        return None
    limit = (now.timestamp() if now else time.time()) - RESUME_WINDOW_HOURS * 3600
    candidates: list[tuple[float, RunPaths]] = []
    for path in reports_dir.glob(f"{launch_id}-*"):
        paths = RunPaths(path.resolve())
        if not paths.run_json.is_file() or paths.report.exists():
            continue
        created = paths.run_json.stat().st_mtime
        if created >= limit:
            candidates.append((created, paths))
    # Пустой или оборванный ``run.json`` (сбой на записи) продолжить нельзя:
    # такая папка пропускается, берётся следующая свежая, иначе начнётся новый разбор.
    for _, paths in sorted(candidates, key=lambda item: item[0], reverse=True):
        try:
            read_run(paths)
        except RunNotFoundError as exc:
            logger.warning("Пропускаю неоконченный разбор: %s", exc)
            continue
        return paths
    return None


def remember_last_run(reports_dir: Path, paths: RunPaths) -> None:
    write_atomic(reports_dir / LAST_RUN_FILE, str(paths.root))


def resolve_run(run_dir: str | None, reports_dir: Path) -> RunPaths:
    """Найти папку разбора: явный путь или последняя из ``.last_run``."""
    if run_dir:
        root = Path(run_dir).expanduser().resolve()
    else:
        marker = reports_dir / LAST_RUN_FILE
        if not marker.is_file():
            raise RunNotFoundError(
                f"Нет последнего разбора в {reports_dir}. Сначала выполни: "
                f"{skill_command('prepare', '<launch_id>')}"
            )
        root = Path(marker.read_text(encoding="utf-8").strip())
    paths = RunPaths(root)
    if not paths.run_json.is_file():
        if not root.exists():
            raise RunNotFoundError(
                f"Папка разбора {root} удалена. Выполни: {skill_command('prepare', '<launch_id>')}"
            )
        raise RunNotFoundError(f"В {root} нет run.json — это не папка разбора alla-launch")
    read_run(paths)  # битый run.json — понятная ошибка, а не traceback в любой команде
    return paths


def read_run(paths: RunPaths) -> dict[str, Any]:
    """Прочитать ``run.json``; пустой, оборванный или чужой файл — ``RunNotFoundError``."""
    try:
        run = read_json(paths.run_json)
    except (OSError, ValueError) as exc:  # JSONDecodeError и UnicodeDecodeError — ValueError
        reason = "пуст" if _is_empty(paths.run_json) else f"не читается ({type(exc).__name__}: {exc})"
    else:
        if isinstance(run, dict) and isinstance(run.get("clusters"), list):
            return run
        reason = "не похож на разбор alla-launch"
    prefix = paths.root.name.partition("-")[0]
    launch = prefix if prefix.isdigit() else "<launch_id>"
    raise RunNotFoundError(
        f"run.json в {paths.root} {reason} — этот разбор не продолжить (скорее всего, запись "
        f"прервалась). Выполни: {skill_command('prepare', launch, '--fresh')}"
    )


def _is_empty(path: Path) -> bool:
    try:
        return not path.read_bytes().strip()
    except OSError:
        return False


def write_atomic(path: Path, text: str) -> None:
    """Записать текст (UTF-8, переводы строк как есть) атомарно — см. :func:`write_atomic_bytes`."""
    write_atomic_bytes(path, text.encode("utf-8"))


def write_atomic_bytes(path: Path, data: bytes, *, mode_from: Path | None = None) -> None:
    """Записать файл целиком через временный файл рядом (без обрыва посередине).

    ``mode_from`` — чьи права доступа перенести (исходник проекта при ``apply``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())  # иначе после сбоя ОС rename может пережить данные: файл на 0 байт
        if mode_from is not None:
            shutil.copymode(mode_from, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    """Атомарно записать JSON (через временный файл)."""
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path) -> str:
    """Файл, написанный моделью: BOM и «битые» байты не должны ронять скрипт."""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def configure_stdio() -> None:
    """UTF-8 для stdout/stderr (консоль Windows по умолчанию не UTF-8)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
