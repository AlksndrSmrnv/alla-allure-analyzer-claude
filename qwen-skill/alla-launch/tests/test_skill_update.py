"""``alla_skill.py update``: скилл обновляется из git-репозитория (настоящий git, ``file://``)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import skill_fixtures  # noqa: F401  # добавляет scripts/ в sys.path

import alla_skill
from alla_skill_lib import extensions

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
SKILL_MD = "---\nname: alla-launch\ndescription: тест\n---\n# Скилл\n"
SOURCE_PATH = "qwen-skill/alla-launch"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _publish(repo: Path, files: dict[str, str], prefix: str = SOURCE_PATH) -> str:
    """Закоммитить версию скилла в ``repo`` (папку скилла целиком заменить ``files``)."""
    if not (repo / ".git").exists():
        repo.mkdir(parents=True)
        _git(repo, "init", "--quiet")
    folder = repo / prefix
    shutil.rmtree(folder, ignore_errors=True)
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "v")
    return _git(repo, "rev-parse", "HEAD")


def _skill_v(version: int, requirements: str = "httpx>=0.27\n") -> dict[str, str]:
    return {
        "SKILL.md": SKILL_MD,
        # Настоящий скрипт: после замены обновлённый скилл должен продолжать работать.
        "scripts/alla_skill.py": (SCRIPTS_DIR / "alla_skill.py").read_text(encoding="utf-8")
        + f"\n# версия {version}\n",
        "scripts/alla_skill_lib/new_{}.py".format(version): "",
        "requirements.txt": requirements,
        "commands/alla/run.md": f"команда версии {version}\n",
    }


def _project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Проект с установленным скиллом (настоящий alla_skill.py): (проект, скилл, скрипт)."""
    project = tmp_path / "project"
    skill = project / ".qwen" / "skills" / "alla-launch"
    shutil.copytree(SCRIPTS_DIR, skill / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (skill / "requirements.txt").write_text("httpx>=0.27\n", encoding="utf-8")
    (skill / "scripts" / "alla_skill_lib" / "stale.py").write_text("", encoding="utf-8")
    (skill / ".env").write_text("ALLURE_TOKEN=secret\n", encoding="utf-8")
    return project, skill, skill / "scripts" / "alla_skill.py"


def _configure(project: Path, url: str, extra: str = "") -> None:
    folder = project / "alla-extensions"
    folder.mkdir(exist_ok=True)
    (folder / "config.toml").write_text(
        f'enabled = []\n\n[update]\nrepo = "{url}"\n{extra}', encoding="utf-8"
    )


def _update(stub: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "ALLA_SKILL_IN_VENV"}
    return subprocess.run(
        [sys.executable, str(stub), "update", *args], capture_output=True, text=True, env=env,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path / "internal"


def test_update_replaces_skill_and_keeps_user_files(tmp_path: Path, repo: Path) -> None:
    sha = _publish(repo, _skill_v(2))
    project, skill, stub = _project(tmp_path)
    (skill / ".venv").mkdir()
    (skill / ".venv" / "marker").write_text("старый venv скилла", encoding="utf-8")
    _configure(project, repo.as_uri())

    result = _update(stub)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "STATUS: updated" in result.stdout
    assert (skill / "scripts" / "alla_skill.py").read_text(encoding="utf-8").endswith("# версия 2\n")
    assert (skill / "scripts" / "alla_skill_lib" / "new_2.py").is_file()
    assert not (skill / "scripts" / "alla_skill_lib" / "stale.py").exists()  # удалён в новой версии
    assert (skill / ".env").read_text(encoding="utf-8") == "ALLURE_TOKEN=secret\n"
    assert (skill / ".venv" / "marker").is_file()
    assert (skill / ".alla-version").read_text(encoding="utf-8").strip() == f"{sha} {SOURCE_PATH}"
    command = project / ".qwen" / "commands" / "alla" / "run.md"
    assert command.read_text(encoding="utf-8") == "команда версии 2\n"
    assert sha[:8] in result.stdout
    # Окружения ещё нет — подсказка про setup; чужих временных папок рядом не осталось.
    assert "setup" in result.stdout
    assert sorted(path.name for path in skill.parent.iterdir()) == ["alla-launch"]


def test_update_again_is_ok_and_touches_nothing(tmp_path: Path, repo: Path) -> None:
    _publish(repo, _skill_v(2))
    project, skill, stub = _project(tmp_path)
    _configure(project, repo.as_uri())
    assert "STATUS: updated" in _update(stub).stdout
    marker = skill / "scripts" / "alla_skill.py"
    before = marker.stat().st_mtime_ns

    result = _update(stub)

    assert result.returncode == 0
    assert result.stdout.startswith("STATUS: ok")
    assert marker.stat().st_mtime_ns == before


def test_update_picks_up_new_commit_and_reports_changes(tmp_path: Path, repo: Path) -> None:
    _publish(repo, _skill_v(2))
    project, skill, stub = _project(tmp_path)
    _configure(project, repo.as_uri())
    _update(stub)
    _publish(repo, _skill_v(3))

    result = _update(stub)

    assert result.stdout.startswith("STATUS: updated")
    assert "добавлено 1" in result.stdout  # new_3.py
    assert "удалено 1" in result.stdout  # new_2.py
    assert not (skill / "scripts" / "alla_skill_lib" / "new_2.py").exists()
    assert (skill / "scripts" / "alla_skill_lib" / "new_3.py").is_file()


def test_update_tells_to_run_setup_when_requirements_changed(tmp_path: Path, repo: Path) -> None:
    import hashlib

    _publish(repo, _skill_v(2, requirements="httpx>=0.28\n"))
    project, skill, stub = _project(tmp_path)
    venv_python = project / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    digest = hashlib.sha256(b"httpx>=0.27\n").hexdigest()
    (project / ".venv" / ".alla-setup-complete").write_text(digest + "\n", encoding="utf-8")
    _configure(project, repo.as_uri())

    result = _update(stub)

    assert result.stdout.startswith("STATUS: updated")
    assert "изменился requirements.txt" in result.stdout
    assert f"{stub} setup" in result.stdout


def test_update_source_and_ref_override_config(tmp_path: Path, repo: Path) -> None:
    _publish(repo, _skill_v(2))
    _git(repo, "branch", "release")
    _publish(repo, _skill_v(3))
    project, skill, stub = _project(tmp_path)
    _configure(project, "file:///нет/такого", 'ref = "main"\n')

    result = _update(stub, "--source", repo.as_uri(), "--ref=release")

    assert "STATUS: updated" in result.stdout
    assert (skill / "scripts" / "alla_skill_lib" / "new_2.py").is_file()
    assert not (skill / "scripts" / "alla_skill_lib" / "new_3.py").exists()


def test_update_finds_skill_in_repository_root_and_by_path(tmp_path: Path, repo: Path) -> None:
    _publish(repo, _skill_v(2), prefix="tools/alla")
    project, skill, stub = _project(tmp_path)
    _configure(project, repo.as_uri())

    assert _update(stub).stdout.startswith("STATUS: error")  # path не задан, скилла нет

    _configure(project, repo.as_uri(), 'path = "tools/alla"\n')
    result = _update(stub)

    assert "STATUS: updated" in result.stdout, result.stdout
    assert (skill / "scripts" / "alla_skill_lib" / "new_2.py").is_file()


def test_update_without_repository_explains_how_to_set_it(tmp_path: Path) -> None:
    project, skill, stub = _project(tmp_path)

    result = _update(stub)

    assert result.returncode == 2
    assert result.stdout.startswith("STATUS: error")
    assert "[update]" in result.stdout and "alla-extensions/config.toml" in result.stdout
    assert (skill / "scripts" / "alla_skill_lib" / "stale.py").exists()


@pytest.mark.parametrize(
    "url",
    ["https://user:token@git.example.com/alla.git", "http://:pw@host/x.git", "-oBad",
     "https://TOKEN@git.example.com/alla.git", "ssh://git:pw@host/x.git"],
)
def test_update_refuses_credentials_in_url(tmp_path: Path, url: str) -> None:
    project, skill, stub = _project(tmp_path)

    result = _update(stub, "--source", url)

    assert result.returncode == 2
    assert result.stdout.startswith("STATUS: error")
    assert (skill / ".env").is_file()


def test_ssh_urls_with_user_only_are_allowed() -> None:
    for url in ("git@git.example.com:team/alla.git", "ssh://git@git.example.com/team/alla.git"):
        assert not alla_skill._URL_PASSWORD_RE.match(url)


def test_git_env_forwards_project_ssh_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """core.sshCommand из настроек репозитория проекта доходит до clone через GIT_SSH_COMMAND."""
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "core.sshCommand", "ssh -i /ключ/проекта")
    monkeypatch.setattr(alla_skill, "PROJECT_DIR", tmp_path)
    for name in ("GIT_SSH_COMMAND", "GIT_SSH"):
        monkeypatch.delenv(name, raising=False)

    assert alla_skill.git_env()["GIT_SSH_COMMAND"] == "ssh -i /ключ/проекта"

    _git(tmp_path, "config", "--unset", "core.sshCommand")
    assert "BatchMode=yes" in alla_skill.git_env().get("GIT_SSH_COMMAND", "BatchMode=yes")


def test_update_refuses_outside_installed_skill(tmp_path: Path, repo: Path) -> None:
    """Рабочая копия разработчика (не .qwen/skills/…) не заменяется."""
    _publish(repo, _skill_v(2))
    checkout = tmp_path / "dev" / "qwen-skill" / "alla-launch"
    shutil.copytree(SCRIPTS_DIR, checkout / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    stub = checkout / "scripts" / "alla_skill.py"

    result = _update(stub, "--source", repo.as_uri())

    assert result.returncode == 2
    assert result.stdout.startswith("STATUS: error")
    assert not stub.read_text(encoding="utf-8").endswith("# версия 2\n")


def test_update_failures_leave_installation_intact(tmp_path: Path, repo: Path) -> None:
    project, skill, stub = _project(tmp_path)
    before = (skill / "scripts" / "alla_skill.py").read_bytes()

    unreachable = _update(stub, "--source", (tmp_path / "нет").as_uri())
    _publish(repo, {"README": "не скилл"})
    no_skill = _update(stub, "--source", repo.as_uri())

    for result in (unreachable, no_skill):
        assert result.returncode == 1
        assert result.stdout.startswith("STATUS: error")
    assert "Не удалось получить репозиторий" in unreachable.stdout
    assert "нет скилла alla-launch" in no_skill.stdout
    assert (skill / "scripts" / "alla_skill.py").read_bytes() == before
    assert (skill / ".env").is_file()
    assert not list(skill.parent.glob(".alla-update-*"))


def test_update_rolls_back_when_swap_fails(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой переименования новой папки возвращает прежнюю вместе с .env и .venv."""
    _publish(repo, _skill_v(2))
    project, skill, _ = _project(tmp_path)
    (skill / ".venv").mkdir()
    monkeypatch.setattr(alla_skill, "SKILL_DIR", skill)
    real_rename = os.rename

    def failing_rename(src: str, dst: str) -> None:
        if str(dst) == str(skill) and Path(src).name == "new":
            raise OSError("занято")
        real_rename(src, dst)

    monkeypatch.setattr(alla_skill.os, "rename", failing_rename)
    source = tmp_path / "source"
    shutil.copytree(repo / SOURCE_PATH, source)

    reason = alla_skill.swap_skill_dir(source)

    assert "занято" in reason
    assert (skill / "scripts" / "alla_skill_lib" / "stale.py").is_file()
    assert (skill / ".env").is_file() and (skill / ".venv").is_dir()
    assert not list(skill.parent.glob(".alla-update-*"))


def test_read_update_config_reads_only_update_table(tmp_path: Path) -> None:
    folder = tmp_path / "alla-extensions"
    folder.mkdir()
    (folder / "config.toml").write_text(
        'repo = "верхний"\nenabled = ["sut-code"]\n\n[sut-code]\nrepo = "чужой"\n\n'
        "[update]  # адрес команды\n"
        'repo = "git@git.example.com:team/alla.git"  # внутренний\n'
        "ref = 'v1.2'\n"
        'path = "a\\"b"\n'
        "# note = \"закомментировано\"\n\n[other]\nref = \"чужая\"\n",
        encoding="utf-8",
    )

    assert alla_skill.read_update_config(tmp_path) == {
        "repo": "git@git.example.com:team/alla.git", "ref": "v1.2", "path": 'a"b',
    }
    assert alla_skill.read_update_config(tmp_path / "нет") == {}


def test_config_table_update_does_not_break_extensions_config(tmp_path: Path) -> None:
    _configure(tmp_path, "git@git.example.com:team/alla.git")

    config = extensions.load_config(tmp_path)

    assert config.enabled == ()
    assert config.settings["update"]["repo"] == "git@git.example.com:team/alla.git"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], (None, None)),
        (["--source", "u", "--ref", "r"], ("u", "r")),
        (["--source=u"], ("u", None)),
        (["--source"], None),
        (["--bogus"], None),
    ],
)
def test_update_argument_split(argv: list[str], expected: tuple[str | None, str | None] | None) -> None:
    assert alla_skill.split_update_args(argv) == expected


def test_update_reinstalls_when_path_changes_within_same_commit(tmp_path: Path, repo: Path) -> None:
    _publish(repo, _skill_v(2), prefix="old-layout/alla-launch")
    _publish(repo, _skill_v(3), prefix="new-layout/alla-launch")
    project, skill, stub = _project(tmp_path)
    _configure(project, repo.as_uri(), 'path = "old-layout/alla-launch"\n')
    assert "STATUS: updated" in _update(stub).stdout
    _configure(project, repo.as_uri(), 'path = "new-layout/alla-launch"\n')

    result = _update(stub)

    assert result.stdout.startswith("STATUS: updated")
    assert (skill / "scripts" / "alla_skill_lib" / "new_3.py").is_file()
    assert not (skill / "scripts" / "alla_skill_lib" / "new_2.py").exists()


def test_update_keeps_staging_when_retained_files_cannot_be_restored(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не вернулся .env — единственная копия остаётся в .alla-update-*, а не удаляется."""
    _publish(repo, _skill_v(2))
    project, skill, _ = _project(tmp_path)
    monkeypatch.setattr(alla_skill, "SKILL_DIR", skill)
    real_rename = os.rename

    def failing_rename(src: str, dst: str) -> None:
        if str(dst) == str(skill) and Path(src).name == "new":
            raise OSError("занято")
        if Path(src).name == ".env" and Path(src).parent.name == "new":
            raise OSError("не вернуть")
        real_rename(src, dst)

    monkeypatch.setattr(alla_skill.os, "rename", failing_rename)
    source = tmp_path / "source"
    shutil.copytree(repo / SOURCE_PATH, source)

    reason = alla_skill.swap_skill_dir(source)

    assert "не вернулись на место" in reason and ".alla-update-" in reason
    assert (skill / "scripts" / "alla_skill_lib" / "stale.py").is_file()  # прежняя папка на месте
    kept = list(skill.parent.glob(".alla-update-*/new/.env"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "ALLURE_TOKEN=secret\n"
