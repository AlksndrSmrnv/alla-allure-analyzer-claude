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
from test_skill_flow import _prepare, _run

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
    assert names == WITH_ARGS | {"review", "setup", "update"}
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


def test_install_planned_writes_new_and_changed_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws, "SKILL_DIR", SKILL_DIR)
    target_dir = tmp_path / ".qwen" / "commands" / "alla"
    (target_dir).mkdir(parents=True)
    (target_dir / "my.md").write_text("своя команда\n", encoding="utf-8")

    plan = qwen_files.planned_files(tmp_path)
    assert [item.state for item in plan] == [qwen_files.NEW] * len(plan)
    written = qwen_files.install_planned(plan)
    assert [item.target for item in written] == [item.target for item in plan]
    for path in _commands():
        assert (target_dir / path.name).read_text(encoding="utf-8") == path.read_text(encoding="utf-8")
    assert (tmp_path / ".qwen" / "agents" / "alla-batch.md").is_file()
    plan = qwen_files.planned_files(tmp_path)
    assert {item.state for item in plan} == {qwen_files.SAME}
    assert qwen_files.install_planned(plan) == []  # уже установлены — не пишет
    (target_dir / "apply.md").write_text("старая версия", encoding="utf-8")
    plan = qwen_files.planned_files(tmp_path)
    assert [item.target for item in plan if item.state == qwen_files.CHANGED] == [target_dir / "apply.md"]
    assert [item.target for item in qwen_files.install_planned(plan)] == [target_dir / "apply.md"]
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


def test_source_without_trailing_newline_is_installed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_text дописывает перевод строки: сверка с ним, иначе файл вечно «изменён»."""
    monkeypatch.setattr(ws, "SKILL_DIR", tmp_path / "no-skill")
    source = tmp_path / "ext-agent.md"
    source.write_text("без перевода строки", encoding="utf-8")
    project = tmp_path / "project"

    plan = qwen_files.planned_files(project, {"alla-demo": source})
    assert [item.state for item in plan] == [qwen_files.NEW]
    assert len(qwen_files.install_planned(plan)) == 1
    plan = qwen_files.planned_files(project, {"alla-demo": source})
    assert [item.state for item in plan] == [qwen_files.SAME]
    assert qwen_files.install_planned(plan) == []


def test_command_file_in_another_encoding_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws, "SKILL_DIR", SKILL_DIR)
    target = tmp_path / ".qwen" / "commands" / "alla" / "run.md"
    target.parent.mkdir(parents=True)
    target.write_bytes("команда".encode("utf-16"))

    plan = qwen_files.planned_files(tmp_path)
    assert next(item.state for item in plan if item.target == target) == qwen_files.CHANGED
    qwen_files.install_planned(plan)
    assert target.read_text(encoding="utf-8") == (COMMANDS_DIR / "run.md").read_text(encoding="utf-8")


def test_install_files_lists_without_writing_and_installs_after_yes(
    project: Path, testops: FakeTestOps, capsys,
) -> None:
    shutil.copytree(SKILL_DIR / "commands", ws.SKILL_DIR / "commands")
    shutil.copytree(SKILL_DIR / "agents", ws.SKILL_DIR / "agents")
    qwen_dir = project / ".qwen"

    code, out = _run(["install-files", "--project-root", str(project)], capsys)
    assert code == 0, out
    assert out.splitlines()[0] == "STATUS: install_files"
    assert "alla-batch.md" in out and "/alla:run" in out and str(qwen_dir) in out
    assert "install-files --yes" in out and "«да»" in out
    assert f"--project-root {shlex.quote(str(project))}" in out  # подтверждение — для того же проекта
    assert not qwen_dir.exists()  # без --yes ничего не записано

    code, out = _run(["install-files", "--yes", "--project-root", str(project)], capsys)
    assert code == 0, out
    assert out.splitlines()[0] == "STATUS: ok"
    assert (qwen_dir / "agents" / "alla-batch.md").is_file()
    installed = sorted(path.name for path in (qwen_dir / "commands" / "alla").glob("*.md"))
    assert installed == [path.name for path in _commands()]

    code, out = _run(["install-files", "--project-root", str(project)], capsys)
    assert out.splitlines()[0] == "STATUS: ok" and "ставить нечего" in out
    (qwen_dir / "commands" / "alla" / "apply.md").write_text("старая версия", encoding="utf-8")
    code, out = _run(["install-files", "--project-root", str(project)], capsys)
    assert out.splitlines()[0] == "STATUS: install_files"
    assert "обновить" in out and "apply.md" in out and "run.md" not in out


def test_prepare_and_check_do_not_write_into_qwen_dir(
    project: Path, testops: FakeTestOps, capsys,
) -> None:
    shutil.copytree(SKILL_DIR / "commands", ws.SKILL_DIR / "commands")
    shutil.copytree(SKILL_DIR / "agents", ws.SKILL_DIR / "agents")

    _prepare(project, capsys)
    code, out = _run(["check", "--project-root", str(project)], capsys)

    assert not (project / ".qwen").exists()
    assert "не установлены или устарели" in out and "install-files" in out
    assert f"--project-root {shlex.quote(str(project))}" in out
