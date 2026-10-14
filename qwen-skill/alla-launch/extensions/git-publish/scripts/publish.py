"""Действие ``publish`` расширения git-publish: применённая правка → новая ветка на remote.

Рабочая копия, индекс и текущая ветка инженера не меняются: коммит собирается во
временном индексе git (``GIT_INDEX_FILE``) от базового коммита, а в него попадает ровно
правка из отметки ``apply`` — чужие незакоммиченные изменения того же файла и других
файлов в коммит не уходят. Ветка создаётся локально без checkout и пушится на remote.

Запускает скилл (``alla_skill.py extensions action git-publish publish --context …``):
контекст события ``after_apply`` — в ``ALLA_CONTEXT``. Сообщение коммита — файл
``<stem>.message.txt`` рядом с контекстом (его пишет субагент); результат —
``<stem>.result.json``, по нему повтор не создаёт вторую ветку, а хук ``after_revert``
напоминает о ветке на сервере.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

GIT_TIMEOUT = 30
NETWORK_TIMEOUT = 90
MAX_BRANCH_SUFFIX = 20
MESSAGE_LIMIT = 5000
URL_RE = re.compile(r"https?://\S+")


class PublishError(Exception):
    """Выгрузка невозможна; текст — для пользователя."""


def git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = GIT_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=data,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"git {args[0]} не ответил за {timeout} с") from exc
    except OSError as exc:
        raise PublishError(f"git не запускается: {exc}") from exc
    if check and result.returncode != 0:
        raise PublishError(f"git {args[0]}: {_tail(result.stderr) or f'код {result.returncode}'}")
    return result


def out(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


def _tail(stderr: bytes, lines: int = 3) -> str:
    text = stderr.decode("utf-8", "replace").strip().splitlines()
    return " / ".join(line.strip() for line in text[-lines:])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def publish(context: dict[str, Any], context_file: Path) -> list[str]:
    problem = context.get("problem") or {}
    record = problem.get("record")
    if not isinstance(record, dict):
        raise PublishError("правка не применялась командой apply — выгружать нечего")
    settings = context["settings"]
    project = Path(context["project_root"])
    remote = str(settings["remote"])
    stem = context_file.stem
    result_file = context_file.with_name(f"{stem}.result.json")
    message_file = context_file.with_name(f"{stem}.message.txt")

    top = Path(out(git(project, "rev-parse", "--show-toplevel"))).resolve()
    target = (project / str(record["file"])).resolve()
    if not target.is_relative_to(top):
        raise PublishError(f"{record['file']} вне репозитория git {top}")
    rel = target.relative_to(top).as_posix()
    current = target.read_bytes()
    if sha256(current) != record["sha_after"]:
        raise PublishError(
            f"{record['file']} менялся после apply — в ветку ушли бы чужие правки. "
            "Выгрузи правку вручную."
        )
    if git(top, "remote", "get-url", remote, check=False).returncode != 0:
        raise PublishError(
            f"в репозитории нет remote «{remote}». Укажи нужный в "
            "alla-extensions/config.toml: [git-publish] remote = \"…\""
        )

    previous = _load(result_file)
    if previous.get("sha_after") == record["sha_after"] and previous.get("branch"):
        branch, commit = str(previous["branch"]), str(previous["commit"])
        if previous.get("pushed"):
            return [f"Эта правка уже выгружена в ветку {branch} (коммит {commit[:8]}) на {remote}."]
        if out(git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)) != commit:
            raise PublishError(
                f"локальная ветка {branch} от прошлой попытки изменилась — выгрузи правку вручную"
            )
    else:
        base = out(git(top, "rev-parse", "--verify", f"{settings['base']}^{{commit}}"))
        built = build_commit(top, project, base, rel, current, record, problem, _message(
            message_file, context
        ))
        if built is None:
            return [f"Правка уже есть в {settings['base']} — выгружать нечего."]
        commit = built
        branch = free_branch(top, remote, _branch_name(settings, context, problem))
        git(top, "branch", branch, commit)
        _save(result_file, {"branch": branch, "commit": commit, "remote": remote,
                            "sha_after": record["sha_after"], "pushed": False, "url": None})

    push = git(top, "push", "-u", remote, f"refs/heads/{branch}:refs/heads/{branch}",
               timeout=NETWORK_TIMEOUT, check=False)
    if push.returncode != 0:
        raise PublishError(
            f"push в {remote} не прошёл ({_tail(push.stderr)}). Коммит {commit[:8]} лежит в "
            f"локальной ветке {branch}; повторить выгрузку можно той же командой."
        )
    url = _merge_request_url(push.stderr)
    _save(result_file, {"branch": branch, "commit": commit, "remote": remote,
                        "sha_after": record["sha_after"], "pushed": True, "url": url})
    current_branch = out(git(top, "rev-parse", "--abbrev-ref", "HEAD", check=False)) or "?"
    lines = [f"Правка выгружена в новую ветку {branch} на {remote} (коммит {commit[:8]})."]
    if url:
        lines.append(f"Создать merge request: {url}")
    lines.append(
        f"Локально ничего не изменилось: правка осталась незакоммиченной в ветке {current_branch}."
    )
    return lines


def build_commit(
    top: Path,
    project: Path,
    base: str,
    rel: str,
    current: bytes,
    record: dict[str, Any],
    problem: dict[str, Any],
    message: str,
) -> str | None:
    """Коммит «база + правка» во временном индексе; None — правка в базе уже есть."""
    listed = out(git(top, "ls-tree", base, "--", rel))
    if not listed:
        raise PublishError(f"файла {rel} нет в базовом коммите — выгрузи правку вручную")
    mode, _, blob = listed.split(maxsplit=3)[:3]
    base_bytes = git(top, "cat-file", "blob", blob).stdout
    with tempfile.TemporaryDirectory(prefix="alla-git-publish-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(top, "read-tree", base, env=env)
        if sha256(base_bytes) == record["sha_before"]:
            # В базе — ровно версия до правки: файл после apply и есть «база + правка», байт в байт.
            new_blob = out(git(top, "hash-object", "-w", "--stdin", data=current))
            git(top, "update-index", "--cacheinfo", f"{mode},{new_blob},{rel}", env=env)
        else:
            # В базе файл другой (незакоммиченные правки инженера, другая база, autocrlf):
            # кладём только показанный пользователю diff.
            args = ["apply", "--cached", "--whitespace=nowarn"]
            prefix = project.resolve().relative_to(top).as_posix()
            if prefix != ".":
                args.append(f"--directory={prefix}")
            applied = git(top, *args, str(problem["patch_path"]), env=env, check=False)
            if applied.returncode != 0:
                raise PublishError(
                    f"в базовом коммите {rel} отличается от версии до правки, и правка на него "
                    f"не ложится ({_tail(applied.stderr, 1)}). Выгрузи правку вручную."
                )
        tree = out(git(top, "write-tree", env=env))
    if tree == out(git(top, "rev-parse", f"{base}^{{tree}}")):
        return None
    committed = git(top, "commit-tree", tree, "-p", base, "-F", "-", data=message.encode("utf-8"),
                    check=False)
    if committed.returncode != 0:
        stderr = committed.stderr.decode("utf-8", "replace")
        if "user.email" in stderr or "tell me who you are" in stderr.lower():
            raise PublishError(
                "git не знает автора коммита. Пользователю нужно задать git config user.name "
                "и user.email"
            )
        raise PublishError(f"git commit-tree: {_tail(committed.stderr)}")
    return out(committed)


def free_branch(top: Path, remote: str, wanted: str) -> str:
    """Имя ветки, которой нет ни локально, ни на remote: ``wanted``, ``wanted-2``…"""
    for number in range(1, MAX_BRANCH_SUFFIX + 1):
        name = wanted if number == 1 else f"{wanted}-{number}"
        if git(top, "check-ref-format", "--branch", name, check=False).returncode != 0:
            raise PublishError(f"«{name}» — недопустимое имя ветки; поправь branch_prefix")
        local = git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        if local.returncode == 0:
            continue
        remote_heads = git(top, "ls-remote", "--heads", remote, f"refs/heads/{name}",
                           timeout=NETWORK_TIMEOUT, check=False)
        if remote_heads.returncode != 0:
            raise PublishError(f"нет связи с {remote}: {_tail(remote_heads.stderr)}")
        if not out(remote_heads):
            return name
    raise PublishError(f"ветки {wanted}…-{MAX_BRANCH_SUFFIX} уже заняты — выгрузи правку вручную")


def _branch_name(settings: dict[str, Any], context: dict[str, Any], problem: dict[str, Any]) -> str:
    return f"{settings['branch_prefix']}{context.get('launch_id')}-problem-{problem.get('number')}"


def _message(message_file: Path, context: dict[str, Any]) -> str:
    problem = context.get("problem") or {}
    trailer = f"alla: прогон {context.get('launch_id')}, проблема {problem.get('number')}"
    try:
        text = message_file.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        text = ""
    if not text:
        why = " ".join(str((problem.get("proposal") or {}).get("why") or "").split())
        text = f"fix(autotest): правка автотеста для проблемы {problem.get('number')}"
        if why:
            text += f"\n\n{why}"
    text = text[:MESSAGE_LIMIT]
    return (text if trailer in text else f"{text}\n\n{trailer}") + "\n"


def _merge_request_url(stderr: bytes) -> str | None:
    """Ссылку на создание MR/PR печатают GitLab, GitHub и Bitbucket в строках ``remote:``."""
    for line in stderr.decode("utf-8", "replace").splitlines():
        if line.startswith("remote:"):
            match = URL_RE.search(line)
            if match:
                return match.group(0)
    return None


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    context_file = Path(os.environ["ALLA_CONTEXT"])
    context = json.loads(context_file.read_text(encoding="utf-8"))
    try:
        lines = publish(context, context_file)
    except (PublishError, OSError) as exc:
        print("STATUS: error")
        print(f"Правку не выгрузил: {exc}")
        return 1
    print("STATUS: ok")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
