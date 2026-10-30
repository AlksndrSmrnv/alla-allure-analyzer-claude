"""Встроенное расширение git-publish на настоящем git: репозиторий проекта и bare-remote."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from skill_fixtures import (  # noqa: F401
    nested_project_fixture,
    project_fixture,
    testops_fixture,
)
from skill_fake_testops import FakeTestOps
from test_skill_flow import PROPOSAL, TEST_ANALYSIS, _finish, _prepare, _run

from alla_skill_lib import cli
from alla_skill_lib import workspace as ws

BUNDLED = Path(cli.__file__).resolve().parents[2] / "extensions"
TEST_FILE = "src/test/java/ru/company/orders/OrderTest.java"
LOGIN_ANALYSIS = (
    "ЧТО СЛОМАЛОСЬ: Сервис авторизации недоступен.\n"
    "ПРИЧИНА: окружение — auth-service:8080 не принимает соединения.\n"
    "НАБЛЮДЕНИЯ:\n- [S1] «Connection refused: auth-service:8080»\n"
    "КАК ИСПРАВИТЬ:\n1. Поднять auth-service на стенде.\n"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Глобальные настройки git того, кто запускает тесты (подпись коммитов, хуки), не влияют."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)


def make_repo(repo: Path, tmp_path: Path) -> Path:
    """Настоящий git вместо пустой папки .git фикстуры; возвращает bare-remote."""
    shutil.rmtree(repo / ".git")
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Инженер")
    git(repo, "config", "user.email", "qa@example.com")
    (repo / ".gitignore").write_text("alla-reports/\n.qwen/\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    return remote


def enable(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.copytree(BUNDLED / "git-publish", ws.SKILL_DIR / "extensions" / "git-publish")
    code, out = _run(["extensions", "enable", "git-publish", "--project-root", str(project)], capsys)
    assert code == 0, out


def applied_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[Path, str, str]:
    """Разбор до done с правкой для проблемы 1, правка применена; (папка, file_id, вывод apply)."""
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    (run_dir / "analyses" / f"{order}.md").write_text(TEST_ANALYSIS, encoding="utf-8")
    assert _run(["next", str(run_dir)], capsys)[1].startswith("STATUS: propose")
    (run_dir / "proposals" / f"{order}.md").write_text(PROPOSAL, encoding="utf-8")
    _finish(run_dir, capsys, {login: LOGIN_ANALYSIS})
    _, diff = _run(["apply", "1", "--run", str(run_dir)], capsys)
    diff_hash = diff.split("--yes --diff ")[1].split()[0]
    code, out = _run(["apply", "1", "--run", str(run_dir), "--yes", "--diff", diff_hash], capsys)
    assert code == 0 and out.startswith("STATUS: applied"), out
    return run_dir, order, out


def publish(project: Path, run_dir: Path, order: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    context = run_dir / "extensions" / "git-publish" / f"after_apply-{order}.json"
    return _run(["extensions", "action", "git-publish", "publish", "--context", str(context),
                 "--project-root", str(project)], capsys)


def test_apply_hands_publish_task_to_subagent(
    project: Path, testops: FakeTestOps, tmp_path: Path, capsys
) -> None:
    make_repo(project, tmp_path)
    enable(project, capsys)
    assert (project / ".qwen" / "agents" / "alla-git-publish.md").is_file()
    run_dir, order, out = applied_run(project, capsys)

    task = run_dir / "extensions" / "git-publish" / f"after_apply-{order}.md"
    assert "Расширения:\n- Выгрузка правки в git (git-publish): запусти субагента" in out
    assert "subagent_type «alla-git-publish»" in out and f"«Выполни задание {task}»" in out
    text = task.read_text(encoding="utf-8")
    assert "$" not in text.replace("$ ", "")  # все переменные шаблона подставлены
    assert f"Файл: {TEST_FILE}, строка 6" in text and "201 Created" in text
    assert ws.skill_command("extensions", "action", "git-publish", "publish", "--context",
                            str(task.with_suffix(".json"))) in text
    assert f"{task.with_suffix('')}.message.txt" in text


def test_publish_pushes_only_the_fix_and_leaves_working_copy(
    project: Path, testops: FakeTestOps, tmp_path: Path, capsys
) -> None:
    remote = make_repo(project, tmp_path)
    enable(project, capsys)
    # Чужие незакоммиченные правки: в другом файле и в индексе — в коммит попасть не должны.
    login = project / "src/test/java/ru/company/auth/LoginTest.java"
    login.write_text(login.read_text(encoding="utf-8") + "// черновик\n", encoding="utf-8")
    (project / "notes.txt").write_text("личное\n", encoding="utf-8")
    git(project, "add", "notes.txt")
    run_dir, order, _ = applied_run(project, capsys)
    status_before = git(project, "status", "--porcelain")
    head_before = git(project, "rev-parse", "HEAD")
    (run_dir / "extensions" / "git-publish" / f"after_apply-{order}.message.txt").write_text(
        "fix(autotest): ждать 201 при создании заказа\n\nAPI отвечает 201 Created.\n",
        encoding="utf-8",
    )

    code, out = publish(project, run_dir, order, capsys)
    assert code == 0 and out.startswith("STATUS: ok"), out
    branch = "alla/777-problem-1"
    assert f"новую ветку {branch} на origin" in out and "в ветке main" in out
    assert git(project, "status", "--porcelain") == status_before
    assert git(project, "rev-parse", "HEAD") == head_before
    assert git(project, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    pushed = git(remote, "rev-parse", f"refs/heads/{branch}")
    assert git(remote, "rev-parse", f"{pushed}^") == head_before
    assert git(remote, "diff", "--name-only", head_before, pushed) == TEST_FILE
    assert git(remote, "show", f"{pushed}:{TEST_FILE}") == (project / TEST_FILE).read_text(
        encoding="utf-8").rstrip("\n")
    message = git(remote, "log", "-1", "--format=%B", pushed)
    assert message.startswith("fix(autotest): ждать 201") and "alla: прогон 777, проблема 1" in message

    # Повтор не создаёт вторую ветку.
    code, again = publish(project, run_dir, order, capsys)
    assert code == 0 and f"уже выгружена в ветку {branch}" in again
    assert "alla/777-problem-1-2" not in git(remote, "branch", "--list")

    # Откат напоминает о ветке на сервере, но ничего там не удаляет.
    code, reverted = _run(["revert", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and reverted.startswith("STATUS: reverted")
    assert f"ветка {branch} с ней осталась на origin" in reverted
    assert git(remote, "rev-parse", f"refs/heads/{branch}") == pushed


def test_publish_uses_patch_when_file_had_local_edits(
    project: Path, testops: FakeTestOps, tmp_path: Path, capsys
) -> None:
    remote = make_repo(project, tmp_path)
    enable(project, capsys)
    test_file = project / TEST_FILE
    # Незакоммиченная правка того же файла до apply: в ветку уходит только diff правки.
    test_file.write_text(test_file.read_text(encoding="utf-8").replace(
        "public class OrderTest {", "public class OrderTest { // TODO"), encoding="utf-8")
    run_dir, order, _ = applied_run(project, capsys)

    code, out = publish(project, run_dir, order, capsys)
    assert code == 0, out
    pushed = git(remote, "rev-parse", "refs/heads/alla/777-problem-1")
    content = git(remote, "show", f"{pushed}:{TEST_FILE}")
    assert "assertEquals(201, api.create()" in content and "// TODO" not in content


def test_publish_takes_next_free_branch_name(
    project: Path, testops: FakeTestOps, tmp_path: Path, capsys
) -> None:
    make_repo(project, tmp_path)
    git(project, "push", "-q", "origin", "main:refs/heads/alla/777-problem-1")
    enable(project, capsys)
    run_dir, order, _ = applied_run(project, capsys)
    code, out = publish(project, run_dir, order, capsys)
    assert code == 0 and "alla/777-problem-1-2" in out, out


def test_failed_push_keeps_local_branch_and_retries_it(
    project: Path, testops: FakeTestOps, tmp_path: Path, capsys
) -> None:
    remote = make_repo(project, tmp_path)
    enable(project, capsys)
    run_dir, order, _ = applied_run(project, capsys)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: доступ закрыт' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    code, out = publish(project, run_dir, order, capsys)
    assert code == 1 and out.startswith("STATUS: error") and "push в origin не прошёл" in out
    local = git(project, "rev-parse", "refs/heads/alla/777-problem-1")

    hook.unlink()
    code, out = publish(project, run_dir, order, capsys)
    assert code == 0 and "alla/777-problem-1 " in out, out
    assert git(remote, "rev-parse", "refs/heads/alla/777-problem-1") == local


def test_publish_refuses_when_file_changed_after_apply(
    project: Path, testops: FakeTestOps, tmp_path: Path, capsys
) -> None:
    make_repo(project, tmp_path)
    enable(project, capsys)
    run_dir, order, _ = applied_run(project, capsys)
    test_file = project / TEST_FILE
    test_file.write_text(test_file.read_text(encoding="utf-8") + "// ещё\n", encoding="utf-8")
    code, out = publish(project, run_dir, order, capsys)
    assert code == 1 and "менялся после apply" in out


def test_publish_without_remote_explains_setting(
    project: Path, testops: FakeTestOps, tmp_path: Path, capsys
) -> None:
    make_repo(project, tmp_path)
    git(project, "remote", "remove", "origin")
    enable(project, capsys)
    run_dir, order, _ = applied_run(project, capsys)
    code, out = publish(project, run_dir, order, capsys)
    assert code == 1 and "нет remote «origin»" in out and "[git-publish] remote" in out


def test_publish_from_project_in_repo_subfolder(
    nested_project: Path, testops: FakeTestOps, tmp_path: Path, capsys
) -> None:
    """Проект автотестов — подпапка репозитория: пути коммита и патча от корня репозитория."""
    remote = make_repo(nested_project, tmp_path)
    enable(nested_project, capsys)
    run_dir, run, _ = _prepare(nested_project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    analysis = TEST_ANALYSIS.replace("КОД: src/", "КОД: autotests/src/")
    (run_dir / "analyses" / f"{order}.md").write_text(analysis, encoding="utf-8")
    assert _run(["next", str(run_dir)], capsys)[1].startswith("STATUS: propose")
    (run_dir / "proposals" / f"{order}.md").write_text(
        PROPOSAL.replace("ФАЙЛ: src/", "ФАЙЛ: autotests/src/"), encoding="utf-8")
    _finish(run_dir, capsys, {login: LOGIN_ANALYSIS})
    _, diff = _run(["apply", "1", "--run", str(run_dir)], capsys)
    diff_hash = diff.split("--yes --diff ")[1].split()[0]
    _run(["apply", "1", "--run", str(run_dir), "--yes", "--diff", diff_hash], capsys)

    code, out = publish(nested_project, run_dir, order, capsys)
    assert code == 0, out
    pushed = git(remote, "rev-parse", "refs/heads/alla/777-problem-1")
    assert git(remote, "diff", "--name-only", f"{pushed}^", pushed) == f"autotests/{TEST_FILE}"
    result = json.loads((run_dir / "extensions" / "git-publish" / f"after_apply-{order}.result.json")
                        .read_text(encoding="utf-8"))
    assert result["pushed"] is True and result["branch"] == "alla/777-problem-1"


def test_publish_keeps_crlf_bytes(
    project: Path, testops: FakeTestOps, tmp_path: Path, capsys
) -> None:
    test_file = project / TEST_FILE
    test_file.write_bytes(test_file.read_bytes().replace(b"\n", b"\r\n"))
    remote = make_repo(project, tmp_path)
    enable(project, capsys)
    run_dir, order, _ = applied_run(project, capsys)
    code, out = publish(project, run_dir, order, capsys)
    assert code == 0, out
    pushed = git(remote, "rev-parse", "refs/heads/alla/777-problem-1")
    blob = subprocess.run(["git", "-C", str(remote), "show", f"{pushed}:{TEST_FILE}"],
                          capture_output=True, check=True).stdout
    assert blob == test_file.read_bytes() and b"201, api.create()" in blob and b"\r\n" in blob
