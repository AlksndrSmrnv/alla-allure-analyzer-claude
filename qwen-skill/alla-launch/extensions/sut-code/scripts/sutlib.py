"""Общий модуль расширения sut-code: копия кода тестируемого приложения (SUT) и её ревизии.

Скрипты действий (``prepare``, ``search``, ``files``, ``switch``, ``history``, ``compare``,
``check``) импортируют его как соседний файл: Python кладёт папку скрипта в ``sys.path``.
Код скилла не импортируется — всё нужное приходит в JSON-контексте (``ALLA_CONTEXT``).

Копия лежит внутри проекта, чтобы её мог читать субагент (правила сеанса скилла разрешают
только файлы проекта)::

    <проект>/.alla-sut/
      .gitignore                       — «*»: копия не попадает в git проекта
      <имя>-<хэш ссылки>/repo.git      — bare partial clone (--filter=blob:none)
      <имя>-<хэш ссылки>/trees/<sha12> — git worktree --detach на ревизию
      <имя>-<хэш ссылки>/lock          — блокировка: fetch и worktree не идут параллельно

Первый клон и создание дерева большой ревизии могут идти дольше, чем живёт команда в Qwen
(120 с), поэтому они идут фоновым процессом (``job``), а скрипт ждёт их не дольше
``WAIT_BUDGET``: не дождался — просит повторить запрос, а работа продолжается.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

SUT_DIRNAME = ".alla-sut"
GIT_TIMEOUT = 30
NETWORK_TIMEOUT = 60
WAIT_BUDGET = 70  # секунд ожидания фоновой работы за один вызов: Qwen обрывает команду на 120 с
FETCH_FRESH = 120  # секунд: свежую копию повторно не обновляем
JOB_STALE = 3 * 3600  # фоновая работа без признаков жизни дольше — считается умершей
CATEGORIES = ("приложение", "тест", "окружение", "данные", "неизвестно")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
SCP_URL_RE = re.compile(r"^[\w.+-]+@[\w.-]+:(?!//)[^\s]+$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\s]")


class SutError(Exception):
    """Действие невозможно; текст — для пользователя, по-русски."""


class SutPending(SutError):
    """Фоновая работа (клон, дерево ревизии) ещё идёт: повторить запрос позже."""


# ---------------------------------------------------------------------------
# контекст и вывод
# ---------------------------------------------------------------------------


def load_context() -> tuple[dict[str, Any], Path]:
    context_file = Path(os.environ["ALLA_CONTEXT"])
    return json.loads(context_file.read_text(encoding="utf-8")), context_file


def emit(status: str, lines: list[str]) -> int:
    print(f"STATUS: {status}")
    for line in lines:
        print(line)
    return 0 if status == "ok" else 1


def run_main(body: Any) -> int:
    """Обёртка действия: ошибки — строкой для пользователя, без traceback."""
    try:
        context, context_file = load_context()
        return emit("ok", body(context, context_file))
    except SutError as exc:
        return emit("error", [str(exc)])
    except (OSError, ValueError, KeyError) as exc:
        return emit("error", [f"Расширение sut-code не сработало: {type(exc).__name__}: {exc}"])


def action_args() -> str:
    """Аргументы действия после «--» одной строкой (кавычки уже сняты shell)."""
    return " ".join(sys.argv[1:]).strip()


# ---------------------------------------------------------------------------
# репозитории из настроек и запроса
# ---------------------------------------------------------------------------


@dataclass
class Repo:
    name: str
    url: str
    folder: Path

    @property
    def git_dir(self) -> Path:
        return self.folder / "repo.git"

    @property
    def trees(self) -> Path:
        return self.folder / "trees"

    def tree_path(self, sha: str) -> Path:
        return self.trees / sha[:12]


def cache_root(project_root: Path) -> Path:
    root = project_root / SUT_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    ignore = root / ".gitignore"
    if not ignore.is_file():
        ignore.write_text("# Копия кода тестируемого приложения (расширение sut-code alla-launch).\n*\n",
                          encoding="utf-8")
    return root


def check_url(url: str, allow_local: bool) -> None:
    """Только https/http/ssh и scp-вид ``git@host:path``; без пароля в ссылке и без
    транспорта ``ext::`` (он выполняет команды) и ведущего «-» (git принял бы его за опцию)."""
    if not url or CONTROL_RE.search(url) or url.startswith("-"):
        raise SutError(f"ссылка «{url}» не годится: пробелы, управляющие символы или «-» в начале")
    lowered = url.lower()
    if lowered.startswith(("https://", "http://", "ssh://")):
        rest = url.split("://", 1)[1]
        userinfo = rest.split("/", 1)[0].rpartition("@")[0]
        if ":" in userinfo:
            raise SutError(
                "в ссылке на репозиторий есть пароль или токен — убери его: доступ к git идёт через "
                "ssh-ключи или credential helper инженера, а настройки коммитятся"
            )
        if not rest.split("/", 1)[0].rpartition("@")[2]:
            raise SutError(f"в ссылке «{url}» нет адреса сервера")
        return
    if "::" in url:
        raise SutError(f"ссылка «{url}»: транспорт «::» не поддерживается")
    if SCP_URL_RE.fullmatch(url):
        return
    if allow_local and (Path(url).is_absolute() or lowered.startswith("file://")):
        return
    raise SutError(
        f"ссылка «{url}» не похожа на адрес git: нужен https://…, ssh://… или git@сервер:путь"
    )


def default_repo_name(url: str) -> str:
    tail = url.rstrip("/").replace(":", "/").rsplit("/", 1)[-1]
    tail = tail.removesuffix(".git").lower()
    name = re.sub(r"[^a-z0-9._-]+", "-", tail).strip("-._")[:40]
    return name or "app"


def parse_repo_spec(spec: str) -> tuple[str, str]:
    """``имя=ссылка`` или ``ссылка``; (имя, ссылка)."""
    spec = spec.strip()
    name, sep, url = spec.partition("=")
    if sep and REPO_NAME_RE.fullmatch(name) and "/" not in name and ":" not in name:
        return name, url.strip()
    return default_repo_name(spec), spec


def repos(context: dict[str, Any]) -> list[Repo]:
    """Репозитории запроса: настройка ``repos`` и разовые ``--param repo=…``."""
    settings = context["settings"]
    specs = [*settings.get("repos", []), *(context.get("params") or {}).get("repo", [])]
    if not specs:
        raise SutError(
            "Не знаю, где код приложения. Укажи ссылку на git прямо в запросе "
            "(/alla:code N <ссылка>) или постоянно — в alla-extensions/config.toml:\n"
            "[sut-code]\nrepos = [\"ssh://git@git.company/team/app.git\"]"
        )
    root = cache_root(Path(context["project_root"]))
    result: dict[str, Repo] = {}
    for spec in specs:
        name, url = parse_repo_spec(spec)
        check_url(url, bool(settings.get("allow_local")))
        if name in result:
            if result[name].url == url:
                continue
            raise SutError(
                f"два репозитория с именем «{name}» ({result[name].url} и {url}) — дай им разные "
                "имена: «имя=ссылка»"
            )
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
        result[name] = Repo(name, url, root / f"{name}-{digest}")
    return list(result.values())


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def git_env() -> dict[str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
           "GIT_ASKPASS": os.environ.get("GIT_ASKPASS", "echo")}
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env and not _git_config("core.sshCommand"):
        # Пароль ключа ssh спросить некому: без BatchMode ssh ждал бы ввода до таймаута.
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def _git_config(key: str) -> str:
    try:
        result = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True,
                                timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def safe_options(allow_local: bool) -> list[str]:
    return ["-c", "protocol.ext.allow=never",
            "-c", f"protocol.file.allow={'always' if allow_local else 'never'}"]


def git(
    cwd: Path,
    *args: str,
    timeout: int = GIT_TIMEOUT,
    check: bool = True,
    allow_local: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *safe_options(allow_local), "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=git_env(), timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise SutError(f"git {args[0]} не ответил за {timeout} с") from exc
    except OSError as exc:
        raise SutError(f"git не запускается: {exc}") from exc
    if check and result.returncode != 0:
        raise SutError(f"git {args[0]}: {tail(result.stderr) or f'код {result.returncode}'}")
    return result


def tail(text: str, lines: int = 2) -> str:
    rows = [row.strip() for row in text.strip().splitlines() if row.strip()]
    return " / ".join(rows[-lines:])


@contextlib.contextmanager
def locked(repo: Repo) -> Iterator[None]:
    """Fetch и worktree одного репозитория — по очереди (несколько субагентов, повтор)."""
    repo.folder.mkdir(parents=True, exist_ok=True)
    with open(repo.folder / "lock", "a+", encoding="utf-8") as handle:
        try:
            import fcntl
        except ImportError:  # Windows: без блокировки, повтор безопасен
            yield
            return
        deadline = time.monotonic() + WAIT_BUDGET
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise SutPending(
                        f"копией {repo.name} сейчас занят другой запрос — повтори через минуту"
                    ) from None
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# фоновая работа: первый клон и дерево ревизии
# ---------------------------------------------------------------------------


@dataclass
class Job:
    """Фоновая работа: шаги git по порядку; итог — файл ``done`` или ``failed``."""

    folder: Path  # .alla-sut/<repo>/jobs/<имя>
    steps: list[list[str]] = field(default_factory=list)
    on_fail: list[list[str]] = field(default_factory=list)
    format: str = ""  # как работа распаковывает дерево (``TREE_FORMAT``), пишется в spec.json

    @property
    def done(self) -> Path:
        return self.folder / "done"

    @property
    def failed(self) -> Path:
        return self.folder / "failed"

    @property
    def pid_file(self) -> Path:
        return self.folder / "pid"


def start_job(job: Job, allow_local: bool) -> subprocess.Popen[bytes]:
    if job.folder.exists():
        shutil.rmtree(job.folder)
    job.folder.mkdir(parents=True)
    spec = {"steps": job.steps, "on_fail": job.on_fail, "allow_local": allow_local,
            "format": job.format}
    (job.folder / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    log = open(job.folder / "log.txt", "wb")  # noqa: SIM115 — живёт дольше этого процесса
    kwargs: dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":
        kwargs = {"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
                  | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "job", str(job.folder)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=git_env(), **kwargs,
    )
    log.close()
    job.pid_file.write_text(f"{process.pid} {int(time.time())}", encoding="utf-8")
    return process


def job_format(job: Job) -> str | None:
    """Формат из spec.json работы; ``None`` — работа прежней версии или spec не читается."""
    try:
        spec = json.loads((job.folder / "spec.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = spec.get("format") if isinstance(spec, dict) else None
    return value if isinstance(value, str) and value else None


def job_alive(job: Job, process: subprocess.Popen[bytes] | None = None) -> bool:
    if process is not None:
        return process.poll() is None
    try:
        pid_text, started = job.pid_file.read_text(encoding="utf-8").split()
    except (OSError, ValueError):
        return False
    if time.time() - int(started) > JOB_STALE:
        return False
    if os.name == "nt":
        return True
    try:
        os.kill(int(pid_text), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_job(job: Job, deadline: float, process: subprocess.Popen[bytes] | None,
             what: str) -> None:
    """Ждать итога фоновой работы до ``deadline``; не дождались — ``SutPending``."""
    while True:
        if job.done.is_file():
            return
        if job.failed.is_file():
            reason = job.failed.read_text(encoding="utf-8", errors="replace").strip()
            shutil.rmtree(job.folder, ignore_errors=True)
            raise SutError(f"{what} не удалось: {reason}")
        if not job_alive(job, process):
            shutil.rmtree(job.folder, ignore_errors=True)
            raise SutError(f"{what} прервалось — повтори запрос, начнётся заново")
        if time.monotonic() > deadline:
            raise SutPending(f"{what} ещё идёт ({_size_note(job)}) — повтори запрос через пару минут")
        time.sleep(0.5)


def _size_note(job: Job) -> str:
    total = 0
    for path in job.folder.parent.parent.rglob("*"):
        with contextlib.suppress(OSError):
            if path.is_file():
                total += path.stat().st_size
    return f"скачано {total // (1024 * 1024)} МБ"


def run_job(folder: Path) -> int:
    """Тело фоновой работы (``python sutlib.py job <папка>``)."""
    job = Job(folder)
    spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    allow_local = bool(spec.get("allow_local"))
    for step in spec["steps"]:
        result = subprocess.run(["git", *safe_options(allow_local), *step],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", stdin=subprocess.DEVNULL)
        sys.stdout.write(result.stdout + result.stderr)
        if result.returncode != 0:
            for undo in spec.get("on_fail", []):
                subprocess.run(["git", *undo], capture_output=True, stdin=subprocess.DEVNULL)
            command = next((part for part in step if not part.startswith("-") and "/" not in part), "?")
            job.failed.write_text(tail(result.stderr) or f"git {command}: код {result.returncode}",
                                  encoding="utf-8")
            return 1
    job.done.write_text("ok", encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# копия: клон, обновление, ревизии, деревья
# ---------------------------------------------------------------------------


def ensure_clone(repo: Repo, deadline: float, allow_local: bool) -> bool:
    """Копия есть или скачана; True — только что скачана (обновлять не надо)."""
    if (repo.git_dir / "HEAD").is_file():
        return False
    job = Job(repo.folder / "jobs" / "clone")
    process = None
    if not (job.pid_file.is_file() and job_alive(job)) and not job.done.is_file():
        tmp = repo.folder / "repo.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        repo.folder.mkdir(parents=True, exist_ok=True)
        (repo.folder / "url").write_text(repo.url + "\n", encoding="utf-8")
        job.steps = [
            ["clone", "--quiet", "--bare", "--filter=blob:none", "--", repo.url, str(tmp)],
            # Ветки — как на сервере (refs/heads/*); деревья создаются с --detach, поэтому
            # fetch в refs/heads не мешает.
            ["-C", str(tmp), "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"],
        ]
        process = start_job(job, allow_local)
    wait_job(job, deadline, process, f"Первое скачивание копии {repo.name} ({repo.url})")
    tmp = repo.folder / "repo.tmp"
    if tmp.is_dir() and not repo.git_dir.exists():
        tmp.rename(repo.git_dir)
    shutil.rmtree(job.folder, ignore_errors=True)
    if not (repo.git_dir / "HEAD").is_file():
        raise SutError(f"скачивание копии {repo.name} закончилось без копии — повтори запрос")
    (repo.folder / "fetched").write_text(str(int(time.time())), encoding="utf-8")
    return True


def fetch(repo: Repo, allow_local: bool) -> str | None:
    """Обновить копию; ``None`` — обновлена, иначе предупреждение (работаем со старой)."""
    stamp = repo.folder / "fetched"
    with contextlib.suppress(OSError, ValueError):
        if time.time() - int(stamp.read_text(encoding="utf-8")) < FETCH_FRESH:
            return None
    result = git(repo.git_dir, "fetch", "--quiet", "--prune", "--tags", "origin",
                 timeout=NETWORK_TIMEOUT, check=False, allow_local=allow_local)
    if result.returncode != 0:
        when = datetime.fromtimestamp(stamp.stat().st_mtime).strftime("%d.%m %H:%M") \
            if stamp.is_file() else "?"
        return (f"копию {repo.name} обновить не удалось ({tail(result.stderr) or 'нет связи'}) — "
                f"смотрю на данные от {when}")
    stamp.write_text(str(int(time.time())), encoding="utf-8")
    return None


def default_branch(repo: Repo, settings: dict[str, Any]) -> str:
    configured = str(settings.get("default_branch") or "").strip()
    if configured:
        return configured
    result = git(repo.git_dir, "symbolic-ref", "--short", "HEAD", check=False)
    return result.stdout.strip() or "main"


def check_ref_value(value: str) -> None:
    if not value or value.startswith("-") or CONTROL_RE.search(value) or ".." in value:
        raise SutError(f"ревизия «{value}» не годится: ветка, тег или коммит без пробелов и «..»")


def resolve_ref(repo: Repo, value: str) -> tuple[str, str] | None:
    """Ветка, тег (``1.4.2`` → ``1.4.2`` или ``v1.4.2``) или коммит → (sha, что это)."""
    check_ref_value(value)
    value = value.removeprefix("origin/")
    candidates = [(f"refs/heads/{value}", f"ветка {value}"),
                  (f"refs/tags/{value}", f"тег {value}"),
                  (f"refs/tags/v{value}", f"тег v{value}")]
    for ref, what in candidates:
        result = git(repo.git_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                     check=False)
        if result.returncode == 0:
            return result.stdout.strip(), what
    if SHA_RE.fullmatch(value.lower()):
        result = git(repo.git_dir, "rev-parse", "--verify", "--quiet", f"{value}^{{commit}}",
                     check=False)
        if result.returncode == 0:
            return result.stdout.strip(), f"коммит {value[:12]}"
    return None


def similar_refs(repo: Repo, value: str, limit: int = 5) -> list[str]:
    """Ветки и теги, похожие на названную ревизию: подсказка, если такой нет."""
    result = git(repo.git_dir, "for-each-ref", "--format=%(refname:short)", "--sort=-creatordate",
                 "refs/heads", "refs/tags", check=False)
    names = result.stdout.split()
    key = re.sub(r"[^0-9a-z]", "", value.lower())
    found = [name for name in names if key and key[:3] in re.sub(r"[^0-9a-z]", "", name.lower())]
    return (found or names)[:limit]


def approximate(repo: Repo, branch: str, launch_created: str | None) -> tuple[str, str]:
    """Последний коммит основной ветки на момент запуска прогона (или сейчас)."""
    if launch_created:
        result = git(repo.git_dir, "rev-list", "-1", f"--before={launch_created}",
                     f"refs/heads/{branch}", check=False)
        sha = result.stdout.strip()
        if sha:
            return sha, f"приблизительно: ветка {branch} на момент запуска прогона ({launch_created})"
    result = git(repo.git_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}",
                 check=False)
    if result.returncode != 0:
        raise SutError(
            f"в {repo.name} нет основной ветки {branch} — укажи её: default_branch в "
            "[sut-code] файла alla-extensions/config.toml"
        )
    return result.stdout.strip(), f"приблизительно: последний коммит ветки {branch}"


# Как распаковано дерево — вторая строка ``.ready``. Дерево без неё распаковано до
# ``core.symlinks=false`` и может содержать настоящие ссылки наружу: оно не готово,
# ``ensure_tree`` распакует его заново, а ``load_state`` попросит повторить prepare.
TREE_FORMAT = "symlinks-as-files"


def tree_ready(path: Path) -> bool:
    """Дерево распаковано и распаковано безопасно (``TREE_FORMAT`` в отметке ``.ready``)."""
    try:
        lines = ready_mark(path).read_text(encoding="utf-8").split()
    except OSError:
        return False
    return path.is_dir() and TREE_FORMAT in lines[1:]


def ensure_tree(repo: Repo, sha: str, deadline: float, allow_local: bool) -> Path:
    """Дерево ревизии (``git worktree --detach``); большое дерево создаётся в фоне."""
    path = repo.tree_path(sha)
    ready = ready_mark(path)
    if tree_ready(path):
        os.utime(ready)
        return path
    ready.unlink(missing_ok=True)  # старое дерево: до распаковки заново оно не готово
    job = Job(repo.folder / "jobs" / f"tree-{sha[:12]}", format=TREE_FORMAT)
    what = f"Подготовка ревизии {sha[:8]} копии {repo.name}"
    if job.folder.exists() and job_format(job) != TREE_FORMAT:
        # Распаковка прежней версии (до core.symlinks=false; ensure_tree зовут под locked, так
        # что чужая новая работа здесь не появится): её дерево не годится. Идущую — дождаться
        # (вторая распаковка в ту же папку столкнулась бы с ней), затем распаковать заново.
        if not job.done.is_file() and job_alive(job):
            try:
                wait_job(job, deadline, None, what)
            except SutPending:
                raise
            except SutError:
                pass  # упала или прервалась — всё равно распаковываем заново
        shutil.rmtree(job.folder, ignore_errors=True)
    process = None
    if not (job.pid_file.is_file() and job_alive(job)) and not job.done.is_file():
        repo.trees.mkdir(parents=True, exist_ok=True)
        git(repo.git_dir, "worktree", "prune", check=False)
        if path.exists():
            git(repo.git_dir, "worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
        # Символические ссылки — обычными файлами с текстом ссылки: ссылка «Service.java →
        # ../../.env» иначе отдала бы субагенту и данным prepare файл вне ревизии.
        job.steps = [["-c", "core.symlinks=false", "-C", str(repo.git_dir), "worktree", "add",
                      "--quiet", "--detach", str(path), sha]]
        job.on_fail = [["-C", str(repo.git_dir), "worktree", "remove", "--force", str(path)]]
        process = start_job(job, allow_local)
    wait_job(job, deadline, process, what)
    shutil.rmtree(job.folder, ignore_errors=True)
    ready.write_text(f"{sha}\n{TREE_FORMAT}\n", encoding="utf-8")
    return path


# Каждое действие запроса отмечает своё дерево (``load_state``). Дерево, которым пользовались
# за этот срок, может читать субагент другого запроса — очистка его не трогает: ``keep_trees``
# — мягкий лимит, лишние деревья уйдут при следующей очистке.
TREE_BUSY_SECONDS = 60 * 60


def ready_mark(tree: Path) -> Path:
    return tree.with_name(tree.name + ".ready")


def cleanup_trees(repo: Repo, keep: int, in_use: set[Path]) -> None:
    """Оставить ``keep`` последних деревьев (по последнему использованию), те, что в работе у
    этого запроса, и те, которыми пользовались недавно (``TREE_BUSY_SECONDS``). Дерево прежней
    распаковки (``tree_ready`` — нет) удаляется всегда: в нём могут быть ссылки наружу, а путь к
    нему остался в старых данных субагента."""
    marks = sorted(repo.trees.glob("*.ready"), key=lambda mark: mark.stat().st_mtime, reverse=True)
    busy_since = time.time() - TREE_BUSY_SECONDS
    for index, mark in enumerate(marks):
        path = mark.with_suffix("")
        if path in in_use:
            continue
        if tree_ready(path) and (index < max(keep, 1) or mark.stat().st_mtime > busy_since):
            continue
        git(repo.git_dir, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
        mark.unlink(missing_ok=True)
    git(repo.git_dir, "worktree", "prune", check=False)


def commit_line(repo: Repo, sha: str) -> str:
    result = git(repo.git_dir, "log", "-1", "--format=%h %ad %s", "--date=short", sha, check=False)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# состояние запроса: какие деревья выбраны
# ---------------------------------------------------------------------------


def state_path(context_file: Path) -> Path:
    # Не .json: действия скилла принимают контекстом любой .json папки расширения.
    return context_file.with_name(f"{context_file.stem}.sut-state")


def data_path(context_file: Path) -> Path:
    return context_file.with_name(f"{context_file.stem}.sut.md")


def load_state(context_file: Path, *, need_trees: bool = True) -> dict[str, Any]:
    """Выбранные деревья запроса; каждое отмечается как используемое (``cleanup_trees``).

    Дерево удалено (очистка копии, ручное удаление) — ошибка «выполни prepare заново», а не
    чтение пустой папки; ``switch`` (``need_trees=False``) дерево создаёт сам.
    """
    path = state_path(context_file)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    if not isinstance(state, dict) or not state.get("trees"):
        raise SutError("ревизии ещё не выбраны — сначала выполни действие prepare из задания")
    for tree in state["trees"]:
        folder = Path(tree["tree"])
        if tree_ready(folder):
            with contextlib.suppress(OSError):
                os.utime(ready_mark(folder))
        elif need_trees:
            raise SutError(f"папки ревизии {tree['repo']} {str(tree['sha'])[:8]} уже нет или она "
                           "распакована прежней версией расширения — выполни действие prepare из "
                           "задания заново")
    return state


def save_state(context_file: Path, state: dict[str, Any]) -> None:
    state_path(context_file).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")


def tree_of(state: dict[str, Any], repo: str | None) -> dict[str, Any]:
    trees = state["trees"]
    if repo is None:
        if len(trees) == 1:
            return dict(trees[0])
        raise SutError(
            "репозиториев несколько — укажи, в каком: " + ", ".join(t["repo"] for t in trees)
            + " (путь — «репозиторий:путь», ревизия для switch — «репозиторий=ревизия»)"
        )
    for tree in trees:
        if tree["repo"] == repo:
            return dict(tree)
    raise SutError(f"репозитория «{repo}» в этом запросе нет; есть: "
                   + ", ".join(t["repo"] for t in trees))


def split_location(raw: str, state: dict[str, Any]) -> tuple[dict[str, Any], str, int | None]:
    """``[репозиторий:]путь[:строка]`` → (дерево, путь внутри дерева, строка)."""
    text = raw.strip().strip("«»\"'`")
    line: int | None = None
    match = re.fullmatch(r"(.+?):(\d+)", text)
    if match:
        text, line = match.group(1), int(match.group(2))
    repo, sep, path = text.partition(":")
    names = {tree["repo"] for tree in state["trees"]}
    if not sep or repo not in names:
        repo, path = "", text
    absolute = PurePosixPath(text.replace("\\", "/"))
    for item in state["trees"]:
        # Субагент может назвать файл полным путём из файла данных — это тоже место в ревизии.
        root = PurePosixPath(str(item["tree"]).replace("\\", "/"))
        if absolute.is_relative_to(root):
            repo, path = item["repo"], absolute.relative_to(root).as_posix()
            break
    tree = tree_of(state, repo or None)
    path = path.strip().lstrip("/")
    if not path:
        raise SutError("нужен путь к файлу внутри репозитория")
    resolved = (Path(tree["tree"]) / path).resolve()
    if not resolved.is_relative_to(Path(tree["tree"]).resolve()):
        raise SutError(f"путь {path} — вне репозитория")
    return tree, PurePosixPath(path).as_posix(), line


# ---------------------------------------------------------------------------
# кадры стека и тексты ошибок из данных проблемы
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    path: str  # путь или его хвост, как в кадре (через «/»)
    line: int
    text: str  # строка кадра для пояснения


_JAVA_FRAME = re.compile(
    r"\bat\s+(?:[\w.$-]+/)?(?P<cls>[\w.$<>]+)\.[\w$<>]+\((?P<file>[\w$-]+\.\w+):(?P<line>\d+)\)")
_PY_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')
_NET_FRAME = re.compile(r"\bin (?P<path>(?:[A-Za-z]:)?[^\s:]+\.\w+):line (?P<line>\d+)")
_PHP_FRAME = re.compile(r"(?P<path>[\w./\\-]+\.php)(?:\((?P<line>\d+)\)| on line (?P<line2>\d+))")
_PATH_FRAME = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[\w.@~+/\\-]*[\w-]\.(?:go|js|jsx|mjs|cjs|ts|tsx|rb|py|kt|kts|scala|"
    r"java|groovy|cs|php|rs|c|cc|cpp|h|hpp|swift|ex|exs|vue|svelte)):(?P<line>\d+)")
_LIBRARY = re.compile(
    r"^(?:java|javax|jdk|sun|com\.sun|kotlin|kotlinx|scala|org\.junit|org\.testng|org\.apache|"
    r"org\.springframework|io\.netty|reactor|okhttp3|retrofit2|com\.fasterxml)\.|"
    r"(?:site-packages|dist-packages|node_modules|/usr/lib/|/usr/local/lib/|<frozen |"
    r"/go/pkg/mod/|/vendor/|\.gradle/caches)")
MAX_FRAMES = 8


def frames(sources: list[dict[str, Any]]) -> list[Frame]:
    """Кадры стека всех языков из кусков «Данных» проблемы, по порядку, без повторов."""
    found: dict[tuple[str, int], Frame] = {}
    for source in sources:
        for raw_line in str(source.get("text", "")).splitlines():
            for frame in _frames_of_line(raw_line.strip()):
                if _LIBRARY.search(frame.path) or _LIBRARY.search(frame.text):
                    continue
                found.setdefault((frame.path, frame.line), frame)
    return list(found.values())


def _frames_of_line(line: str) -> Iterator[Frame]:
    match = _JAVA_FRAME.search(line)
    if match:
        package = match.group("cls").split("$", 1)[0].rsplit(".", 1)[0]
        prefix = package.replace(".", "/") + "/" if "." in match.group("cls") else ""
        if not _LIBRARY.search(match.group("cls")):
            yield Frame(prefix + match.group("file"), int(match.group("line")), line)
        return
    for regex in (_PY_FRAME, _NET_FRAME):
        match = regex.search(line)
        if match:
            yield Frame(_norm(match.group("path")), int(match.group("line")), line)
            return
    match = _PHP_FRAME.search(line)
    if match:
        number = match.group("line") or match.group("line2")
        yield Frame(_norm(match.group("path")), int(number), line)
        return
    for match in _PATH_FRAME.finditer(line):
        yield Frame(_norm(match.group("path")), int(match.group("line")), line)


def _norm(path: str) -> str:
    return path.replace("\\", "/").removeprefix("file://")


def tracked_files(tree: Path) -> list[str]:
    result = git(tree, "ls-files", "-z", check=False)
    return [name for name in result.stdout.split("\0") if name]


def match_frame(frame: Frame, files_by_name: dict[str, list[str]]) -> str | None:
    """Файл дерева для кадра: то же имя и самый длинный общий хвост пути."""
    parts = [part for part in frame.path.split("/") if part and part not in (".", "..")]
    if not parts:
        return None
    best: tuple[int, str] | None = None
    for candidate in files_by_name.get(parts[-1], []):
        cand = candidate.split("/")
        common = 0
        while common < min(len(cand), len(parts)) and cand[-1 - common] == parts[-1 - common]:
            common += 1
        if best is None or common > best[0] or (common == best[0] and len(candidate) < len(best[1])):
            best = (common, candidate)
    return best[1] if best else None


_EXCEPTION_MESSAGE = re.compile(r"(?:\w+(?:Exception|Error|Fault|Panic)|panic):\s*(?P<msg>.{6,})")
_LEVEL_MESSAGE = re.compile(
    r"\b(?:ERROR|FATAL|SEVERE|CRITICAL|CRIT|ALERT|EMERG|ERR)\b[\]\s:|-]*(?:[\w.$\[\]-]+\s*[:-]\s+)?"
    r"(?P<msg>.{6,})")
_VARIABLE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|\d[\d.:,_-]*|'[^']*'|\"[^\"]*\"|<[^>]*>|\[[^\]]*\]|"
    r"\{[^}]*\}|\S+@\S+|https?://\S+")
MAX_MESSAGES = 6


def messages(sources: list[dict[str, Any]]) -> list[str]:
    """Постоянные куски текстов ошибок (без чисел, id, строк в кавычках) — их ищем в коде."""
    found: list[str] = []
    for source in sources:
        if source.get("kind") == "trace":
            continue
        for raw_line in str(source.get("text", "")).splitlines():
            match = _EXCEPTION_MESSAGE.search(raw_line) or _LEVEL_MESSAGE.search(raw_line)
            if not match:
                continue
            pieces = [piece.strip(" .,:;-=()") for piece in _VARIABLE.split(match.group("msg"))]
            piece = max(pieces, key=len, default="")
            if len(piece) >= 10 and len(re.findall(r"[^\W\d_]", piece)) >= 6 and piece not in found:
                found.append(piece)
            if len(found) >= MAX_MESSAGES:
                return found
    return found


def grep(tree: Path, text: str, *, limit: int, ignore_case: bool = False) -> list[tuple[str, int, str]]:
    """Строки дерева с текстом (``git grep -F``); (путь, строка, текст строки)."""
    args = ["grep", "-n", "-I", "-F", "--no-color", "--max-count=3"]
    if ignore_case:
        args.append("-i")
    result = git(tree, *args, "-e", text, "--", ".", check=False)
    rows: list[tuple[str, int, str]] = []
    for row in result.stdout.splitlines():
        path, _, rest = row.partition(":")
        number, _, content = rest.partition(":")
        if number.isdigit():
            rows.append((path, int(number), content.strip()[:200]))
        if len(rows) >= limit:
            break
    return rows


def window(root: Path, path: str, line: int, radius: int = 8) -> list[str]:
    """Строки файла ревизии вокруг ``line`` с номерами; ``>`` — сама строка.

    Файл вне папки ревизии (ссылка из дерева, созданного до ``core.symlinks=false``) не
    читается.
    """
    target = root / path
    try:
        if not target.resolve().is_relative_to(root.resolve()):
            return []
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    start, end = max(1, line - radius), min(len(lines), line + radius)
    return [f"{number:>5} {'>' if number == line else '|'} {lines[number - 1][:200]}"
            for number in range(start, end + 1)]


if __name__ == "__main__":
    if sys.argv[1:2] == ["job"]:
        sys.exit(run_job(Path(sys.argv[2])))
    sys.exit(2)
