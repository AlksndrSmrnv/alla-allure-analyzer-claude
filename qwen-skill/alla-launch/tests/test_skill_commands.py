"""Слэш-команды ``/alla:*``: формат для Qwen Code, ссылки на SKILL.md, установка в проект.

Qwen Code молча пропускает команду с битым frontmatter, а ссылка на несуществующий раздел
``SKILL.md`` оставит модель без сценария — оба случая видны только здесь.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path

import pytest
from skill_fake_testops import FakeTestOps
from skill_fixtures import project_fixture, testops_fixture  # noqa: F401
from test_skill_flow import _prepare

from alla_skill_lib import qwen_files
from alla_skill_lib import workspace as ws

SKILL_DIR = Path(__file__).resolve().parents[1]
COMMANDS_DIR = SKILL_DIR / "commands" / "alla"
SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
WITH_ARGS = {"run", "continue", "apply", "revert", "remember", "reject", "extensions", "rerun"}
# Регулярка parseMarkdownCommand из Qwen Code (FileCommandLoader).
FRONTMATTER = re.compile(r"^---\n(?:([\s\S]*?)\n)?---(?:\n|$)([\s\S]*)$")
# С этих символов простое значение YAML начинаться не может (кроме «<»).
YAML_INDICATORS = tuple("[]{}&*!|>'\"%@`#,?:-")


def _command(path: Path) -> tuple[dict[str, str], str]:
    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    assert match, f"{path.name}: нет frontmatter"
    fields: dict[str, str] = {}
    for line in (match.group(1) or "").splitlines():
        key, sep, value = line.partition(": ")
        assert sep and key and not line.startswith(" "), f"{path.name}: «{line}»"
        value = value.strip()
        if value.startswith('"'):
            assert value.endswith('"') and value.count('"') == 2, f"{path.name}: {key}"
            value = value[1:-1]
        else:
            assert not value.startswith(YAML_INDICATORS), f"{path.name}: {key} нужно в кавычках"
            assert ": " not in value and " #" not in value, f"{path.name}: {key} нужно в кавычках"
        fields[key] = value
    return fields, match.group(2).strip()


def _commands() -> list[Path]:
    return sorted(COMMANDS_DIR.glob("*.md"))


def test_command_set_is_the_documented_one() -> None:
    names = {path.stem for path in _commands()}
    assert names == WITH_ARGS | {"review", "setup"}
    assert not list(COMMANDS_DIR.parent.glob("*.md")), "команда вне alla/ осталась бы без префикса"
    for name in names:
        assert f"`/alla:{name}" in SKILL_MD, f"/alla:{name} нет в разделе «Слэш-команды»"


@pytest.mark.parametrize("path", _commands(), ids=lambda path: path.stem)
def test_command_file_is_valid_for_qwen(path: Path) -> None:
    fields, body = _command(path)
    assert set(fields) <= {"description", "argument-hint", "disable-model-invocation"}
    assert fields["description"]
    # Без этого модель может вызвать команду сама — например, /alla:apply.
    assert fields["disable-model-invocation"] == "true"
    assert ("{{args}}" in body) is (path.stem in WITH_ARGS)
    assert ("argument-hint" in fields) is (path.stem in WITH_ARGS)
    assert "!{" not in body and "@{" not in body  # только просьба к модели, без инъекций
    assert "скилл alla-launch (инструмент skill)" in " ".join(body.split())


@pytest.mark.parametrize("path", _commands(), ids=lambda path: path.stem)
def test_command_refers_to_existing_skill_sections(path: Path) -> None:
    _, body = _command(path)
    text = " ".join(body.split())
    # Разделы в тексте — «1. Старт», «Слэш-команды»; прочие «…» — цитаты, не ссылки.
    sections = [quote for quote in re.findall(r"«([^»]+)»", text)
                if re.match(r"\d\. ", quote) or quote == "Слэш-команды"]
    assert sections, f"{path.stem}: не назван раздел SKILL.md"
    headings = {line[3:].strip() for line in SKILL_MD.splitlines() if line.startswith("## ")}
    for section in sections:
        assert section in headings, f"{path.stem}: раздела «{section}» нет в SKILL.md"
    for item in re.findall(r"пункт «([^»]+)»", text):
        assert f"**{item}**" in SKILL_MD, f"{path.stem}: пункта «{item}» нет в SKILL.md"


def test_install_commands_writes_new_and_changed_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws, "SKILL_DIR", SKILL_DIR)
    target_dir = tmp_path / ".qwen" / "commands" / "alla"
    (target_dir).mkdir(parents=True)
    (target_dir / "my.md").write_text("своя команда\n", encoding="utf-8")

    written = qwen_files.install_commands(tmp_path)
    assert [path.name for path in written] == [path.name for path in _commands()]
    for path in _commands():
        assert (target_dir / path.name).read_text(encoding="utf-8") == path.read_text(encoding="utf-8")
    assert qwen_files.install_commands(tmp_path) == []  # уже установлены — не пишет
    (target_dir / "apply.md").write_text("старая версия", encoding="utf-8")
    assert qwen_files.install_commands(tmp_path) == [target_dir / "apply.md"]
    assert (target_dir / "my.md").exists()  # чужое не удаляется


@pytest.mark.skipif(os.name == "nt", reason="на Windows свои кавычки")
def test_setup_hint_quotes_awkward_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подсказка с командой должна разбираться shell и при апострофе или скобках в пути."""
    import alla_skill

    awkward = Path("/work/QA'sTests/e2e(test)/alla_skill.py")
    monkeypatch.setattr(alla_skill, "__file__", str(awkward))
    command = alla_skill.self_command("setup", "--python", "/opt/py 3.11/bin/python")

    parts = shlex.split(command)
    assert parts[1].endswith("QA'sTests/e2e(test)/alla_skill.py")
    assert parts[2:] == ["setup", "--python", "/opt/py 3.11/bin/python"]


def test_setup_installs_the_same_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``setup`` идёт на Python 3.8 без библиотеки — его копия должна давать тот же результат."""
    import alla_skill

    monkeypatch.setattr(ws, "SKILL_DIR", SKILL_DIR)
    monkeypatch.setattr(alla_skill, "SKILL_DIR", SKILL_DIR)
    lib, launcher = tmp_path / "lib", tmp_path / "launcher"
    qwen_files.install_commands(lib)
    written = alla_skill.install_commands(launcher)
    assert [path.name for path in written] == [path.name for path in _commands()]
    assert alla_skill.install_commands(launcher) == []
    for path in _commands():
        relative = Path(".qwen", "commands", "alla", path.name)
        assert (launcher / relative).read_bytes() == (lib / relative).read_bytes()


def test_prepare_installs_agent_and_commands(project: Path, testops: FakeTestOps, capsys) -> None:
    shutil.copytree(SKILL_DIR / "commands", ws.SKILL_DIR / "commands")
    shutil.copytree(SKILL_DIR / "agents", ws.SKILL_DIR / "agents")

    _prepare(project, capsys)

    assert (project / ".qwen" / "agents" / "alla-batch.md").is_file()
    installed = sorted(path.name for path in (project / ".qwen" / "commands" / "alla").glob("*.md"))
    assert installed == [path.name for path in _commands()]
