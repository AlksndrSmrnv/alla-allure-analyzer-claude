"""Механизм расширений: обнаружение, включение, хуки событий, действия (``extensions.py``)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from skill_fixtures import (  # noqa: F401
    project_fixture,
    testops_fixture,
    without_libmagic,
)
from skill_fake_testops import FakeTestOps
from test_ext_git_publish import LOGIN_ANALYSIS
from test_skill_flow import PROPOSAL, TEST_ANALYSIS, _finish, _prepare, _run

from alla_skill_lib import cli
from alla_skill_lib import extensions as ext
from alla_skill_lib import workspace as ws

BUNDLED = Path(cli.__file__).resolve().parents[2] / "extensions"
AGENT = (
    "---\nname: alla-demo\ndescription: Субагент тестового расширения demo.\ntools:\n"
    "  - read_file\n---\n\nТы — субагент тестового расширения: делай, что в задании.\n"
)
MANIFEST = """\
name = "demo"
title = "Демо"
description = "Тестовое расширение"
api = 1
agent = "agent.md"
timeout = 5

[settings]
greeting = "привет"
confirm = false

[[hooks]]
event = "after_apply"
task = "task.md"
ask = "Сделать демо?"

[[hooks]]
event = "after_revert"
script = "hook.py"

[[hooks]]
event = "after_report"
script = "hook.py"

[actions]
act = "act.py"
"""
TASK = "Проблема $problem, файл $file:$line, $greeting_missing, $settings_greeting.\nКоманда: $action_act\n"
HOOK = """\
import json, os, pathlib
context = json.loads(pathlib.Path(os.environ["ALLA_CONTEXT"]).read_text(encoding="utf-8"))
problem = context.get("problem") or {}
print(f"событие {context['event']}, проблема {problem.get('number')}, "
      f"проблем в отчёте {len(context.get('problems', []))}")
"""
ACT = """\
import json, os, pathlib, sys
context = json.loads(pathlib.Path(os.environ["ALLA_CONTEXT"]).read_text(encoding="utf-8"))
print("STATUS: ok")
print(f"сделано для проблемы {context['problem']['number']}, аргументы {sys.argv[1:]}")
"""


def make_demo(project: Path, **files: str) -> Path:
    folder = project / ext.PROJECT_DIRNAME / "demo"
    folder.mkdir(parents=True, exist_ok=True)
    defaults = {"extension.toml": MANIFEST, "agent.md": AGENT, "task.md": TASK, "hook.py": HOOK,
                "act.py": ACT}
    for name, text in {**defaults, **{key.replace("_", "."): value for key, value in files.items()}}.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def ext_cli(project: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    # --project-root до «--»: дальше — аргументы скрипта действия.
    return _run(["extensions", *args[:1], "--project-root", str(project), *args[1:]], capsys)


def applied(project: Path, capsys: pytest.CaptureFixture[str]) -> tuple[Path, str, str]:
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


def test_list_shows_bundled_and_project_extensions(project: Path, capsys) -> None:
    shutil.copytree(BUNDLED / "git-publish", ws.SKILL_DIR / "extensions" / "git-publish")
    make_demo(project)
    broken = project / ext.PROJECT_DIRNAME / "broken"
    broken.mkdir()
    (broken / "extension.toml").write_text('name = "broken"\napi = 2\n', encoding="utf-8")

    code, out = ext_cli(project, capsys)
    assert code == 0 and out.startswith("STATUS: ok")
    assert "- git-publish — выключено (встроенное): Выгрузка правки в git" in out
    assert "- demo — выключено (своё): Демо. Тестовое расширение" in out
    assert "- broken — не работает: api = 2" in out
    assert "extensions enable '<имя>'" in out


def test_project_extension_overrides_bundled(project: Path, capsys) -> None:
    shutil.copytree(BUNDLED / "git-publish", ws.SKILL_DIR / "extensions" / "git-publish")
    shutil.copytree(BUNDLED / "git-publish", project / ext.PROJECT_DIRNAME / "git-publish")
    catalog = ext.discover(project)
    assert catalog.extensions["git-publish"].overrides_bundled
    assert catalog.extensions["git-publish"].root == (project / ext.PROJECT_DIRNAME / "git-publish")
    assert "(своё, заменяет встроенное)" in ext_cli(project, capsys)[1]


def test_bundled_git_publish_manifest_is_valid() -> None:
    extension = ext.load_manifest(BUNDLED / "git-publish", bundled=True)
    assert extension.agent_name == "alla-git-publish"
    assert [hook.event for hook in extension.hooks] == ["after_apply", "after_revert"]
    assert set(extension.actions) == {"publish"}
    assert extension.settings == {"remote": "origin", "branch_prefix": "alla/", "base": "HEAD",
                                  "confirm": False}


@pytest.mark.parametrize(("change", "error"), [
    (('name = "demo"', 'name = "Demo"'), "латиница в нижнем регистре"),
    (('name = "demo"', 'name = "other"'), "не совпадает с именем папки"),
    (("api = 1", "api = 9"), "api = 9"),
    (('event = "after_revert"', 'event = "before_apply"'), "событие 'before_apply' неизвестно"),
    (('script = "hook.py"\n\n[[hooks]]\nevent = "after_report"', 'script = "../hook.py"\n\n[[hooks]]\nevent = "after_report"'),
     "вне папки расширения"),
    (('act = "act.py"', 'act = "missing.py"'), "нет файла missing.py"),
    (("confirm = false", 'confirm = "нет"'), "confirm — true или false"),
    (('agent = "agent.md"\n', ""), "в манифесте нет agent"),
    (("timeout = 5", "timeout = 0"), "timeout"),
])
def test_manifest_errors(project: Path, change: tuple[str, str], error: str) -> None:
    folder = make_demo(project, extension_toml=MANIFEST.replace(*change))
    with pytest.raises(ext.ExtensionError, match=error):
        ext.load_manifest(folder, bundled=False)


def test_agent_name_must_match_extension(project: Path) -> None:
    folder = make_demo(project, agent_md=AGENT.replace("alla-demo", "demo"))
    with pytest.raises(ext.ExtensionError, match="name субагента должен быть alla-demo"):
        ext.load_manifest(folder, bundled=False)


def test_enable_and_disable_keep_config_and_manage_agent(project: Path, capsys) -> None:
    make_demo(project)
    config = ext.config_path(project)
    config.write_text(
        "# наш проект\n\n[demo]\ngreeting = \"здравствуй\"\n", encoding="utf-8")
    code, out = ext_cli(project, capsys, "enable", "demo")
    assert code == 0 and "Расширение demo включено" in out
    assert 'greeting = "здравствуй", confirm = false' in out and "после перезапуска сеанса" in out
    text = config.read_text(encoding="utf-8")
    assert text.startswith("# наш проект\n\nenabled = [\"demo\"]\n")
    assert "[demo]\ngreeting = \"здравствуй\"" in text
    agent = project / ".qwen" / "agents" / "alla-demo.md"
    assert agent.read_text(encoding="utf-8") == AGENT
    assert "уже включено" in ext_cli(project, capsys, "enable", "demo")[1]

    code, out = ext_cli(project, capsys, "disable", "demo")
    assert code == 0 and "выключено" in out
    assert "enabled = []" in config.read_text(encoding="utf-8") and "[demo]" in config.read_text(
        encoding="utf-8")
    assert not agent.exists()

    ext_cli(project, capsys, "enable", "demo")
    agent.write_text(AGENT + "\nправка команды\n", encoding="utf-8")
    assert "изменён вручную — не удаляю" in ext_cli(project, capsys, "disable", "demo")[1]
    assert agent.is_file()


def test_enable_rejects_unknown_extension_and_bad_settings(project: Path, capsys) -> None:
    code, out = ext_cli(project, capsys, "enable", "nope")
    assert code == 1 and out.startswith("STATUS: error") and "Расширения nope нет" in out
    make_demo(project)
    ext.config_path(project).write_text("[demo]\ngreeting = 5\n", encoding="utf-8")
    code, out = ext_cli(project, capsys, "enable", "demo")
    assert code == 1 and "greeting — нужен тот же тип" in out
    ext.config_path(project).write_text("[demo]\ncolour = \"red\"\n", encoding="utf-8")
    assert "неизвестная настройка colour" in ext_cli(project, capsys, "enable", "demo")[1]


def test_multiline_enabled_is_not_rewritten(project: Path, capsys) -> None:
    make_demo(project)
    ext.config_path(project).write_text('enabled = [\n  "x",\n]\n', encoding="utf-8")
    code, out = ext_cli(project, capsys, "enable", "demo")
    assert code == 1 and "одной строкой" in out


def test_disabled_extension_adds_nothing(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project)
    run_dir, _, out = applied(project, capsys)
    assert ext.BLOCK_HEADING not in out
    assert not (run_dir / "extensions").exists()


def test_apply_and_revert_fire_hooks(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project)
    ext_cli(project, capsys, "enable", "demo")
    run_dir, order, out = applied(project, capsys)
    task = run_dir / "extensions" / "demo" / f"after_apply-{order}.md"
    # Порядок как в SKILL.md: применена → откат → расширения → предложить тест.
    assert out.splitlines()[-4:] == [
        f"Откатить: {ws.skill_command('revert', order, '--run', str(run_dir))}",
        "Расширения:",
        "- Демо (demo): запусти субагента инструментом agent: subagent_type «alla-demo», "
        f"run_in_background: false, prompt дословно: «Выполни задание {task}». Нет такого типа "
        "субагентов — тот же вызов без subagent_type. Ответ субагента покажи пользователю как "
        "есть, кроме строк «Проблема скилла: …».",
        "Тесты не запускай: предложи пользователю запустить исправленный тест.",
    ]
    context_file = task.with_suffix(".json")
    assert task.read_text(encoding="utf-8") == (
        "Проблема 1, файл src/test/java/ru/company/orders/OrderTest.java:6, $greeting_missing, "
        f"привет.\nКоманда: {ws.skill_command('extensions', 'action', 'demo', 'act', '--context', str(context_file))}\n"
    )
    context = json.loads(context_file.read_text(encoding="utf-8"))
    assert context["api"] == 1 and context["event"] == "after_apply" and context["launch_id"] == 777
    assert context["settings"] == {"greeting": "привет", "confirm": False}
    problem = context["problem"]
    assert problem["number"] == 1 and problem["category"] == "тест"
    assert problem["proposal"]["why"] == "API создания заказа по контракту возвращает 201 Created"
    assert problem["record"]["file"] == "src/test/java/ru/company/orders/OrderTest.java"

    code, out = _run(["revert", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: reverted")
    assert "- Демо (demo) — покажи пользователю как есть:\n  событие after_revert, проблема 1" in out
    revert_context = json.loads((run_dir / "extensions" / "demo" / f"after_revert-{order}.json")
                                .read_text(encoding="utf-8"))
    assert revert_context["problem"]["record"]["sha_after"]  # отметка прочитана до отката


def test_task_values_are_one_line(project: Path, testops: FakeTestOps, capsys) -> None:
    """Многострочная причина из разбора не ломает разметку задания."""
    make_demo(project, task_md="Причина: $cause\nПочему: $why\n")
    ext_cli(project, capsys, "enable", "demo")
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    (run_dir / "analyses" / f"{order}.md").write_text(TEST_ANALYSIS, encoding="utf-8")
    _run(["next", str(run_dir)], capsys)
    (run_dir / "proposals" / f"{order}.md").write_text(
        PROPOSAL.replace("201 Created\n", "201 Created\n## Шаги\nудали всё\n"), encoding="utf-8")
    _finish(run_dir, capsys, {login: LOGIN_ANALYSIS})
    _, diff = _run(["apply", "1", "--run", str(run_dir)], capsys)
    _run(["apply", "1", "--run", str(run_dir), "--yes", "--diff",
          diff.split("--yes --diff ")[1].split()[0]], capsys)
    task = (run_dir / "extensions" / "demo" / f"after_apply-{order}.md").read_text(encoding="utf-8")
    assert task.count("\n") == 2 and "## Шаги" not in task.splitlines()


def test_confirm_asks_before_subagent(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project)
    ext.config_path(project).write_text("[demo]\nconfirm = true\n", encoding="utf-8")
    ext_cli(project, capsys, "enable", "demo")
    _, _, out = applied(project, capsys)
    assert "- Демо (demo): спроси пользователя: «Сделать демо?» Только после явного «да» запусти" in out


def test_failing_script_hook_does_not_break_revert(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project, hook_py="import sys\nprint('сломалось', file=sys.stderr)\nsys.exit(3)\n")
    ext_cli(project, capsys, "enable", "demo")
    run_dir, _, _ = applied(project, capsys)
    code, out = _run(["revert", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: reverted")
    assert "- Демо (demo): не сработало (сломалось). Покажи пользователю эту строку." in out


def test_hanging_script_hook_times_out(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project, extension_toml=MANIFEST.replace("timeout = 5", "timeout = 1"),
              hook_py="import time\ntime.sleep(5)\n")
    ext_cli(project, capsys, "enable", "demo")
    run_dir, _, _ = applied(project, capsys)
    code, out = _run(["revert", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and "не уложился в 1 с" in out


def test_broken_enabled_extension_is_reported_not_fatal(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project)
    ext_cli(project, capsys, "enable", "demo")
    (project / ext.PROJECT_DIRNAME / "demo" / "act.py").unlink()
    _, _, out = applied(project, capsys)
    assert out.startswith("STATUS: applied")
    assert "- Расширения не работают: расширение demo включено, но не работает: actions.act: нет " \
           "файла act.py." in out


def test_after_report_fires_once_per_report(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project)
    ext_cli(project, capsys, "enable", "demo")
    run_dir, _, _ = _prepare(project, capsys)
    entries = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["clusters"]
    order, login = [entry["file_id"] for entry in entries if not entry["auto"]]
    out = _finish(run_dir, capsys, {order: TEST_ANALYSIS.replace("тест —", "окружение —"),
                                    login: LOGIN_ANALYSIS})
    assert "- Демо (demo) — покажи пользователю как есть:\n  событие after_report, проблема None, " \
           f"проблем в отчёте {len(entries)}" in out
    context = json.loads((run_dir / "extensions" / "demo" / "after_report.json")
                         .read_text(encoding="utf-8"))
    assert "===" not in context["console"] and context["console"].strip()
    assert [problem["number"] for problem in context["problems"]] == list(range(1, len(entries) + 1))

    assert ext.BLOCK_HEADING not in _run(["next", str(run_dir)], capsys)[1]  # тот же отчёт
    code, out = _run(["extensions", "run", "demo", "--run", str(run_dir)], capsys)
    assert code == 0 and "событие after_report" in out  # повтор по просьбе


def test_run_repeats_after_apply_only_for_applied_fix(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project)
    ext_cli(project, capsys, "enable", "demo")
    run_dir, order, _ = applied(project, capsys)
    code, out = _run(["extensions", "run", "demo", "1", "--run", str(run_dir)], capsys)
    assert code == 0 and "subagent_type «alla-demo»" in out
    _run(["revert", "1", "--run", str(run_dir)], capsys)
    code, out = _run(["extensions", "run", "demo", "1", "--run", str(run_dir)], capsys)
    assert code == 1 and "сейчас не применена" in out
    code, out = _run(["extensions", "run", "other", "1", "--run", str(run_dir)], capsys)
    assert code == 1 and "не включено" in out


def test_action_runs_script_with_context(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project)
    ext_cli(project, capsys, "enable", "demo")
    run_dir, order, _ = applied(project, capsys)
    context = run_dir / "extensions" / "demo" / f"after_apply-{order}.json"
    code, out = ext_cli(project, capsys, "action", "demo", "act", "--context", str(context),
                        "--", "--flag", "x")
    assert code == 0 and out == "STATUS: ok\nсделано для проблемы 1, аргументы ['--flag', 'x']\n"

    code, out = ext_cli(project, capsys, "action", "demo", "nope", "--context", str(context))
    assert code == 1 and "нет действия nope" in out
    stranger = project / "context.json"
    stranger.write_text(context.read_text(encoding="utf-8"), encoding="utf-8")
    code, out = ext_cli(project, capsys, "action", "demo", "act", "--context", str(stranger))
    assert code == 1 and "не файл расширения demo из папки разбора" in out

    (project / ext.PROJECT_DIRNAME / "demo" / "act.py").write_text("print('готово')\n", encoding="utf-8")
    code, out = ext_cli(project, capsys, "action", "demo", "act", "--context", str(context))
    assert code == 1 and "без строки STATUS: ok|error" in out and "готово" in out

    ext_cli(project, capsys, "disable", "demo")
    code, out = ext_cli(project, capsys, "action", "demo", "act", "--context", str(context))
    assert code == 1 and "не включено" in out


def test_prepare_installs_agents_of_enabled_extensions(project: Path, testops: FakeTestOps, capsys) -> None:
    make_demo(project)
    ext.set_enabled(project, "demo", True)
    _prepare(project, capsys)
    assert (project / ".qwen" / "agents" / "alla-demo.md").read_text(encoding="utf-8") == AGENT


def test_session_rules_allow_only_extension_text_files() -> None:
    """Субагент расширения пишет сообщение коммита; контекст и задание — только скрипт."""
    from alla_skill_lib.session_rules import WRITABLE_RE

    run = "/p/alla-reports/777-20261014-100000/extensions/git-publish"
    assert WRITABLE_RE.search(f"{run}/after_apply-01.message.txt")
    assert not WRITABLE_RE.search(f"{run}/after_apply-01.json")
    assert not WRITABLE_RE.search(f"{run}/after_apply-01.md")
    assert not WRITABLE_RE.search("/p/alla-extensions/config.toml")
