"""Слэш-команды ``/alla:*``: формат для Qwen Code, ссылки на SKILL.md, отсутствие записи в проект.

Qwen Code молча пропускает команду с битым frontmatter, а ссылка на несуществующий раздел
``SKILL.md`` оставит модель без сценария — оба случая видны только здесь.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import pytest
from skill_fake_testops import FakeTestOps
from skill_fixtures import project_fixture, testops_fixture  # noqa: F401
from test_skill_flow import _prepare, _run

EXT_DIR = Path(__file__).resolve().parents[1]
COMMANDS_DIR = EXT_DIR / "commands" / "alla"
SKILL_MD = (EXT_DIR / "skills" / "launch" / "SKILL.md").read_text(encoding="utf-8")
WITH_ARGS = {
    "run", "continue", "apply", "revert", "remember", "reject",
    "extensions", "rerun", "code", "ticket",
}
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
    assert "скилл alla:launch (инструмент skill)" in " ".join(body.split())


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


@pytest.mark.skipif(os.name == "nt", reason="на Windows свои кавычки")
def test_setup_hint_quotes_awkward_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подсказка с командой должна разбираться shell и при апострофе или скобках в пути."""
    import alla_skill

    awkward = Path("/work/QA'sTests/e2e(test)/scripts/alla_skill.py")
    monkeypatch.setattr(alla_skill, "__file__", str(awkward))
    command = alla_skill.self_command("setup")

    parts = shlex.split(command)
    assert parts[1].endswith("QA'sTests/e2e(test)/scripts/alla_skill.py")
    assert parts[2:] == ["setup"]


def test_prepare_and_check_do_not_write_into_qwen_dir(
    project: Path, testops: FakeTestOps, capsys,
) -> None:
    _prepare(project, capsys)
    code, out = _run(["check", "--project-root", str(project)], capsys)

    assert code == 0 and out.startswith("STATUS: ready"), out
    assert not (project / ".qwen").exists()
    assert f"--project-root {shlex.quote(str(project))}" not in out
