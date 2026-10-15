"""Встроенное расширение sut-code на настоящем git: репозиторий приложения, ревизии, проверка."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from skill_fixtures import (  # noqa: F401
    project_fixture,
    testops_fixture,
    without_libmagic,
)
from skill_fake_testops import FakeTestOps
from test_ext_git_publish import LOGIN_ANALYSIS, isolated_git  # noqa: F401
from test_skill_flow import VALID_ANALYSIS, _finish, _prepare, _run

from alla_skill_lib import cli
from alla_skill_lib import extensions as ext
from alla_skill_lib import workspace as ws

BUNDLED = Path(cli.__file__).resolve().parents[2] / "extensions"
SCRIPTS = BUNDLED / "sut-code" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import check  # noqa: E402
import sutlib  # noqa: E402

SERVICE = "src/main/java/ru/company/OrderService.java"
SERVICE_OK = """\
package ru.company;

public class OrderService {
    private final Repository repository = new Repository();

    public Order create(Request request) {
        Customer customer = request.customer();
        if (customer == null) {
            return Order.rejected("no customer");
        }
        return repository.save(new Order(customer));
    }
}
"""
SERVICE_BUG = """\
package ru.company;

public class OrderService {
    private final Repository repository = new Repository();

    public Order create(Request request) {
        Customer customer = request.customer();
        // Проверку убрали при рефакторинге: пустой customer — исключение
        if (customer == null)
            throw new NullPointerException("customer is null");
        return repository.save(new Order(customer));
    }
}
"""
SERVICE_FIXED = SERVICE_BUG.replace(
    '            throw new NullPointerException("customer is null");',
    '            return Order.rejected("no customer");')
ANALYSIS = """\
РЕВИЗИЯ: orders v1.4.2 ({sha})
ВЫВОД: приложение — OrderService.create бросает NPE, если в запросе нет customer
МЕСТО: src/main/java/ru/company/OrderService.java:10 — throw при пустом customer
КОД:
- src/main/java/ru/company/OrderService.java:10 «throw new NullPointerException("customer is null");»
СВЯЗЬ С ПАДЕНИЕМ: [S3] «java.lang.NullPointerException: customer is null» — лог показывает этот throw
ИСТОРИЯ: {bug} 2026-08-20 — рефакторинг убрал проверку customer
КАК ИСПРАВИТЬ:
1. Вернуть проверку customer в OrderService.create и отвечать 400.
ДЕФЕКТ: заводить — ошибка приложения, в релизе 1.4.2 не исправлена
ЗАГОЛОВОК ДЕФЕКТА: OrderService.create падает с NPE при запросе без customer
"""


def git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


def commit(repo: Path, text: str, message: str, date: str) -> str:
    target = repo / SERVICE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, date=date)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(name="sut")
def sut_fixture(tmp_path: Path) -> dict[str, Any]:
    """Репозиторий приложения: проверка → рефакторинг с NPE (v1.4.2, release/1.4) → исправление.

    Прогон фейкового TestOps запущен 2026-09-01: приблизительная ревизия — коммит с NPE.
    """
    work = tmp_path / "orders-src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "config", "user.name", "Разработчик")
    git(work, "config", "user.email", "dev@example.com")
    first = commit(work, SERVICE_OK, "feat: создание заказа", "2026-08-01T10:00:00Z")
    bug = commit(work, SERVICE_BUG, "refactor: упростить OrderService", "2026-08-20T10:00:00Z")
    git(work, "tag", "v1.4.2")
    git(work, "branch", "release/1.4")
    fix = commit(work, SERVICE_FIXED, "fix: пустой customer снова даёт отказ", "2026-09-10T10:00:00Z")
    remote = tmp_path / "orders.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(remote)], check=True)
    git(remote, "config", "uploadpack.allowFilter", "true")
    return {"url": str(remote), "first": first, "bug": bug, "fix": fix}


def enable(project: Path, capsys: pytest.CaptureFixture[str], **settings: Any) -> None:
    shutil.copytree(BUNDLED / "sut-code", ws.SKILL_DIR / "extensions" / "sut-code")
    code, out = _run(["extensions", "enable", "sut-code", "--project-root", str(project)], capsys)
    assert code == 0, out
    config = ext.config_path(project)
    lines = ["", "[sut-code]"] + [f"{key} = {json.dumps(value)}" for key, value in settings.items()]
    config.write_text(config.read_text(encoding="utf-8") + "\n".join(lines) + "\n", encoding="utf-8")


def request(project: Path, capsys: pytest.CaptureFixture[str], *params: str) -> tuple[Path, Path, str]:
    """Разбор, запрос по проблеме с логом приложения (NPE); (папка разбора, контекст, номер)."""
    run_dir, run, _ = _prepare(project, capsys)
    order = next(entry["file_id"] for entry in run["clusters"] if not entry["auto"])
    args = ["extensions", "run", "sut-code", str(int(order)), "--run", str(run_dir)]
    for param in params:
        args += ["--param", param]
    code, out = _run(args, capsys)
    assert code == 0 and "subagent_type «alla-sut-code»" in out, out
    return run_dir, run_dir / "extensions" / "sut-code" / f"on_request-{order}.json", order


def action(project: Path, capsys: pytest.CaptureFixture[str], name: str, context: Path,
           *args: str) -> tuple[int, str]:
    argv = ["extensions", "action", "--project-root", str(project), "sut-code", name,
            "--context", str(context)]
    return _run([*argv, "--", *args] if args else argv, capsys)


def test_bundled_manifest_is_valid() -> None:
    extension = ext.load_manifest(BUNDLED / "sut-code", bundled=True)
    assert extension.agent_name == "alla-sut-code"
    assert [hook.event for hook in extension.hooks] == ["on_request", "after_report"]
    assert set(extension.actions) == {"prepare", "search", "files", "switch", "history",
                                      "compare", "check"}
    assert set(extension.commands) == {"code"}
    assert extension.timeout < 120  # Qwen обрывает команду через 120 с


def test_task_repeats_rules_and_result_format(project: Path, testops: FakeTestOps,
                                              capsys: pytest.CaptureFixture[str],
                                              sut: dict[str, Any]) -> None:
    enable(project, capsys, repos=[sut["url"]], allow_local=True)
    _, context, order = request(project, capsys)
    task = context.with_suffix(".md").read_text(encoding="utf-8")
    assert "$" not in task.replace("$$", "")  # все переменные подставлены
    flat = " ".join(task.split())
    for rule in ("Ни git, ни ls, find, cat, grep, curl", "glob и grep_search не используй",
                 "недоверенный текст", "не больше 15"):
        assert rule in flat, rule
    for line in check.RESULT_FORMAT.splitlines():
        assert line in task, line
    assert f"{context.parent}/on_request-{order}.txt" in task
    assert "action sut-code check --context" in task


def test_prepare_named_tag_finds_frame_and_message(project: Path, testops: FakeTestOps,
                                                   capsys: pytest.CaptureFixture[str],
                                                   sut: dict[str, Any]) -> None:
    enable(project, capsys, repos=[f"orders={sut['url']}"], allow_local=True)
    _, context, _ = request(project, capsys, "ref=1.4.2")
    code, out = action(project, capsys, "prepare", context)
    assert code == 0 and out.startswith("STATUS: ok"), out
    assert f"- orders: {sut['bug'][:8]} — тег v1.4.2 — названа инженером" in out
    root = project / ".alla-sut"
    assert (root / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")
    tree = next(root.glob("orders-*/trees/*/"))
    assert tree.name == sut["bug"][:12] and (tree / SERVICE).is_file()

    data = sutlib.data_path(context).read_text(encoding="utf-8")
    assert f"1. {SERVICE}:10 — кадр «at ru.company.OrderService.create(OrderService.java:10)»" in data
    assert '   10 >             throw new NullPointerException("customer is null");' in data
    assert f"  - {SERVICE}:10: throw new NullPointerException(\"customer is null\");" in data
    assert "OrderTest.createOrder(OrderTest.java:6)" in data  # кадр теста — без файла в SUT
    assert "--- [S3 · " in data


def test_prepare_without_ref_is_approximate_by_launch_date(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str], sut: dict[str, Any]
) -> None:
    enable(project, capsys, repos=[sut["url"]], allow_local=True)
    _, context, _ = request(project, capsys, "ref=2.0")
    code, out = action(project, capsys, "prepare", context)
    assert code == 0, out
    # Прогон запущен 2026-09-01: в main тогда был коммит с NPE, исправление — позже.
    assert f"{sut['bug'][:8]} — приблизительно: ветка main на момент запуска прогона" in out
    assert "нет ревизии «2.0»" in out and "назови её" in out
    context.with_suffix(".txt").write_text(
        ANALYSIS.format(sha=sut["bug"][:8], bug=sut["bug"][:8]), encoding="utf-8")
    code, out = action(project, capsys, "check", context)
    # Подмену версии и приблизительную ревизию пользователь видит до подтверждения.
    assert code == 0 and "Внимание: в orders нет ревизии «2.0»" in out, out
    assert "Внимание: версия приложения выбрана приблизительно" in out


def test_actions_search_files_history_compare_switch(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str], sut: dict[str, Any]
) -> None:
    enable(project, capsys, repos=[sut["url"]], allow_local=True)
    _, context, _ = request(project, capsys, "ref=release/1.4")
    assert action(project, capsys, "prepare", context)[0] == 0

    code, out = action(project, capsys, "search", context, "Customer", "IS", "null")
    assert code == 0 and f"- {SERVICE}:10: throw new" in out
    assert "не найден" in action(project, capsys, "search", context, "nothing here")[1]
    code, out = action(project, capsys, "files", context, "orderservice")
    assert code == 0 and out.rstrip().endswith(f"/{SERVICE}")

    code, out = action(project, capsys, "history", context, f"{SERVICE}:10")
    assert code == 0 and "refactor: упростить OrderService" in out
    code, out = action(project, capsys, "compare", context, SERVICE)
    assert code == 0 and "fix: пустой customer снова даёт отказ" in out

    code, out = action(project, capsys, "switch", context, "main")
    assert code == 0 and "ветка main" in out and sut["fix"][:8] in out
    state = json.loads(sutlib.state_path(context).read_text(encoding="utf-8"))
    assert state["trees"][0]["sha"] == sut["fix"]
    assert f"({sut['fix'][:8]})" in sutlib.data_path(context).read_text(encoding="utf-8")

    for name, args, error in (("history", ("../../etc/passwd",), "вне репозитория"),
                              ("switch", ("--upload-pack=x",), "не годится"),
                              ("switch", ("nope",), "нет ревизии «nope»"),
                              ("search", ("ab",), "не короче 3")):
        code, out = action(project, capsys, name, context, *args)
        assert code == 1 and error in out, (name, out)


def test_check_accepts_valid_analysis_and_writes_finding(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str], sut: dict[str, Any]
) -> None:
    enable(project, capsys, repos=[sut["url"]], allow_local=True)
    _, context, order = request(project, capsys, "ref=v1.4.2")
    assert action(project, capsys, "prepare", context)[0] == 0
    context.with_suffix(".txt").write_text(
        ANALYSIS.format(sha=sut["bug"][:8], bug=sut["bug"][:8]), encoding="utf-8")
    code, out = action(project, capsys, "check", context)
    assert code == 0 and out.startswith("STATUS: ok"), out
    assert "приложение: OrderService.create бросает NPE" in out
    assert out.rstrip().endswith("Подтверди вывод («да, верно») — сохраню его в отчёт и базу "
                                 "знаний; «нет» — не сохраню.")
    assert f"Ревизия: {Path(sut['url']).stem} v1.4.2 ({sut['bug'][:8]}) — тег v1.4.2 — названа инженером" in out
    assert "Внимание:" not in out

    finding = json.loads(context.with_suffix(".finding.json").read_text(encoding="utf-8"))
    assert finding["problem"] == int(order) and finding["format_ok"] is True
    assert finding["category"] == "приложение"
    assert finding["title"] == "OrderService.create падает с NPE при запросе без customer"
    assert finding["cause"].endswith("(OrderService.java:10)")
    assert finding["fix"] == ["Вернуть проверку customer в OrderService.create и отвечать 400."]
    assert finding["kb_fields"] == {"repo": "orders", "revision": "v1.4.2",
                                    "commit": sut["bug"][:12], "location": f"{SERVICE}:10",
                                    "defect_title": finding["title"]}
    assert any(row.startswith("Связь с падением: [S3]") for row in finding["details"])
    result = json.loads(context.with_suffix(".result.json").read_text(encoding="utf-8"))
    assert result["defect"]["needed"] and result["commits"] == [sut["bug"][:8]]
    assert "## Разбор целиком" in context.with_suffix(".report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(("change", "error"), [
    (("«throw new NullPointerException", "«throw new IllegalStateException"), "нет в src/main"),
    (("OrderService.java:10 «", "OrderService.java:3 «"), "рядом со строкой 3"),
    (("[S3] «java.lang", "[S9] «java.lang"), "куска S9 нет"),
    (("«java.lang.NullPointerException: customer is null»", "«customer was null»"), "цитаты нет"),
    (("ВЫВОД: приложение", "ВЫВОД: баг"), "одна категория"),
    (("OrderService.java:10 — throw", "Missing.java:10 — throw"), "Missing.java нет в ревизии"),
    (("ЗАГОЛОВОК ДЕФЕКТА: OrderService.create падает с NPE при запросе без customer\n", ""),
     "нужен ЗАГОЛОВОК ДЕФЕКТА"),
    (("ИСТОРИЯ: {bug}", "ИСТОРИЯ: deadbeef0"), "коммита deadbeef0"),
])
def test_check_rejects_unverifiable_claims(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str], sut: dict[str, Any],
    change: tuple[str, str], error: str,
) -> None:
    enable(project, capsys, repos=[sut["url"]], allow_local=True)
    _, context, _ = request(project, capsys, "ref=v1.4.2")
    assert action(project, capsys, "prepare", context)[0] == 0
    text = ANALYSIS.replace(*change).format(sha=sut["bug"][:8], bug=sut["bug"][:8])
    context.with_suffix(".txt").write_text(text, encoding="utf-8")
    code, out = action(project, capsys, "check", context)
    assert code == 1 and "Попытка 1 из 3" in out and error in out, out
    assert not context.with_suffix(".finding.json").exists()

    assert "последняя" in action(project, capsys, "check", context)[1]
    code, out = action(project, capsys, "check", context)  # третья — с пометкой
    assert code == 0 and "сохранить его в отчёт и базу знаний нельзя" in out, out
    finding = json.loads(context.with_suffix(".finding.json").read_text(encoding="utf-8"))
    assert finding["format_ok"] is False


def test_check_unknown_needs_no_code(project: Path, testops: FakeTestOps,
                                     capsys: pytest.CaptureFixture[str], sut: dict[str, Any]) -> None:
    enable(project, capsys, repos=[sut["url"]], allow_local=True)
    _, context, _ = request(project, capsys)
    assert action(project, capsys, "prepare", context)[0] == 0
    context.with_suffix(".txt").write_text(
        f"РЕВИЗИЯ: orders main ({sut['bug'][:8]})\n"
        "ВЫВОД: неизвестно — в коде нет места, где формируется ответ 500\n"
        "ИСТОРИЯ: нет данных\nКАК ИСПРАВИТЬ:\n1. Добавить лог сервиса заказов.\n"
        "ДЕФЕКТ: не заводить — причина не найдена\n", encoding="utf-8")
    code, out = action(project, capsys, "check", context)
    assert code == 0 and "сохранять в отчёт и базу знаний нечего" in out, out


def test_errors_without_repository_and_bad_urls(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str], sut: dict[str, Any]
) -> None:
    enable(project, capsys)
    _, context, _ = request(project, capsys)
    code, out = action(project, capsys, "prepare", context)
    assert code == 1 and "Не знаю, где код приложения" in out and "/alla:code N <ссылка>" in out
    for url in (sut["url"], "https://user:token@git.example.com/app.git", "-uhack",
                "ext::sh -c touch% /tmp/pwned", "ftp://git.example.com/app.git"):
        _, context, _ = request(project, capsys, f"repo={url}")
        code, out = action(project, capsys, "prepare", context)
        assert code == 1 and "STATUS: error" in out, (url, out)
    assert not list((project / ".alla-sut").glob("*/repo.git"))


@pytest.mark.parametrize(("url", "ok"), [
    ("https://git.example.com/team/app.git", True),
    ("ssh://git@git.example.com:7999/team/app.git", True),
    ("git@git.example.com:team/app.git", True),
    ("https://user@git.example.com/app.git", True),
    ("https://user:secret@git.example.com/app.git", False),
    ("-u/tmp/x", False),
    ("ext::sh -c id", False),
    ("/srv/git/app.git", False),
    ("file:///srv/git/app.git", False),
    ("git.example.com/app", False),
])
def test_url_validation(url: str, ok: bool) -> None:
    if ok:
        sutlib.check_url(url, allow_local=False)
    else:
        with pytest.raises(sutlib.SutError):
            sutlib.check_url(url, allow_local=False)


def test_repo_names() -> None:
    assert sutlib.parse_repo_spec("orders=ssh://git@h/x/app.git") == ("orders", "ssh://git@h/x/app.git")
    assert sutlib.parse_repo_spec("ssh://git@h/x/Billing-Service.git") == (
        "billing-service", "ssh://git@h/x/Billing-Service.git")
    assert sutlib.parse_repo_spec("git@h:team/app.git")[0] == "app"


def test_frames_of_several_languages() -> None:
    text = "\n".join([
        "\tat ru.company.OrderService.create(OrderService.java:10)",
        "\tat java.base/java.util.Objects.requireNonNull(Objects.java:233)",
        '  File "/app/orders/service.py", line 42, in create',
        '  File "/usr/lib/python3.11/site-packages/flask/app.py", line 1, in run',
        "    at createOrder (/srv/app/src/orders/service.ts:17:9)",
        "    at node_modules/express/lib/router.js:5:1",
        "   at Shop.Orders.Create() in C:\\src\\Shop\\Orders.cs:line 31",
        "\t/go/src/shop/orders/handler.go:88 +0x1d",
        "#0 /var/www/src/Orders/Service.php(12): create()",
    ])
    frames = [(frame.path, frame.line) for frame in sutlib.frames([{"text": text}])]
    assert frames == [("ru/company/OrderService.java", 10), ("/app/orders/service.py", 42),
                      ("/srv/app/src/orders/service.ts", 17), ("C:/src/Shop/Orders.cs", 31),
                      ("/go/src/shop/orders/handler.go", 88), ("/var/www/src/Orders/Service.php", 12)]
    files = {"service.py": ["orders/service.py", "billing/service.py"]}
    assert sutlib.match_frame(sutlib.Frame("/app/orders/service.py", 1, ""), files) == "orders/service.py"
    assert sutlib.messages([{"kind": "log", "text":
        "2026-09-01 [ERROR] OrderService: failed to create order 42\n"
        "java.lang.NullPointerException: customer is null"}]) == [
        "failed to create order", "customer is null"]


def test_first_clone_runs_in_background(tmp_path: Path, sut: dict[str, Any]) -> None:
    repo = sutlib.Repo("orders", sut["url"], tmp_path / ".alla-sut" / "orders-x")
    with pytest.raises(sutlib.SutPending, match="ещё идёт"):
        sutlib.ensure_clone(repo, time.monotonic() - 1, allow_local=True)  # ждать некогда
    assert not repo.git_dir.exists()
    assert sutlib.ensure_clone(repo, time.monotonic() + 30, allow_local=True) is True
    assert (repo.git_dir / "HEAD").is_file() and not (repo.folder / "repo.tmp").exists()
    assert sutlib.ensure_clone(repo, time.monotonic() + 30, allow_local=True) is False
    sha = sutlib.resolve_ref(repo, "1.4.2")
    assert sha is not None and sha[0] == sut["bug"] and sha[1] == "тег v1.4.2"
    tree = sutlib.ensure_tree(repo, sut["bug"], time.monotonic() + 30, allow_local=True)
    assert (tree / SERVICE).read_text(encoding="utf-8") == SERVICE_BUG


def test_failed_clone_is_reported(tmp_path: Path) -> None:
    repo = sutlib.Repo("ghost", str(tmp_path / "missing.git"), tmp_path / ".alla-sut" / "ghost-x")
    with pytest.raises(sutlib.SutError, match="не удалось"):
        sutlib.ensure_clone(repo, time.monotonic() + 30, allow_local=True)
    assert not repo.git_dir.exists() and not (repo.folder / "jobs" / "clone").exists()


def test_hint_after_report(project: Path, testops: FakeTestOps,
                          capsys: pytest.CaptureFixture[str]) -> None:
    enable(project, capsys)
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    out = _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: LOGIN_ANALYSIS})
    number = int(order)
    assert f"причину может показать код приложения — разобрать по коду: /alla:code {number}" in out
    assert "repos = [\"<ссылка>\"]" in out


def confirmed_run(project: Path, capsys: pytest.CaptureFixture[str], sut: dict[str, Any],
                  analysis: str = ANALYSIS) -> tuple[Path, str]:
    """Разбор до done, вывод sut-code по проблеме с NPE проверен; (папка разбора, номер)."""
    enable(project, capsys, repos=[sut["url"]], allow_local=True)
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: LOGIN_ANALYSIS})
    code, out = _run(["extensions", "run", "sut-code", str(int(order)), "--run", str(run_dir),
                      "--param", "ref=v1.4.2"], capsys)
    assert code == 0, out
    context = run_dir / "extensions" / "sut-code" / f"on_request-{order}.json"
    assert action(project, capsys, "prepare", context)[0] == 0
    context.with_suffix(".txt").write_text(
        analysis.format(sha=sut["bug"][:8], bug=sut["bug"][:8]), encoding="utf-8")
    action(project, capsys, "check", context)
    return run_dir, order


def test_confirmed_finding_goes_to_kb_and_report(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str], sut: dict[str, Any]
) -> None:
    run_dir, order = confirmed_run(project, capsys, sut)
    number = str(int(order))
    code, out = _run(["remember", number, "--run", str(run_dir), "--from-extension", "sut-code"],
                     capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    assert "Вывод добавлен в отчёт у проблемы" in out and "next" in out

    record_path = next((project / "alla-kb").glob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["category"] == "service"
    assert record["title"] == "OrderService.create падает с NPE при запросе без customer"
    assert record["resolution_steps"] == [
        "Вернуть проверку customer в OrderService.create и отвечать 400."]
    assert record["sut-code"]["commit"] == sut["bug"][:12]
    assert record["sut-code"]["location"] == f"{SERVICE}:10"
    confirmed = json.loads((run_dir / "confirmed" / f"{order}.json").read_text(encoding="utf-8"))
    assert confirmed["extension"] == "sut-code" and confirmed["kb_entry"] == record["id"]

    out = _run(["next", str(run_dir)], capsys)[1]
    if out.startswith("STATUS: summary"):  # подтверждённый вывод — новые данные сводки
        assert "подтверждено инженером по коду приложения: приложение" in \
            (run_dir / "summary_task.md").read_text(encoding="utf-8")
        (run_dir / "summary.md").write_text("Итог прогона.", encoding="utf-8")
        out = _run(["next", str(run_dir)], capsys)[1]
    assert out.startswith("STATUS: done"), out
    assert "[подтверждено по коду]" in out
    assert "Подтверждено:  [ПРИЛОЖЕНИЕ] OrderService.create бросает NPE" in out
    # Подсказка после отчёта больше не предлагает подтверждённую проблему.
    assert f"/alla:code {number} " not in out
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "- **Подтверждено инженером** (разбор по коду приложения, " in report
    assert f"   - Место: {SERVICE}:10 — throw при пустом customer" in report
    assert "   1. Вернуть проверку customer в OrderService.create и отвечать 400." in report


@pytest.mark.parametrize(("analysis", "error"), [
    (ANALYSIS.replace("ВЫВОД: приложение", "ВЫВОД: неизвестно"), "сохранить можно только"),
    (ANALYSIS.replace("[S3]", "[S9]"), "не прошёл его проверку формата"),
])
def test_unsuitable_finding_is_not_saved(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str], sut: dict[str, Any],
    analysis: str, error: str,
) -> None:
    run_dir, order = confirmed_run(project, capsys, sut, analysis)
    context = run_dir / "extensions" / "sut-code" / f"on_request-{order}.json"
    for _ in range(2):  # неверный формат принимается только с третьей попытки
        action(project, capsys, "check", context)
    code, out = _run(["remember", str(int(order)), "--run", str(run_dir),
                      "--from-extension", "sut-code"], capsys)
    assert code == 1 and error in out and "remember не повторяй" in out, out
    assert not (run_dir / "confirmed").exists() and not list((project / "alla-kb").glob("*.json"))


def test_remember_without_finding(project: Path, testops: FakeTestOps,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order = next(entry["file_id"] for entry in run["clusters"] if not entry["auto"])
    code, out = _run(["remember", str(int(order)), "--run", str(run_dir),
                      "--from-extension", "sut-code"], capsys)
    assert code == 1 and "нет вывода по проблеме" in out
    with pytest.raises(SystemExit):
        cli.main(["remember", "1", "--run", str(run_dir), "--from-extension", "sut-code",
                  "--from-analysis"])
    assert "not allowed with argument" in capsys.readouterr().out


def test_stand_scenario_x03_is_solvable_by_search(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Стенд X03 без модели: стека приложения нет, приблизительная ревизия — коммит контракта
    v2 (до запуска прогона), и строку лога «responded 201 Created» находит действие search."""
    from qwen_stand import SUT_CONTROLLER, build_sut_repo
    from skill_fake_testops import test_bug_launch

    testops.fixture = test_bug_launch()
    bare = tmp_path / "stand-sut.git"
    build_sut_repo(tmp_path / "sut-src", bare)
    enable(project, capsys, repos=[f"orders={bare}"], allow_local=True)
    run_dir, run, _ = _prepare(project, capsys, launch_id=782)
    order = run["clusters"][0]["file_id"]
    code, out = _run(["extensions", "run", "sut-code", str(int(order)), "--run", str(run_dir)],
                     capsys)
    assert code == 0, out
    context = run_dir / "extensions" / "sut-code" / f"on_request-{order}.json"
    code, out = action(project, capsys, "prepare", context)
    assert code == 0 and "приблизительно: ветка main на момент запуска прогона" in out, out
    data = sutlib.data_path(context).read_text(encoding="utf-8")
    assert "Кадров стека с файлами этого приложения в данных нет." in data
    code, out = action(project, capsys, "search", context, "responded 201 Created")
    assert code == 0 and f"- {SUT_CONTROLLER}:" in out, out
    code, out = action(project, capsys, "history", context, SUT_CONTROLLER)
    assert "POST /orders отвечает 201 Created" in out, out


def test_repeat_request_drops_the_previous_finding_and_accepts_full_paths(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str], sut: dict[str, Any]
) -> None:
    enable(project, capsys, repos=[sut["url"]], allow_local=True)
    _, context, _ = request(project, capsys, "ref=origin/release/1.4")
    code, out = action(project, capsys, "prepare", context)
    assert code == 0 and "ветка release/1.4 — названа инженером" in out, out
    tree = json.loads(sutlib.state_path(context).read_text(encoding="utf-8"))["trees"][0]["tree"]
    # Место полным путём из файла данных — тоже место в ревизии.
    text = ANALYSIS.format(sha=sut["bug"][:8], bug=sut["bug"][:8]).replace(
        f"МЕСТО: {SERVICE}:10", f"МЕСТО: {tree}/{SERVICE}:10")
    context.with_suffix(".txt").write_text(text, encoding="utf-8")
    code, out = action(project, capsys, "check", context)
    assert code == 0, out
    assert context.with_suffix(".finding.json").is_file()
    assert action(project, capsys, "prepare", context)[0] == 0  # новый запрос по той же проблеме
    assert not context.with_suffix(".finding.json").exists()
    assert not context.with_suffix(".txt").exists()
