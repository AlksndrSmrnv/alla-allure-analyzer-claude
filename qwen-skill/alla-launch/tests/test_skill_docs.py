"""SKILL.md и справочники: ссылки, шаблоны из кода, примеры против парсеров, правила исполнителя.

Справочники в ``references/`` — то, по чему агент пишет свои файлы. Они не должны
расходиться с кодом: шаблоны вставлены из констант дословно, а примеры с пометкой
``<!-- example: <вид> -->`` проходят настоящие разбор и проверку скилла.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from skill_fake_testops import FakeTestOps
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from test_skill_flow import MARKDOWN_ANALYSIS, PROPOSAL, TEST_ANALYSIS, _next, _prepare
from test_skill_parallel import small_batches_fixture  # noqa: F401

from alla_skill_lib.agent_rules import EXECUTOR_RULES, PROBLEM_PREFIX
from alla_skill_lib.analysis_format import EXPECTED_FORMAT, parse_analysis, validate_analysis
from alla_skill_lib.cli import (
    FORMAT_MISMATCH_NOTE,
    LAST_ATTEMPT_ANALYSIS,
    LAST_ATTEMPT_PROPOSAL,
    PROPOSAL_FORMAT,
)
from alla_skill_lib.feedback import FEEDBACK_FORMAT
from alla_skill_lib.proposals import parse_proposal, validate_proposal

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES = SKILL_DIR / "references"
LIB = SKILL_DIR / "scripts" / "alla_skill_lib"

STATUSES = (
    "analyze", "analyze_batch", "fix", "propose", "summary", "done", "diff", "applied",
    "reverted", "saved", "ok", "ready", "setup_required", "error",
)
_EXAMPLE_RE = re.compile(
    r"<!--\s*example:\s*(?P<kind>[\w-]+)\s*(?:\|\s*(?P<arg>.*?))?\s*-->[ \t]*\n"
    r"(?P<fence>`{3,})[^\n]*\n(?P<body>.*?)\n(?P=fence)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _examples(reference: str, kind: str) -> list[tuple[str | None, str]]:
    """(подстрока ошибки из метки, текст примера) для примеров вида ``kind``."""
    found = [
        (match.group("arg"), match.group("body") + "\n")
        for match in _EXAMPLE_RE.finditer(_read(REFERENCES / reference))
        if match.group("kind") == kind
    ]
    assert found, f"в {reference} нет примеров вида {kind}"
    return found


def _text_block(text: str, first_line: str) -> str:
    """Содержимое fenced-блока ``text``, который начинается со строки ``first_line``."""
    match = re.search(rf"```text\n(?P<body>{re.escape(first_line)}\n.*?)```", text, re.DOTALL)
    assert match, f"нет блока, начинающегося с «{first_line}»"
    return match.group("body")


# --- SKILL.md и ссылки --------------------------------------------------------------


def test_skill_md_has_front_matter() -> None:
    text = _read(SKILL_MD)
    assert text.startswith("---\nname: alla-launch\ndescription: ")
    assert text.split("---", 2)[1].count("\n") == 3  # только name и description


def test_skill_md_links_resolve_and_cover_every_reference() -> None:
    linked = set(re.findall(r"\]\((references/[^)#]+)\)", _read(SKILL_MD)))
    assert linked, "в SKILL.md нет ссылок на справочники"
    for link in linked:
        assert (SKILL_DIR / link).is_file(), f"SKILL.md ссылается на несуществующий {link}"
    on_disk = {f"references/{path.name}" for path in REFERENCES.glob("*.md")}
    assert on_disk == linked, "справочник не упомянут в SKILL.md или ссылка ведёт мимо"


def test_reference_links_inside_references_resolve() -> None:
    for path in REFERENCES.glob("*.md"):
        for link in re.findall(r"`(references/[\w./-]+\.md)`", _read(path)):
            assert (SKILL_DIR / link).is_file(), f"{path.name} называет несуществующий {link}"


def test_skill_md_states_the_executor_rules() -> None:
    text = _read(SKILL_MD)
    for heading in ("## Правила исполнителя", "## Форматы файлов", "## Проблемы скилла"):
        assert heading in text
    rules = text.split("## Правила исполнителя", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "только для чтения", "Своих скриптов не пиши", "python -c", "heredoc", "write_file",
        "не обходи", "Обходной путь не изобретай",
    ):
        assert phrase in rules, f"в «Правилах исполнителя» нет: {phrase}"
    # Слова, на которых держится запрет, есть и в тексте заданий, и в SKILL.md.
    # «Папки не просматривай»: без явного запрета субагенты на быстрой модели начинали с
    # ls/find по папке разбора и скилла (стенд Qwen, P04).
    for keyword in ("write_file", "heredoc", "python -c", ".env", "run.json", "evidence/",
                    "Папки не просматривай (ls, find, cat", PROBLEM_PREFIX):
        assert keyword in EXECUTOR_RULES
        assert keyword in text


def test_fix_instructions_do_not_send_feedback_back_to_next() -> None:
    """`next` обратную связь не сохраняет: после fix по feedback нужен повторный remember."""
    skill = " ".join(_read(SKILL_MD).split())
    protocol = " ".join(_read(REFERENCES / "protocol.md").split())
    for text in (skill, protocol):
        assert "повторный `remember" in text and "Затем выполни" in text


def test_problem_block_template_is_identical_in_skill_md_and_reference() -> None:
    template = _text_block(_read(SKILL_MD), "Проблемы скилла:")
    assert template == _text_block(_read(REFERENCES / "problem-report.md"), "Проблемы скилла:")
    for key in ("Где:", "Ожидалось:", "Произошло:", "Что сделал:", "Предложение:"):
        assert key in template


# --- шаблоны из кода ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "template"),
    [
        ("analysis-format.md", EXPECTED_FORMAT),
        ("proposal-format.md", PROPOSAL_FORMAT),
        ("feedback.md", FEEDBACK_FORMAT),
    ],
)
def test_reference_embeds_the_template_from_code_verbatim(reference: str, template: str) -> None:
    assert f"```text\n{template}\n```" in _read(REFERENCES / reference)


def test_proposal_reference_keeps_the_unconditional_ban_on_longer_timeouts() -> None:
    """PROPOSAL_RULES запрещает рост таймаутов без исключений; справочник не должен их выдумывать."""
    from alla_skill_lib.cli import PROPOSAL_RULES

    assert "увеличивать таймауты" in " ".join(PROPOSAL_RULES.split())
    reference = " ".join(_read(REFERENCES / "proposal-format.md").split())
    assert "увеличивать таймауты" in reference and "запрет безусловный" in reference


def test_one_edit_per_proposal_is_stated_everywhere_the_agent_reads_about_proposals() -> None:
    """Ограничение «одно предложение — одна правка» было нигде не записано — агент пробовал две."""
    from alla_skill_lib.cli import PROPOSAL_RULES

    assert "Одно предложение — одна правка" in " ".join(PROPOSAL_RULES.split())
    for document in (
        REFERENCES / "proposal-format.md",
        REFERENCES / "protocol.md",
        SKILL_DIR / "SKILL.md",
    ):
        assert "одно предложение — одна правка" in " ".join(_read(document).split()).lower(), document


def test_protocol_lists_every_status_the_code_prints() -> None:
    protocol = _read(REFERENCES / "protocol.md")
    for status in STATUSES:
        assert f"`{status}`" in protocol or f"`STATUS: {status}`" in protocol, status
    printed = set()
    for path in [*LIB.glob("*.py"), SKILL_DIR / "scripts" / "alla_skill.py"]:
        printed |= set(re.findall(r'STATUS: ([a-z_]+)', _read(path)))
    assert printed <= set(STATUSES), f"в коде новый статус, которого нет в справочнике: {printed - set(STATUSES)}"


# --- примеры справочников против настоящих парсеров --------------------------------


_DATA_HEADER_RE = re.compile(r"^--- \[(?P<id>S\d+) · [^\n]*\] ---$", re.MULTILINE)


def _example_sources() -> dict[str, dict[str, str]]:
    """Реестр из образца «Данных» справочника (``<!-- example-data -->``)."""
    match = re.search(r"<!-- example-data -->\n```text\n(?P<body>.*?)\n```",
                      _read(REFERENCES / "analysis-format.md"), re.DOTALL)
    assert match, "в analysis-format.md нет образца данных"
    body = match.group("body")
    headers = list(_DATA_HEADER_RE.finditer(body))
    return {
        header.group("id"): {"text": body[header.end() + 1:(
            headers[index + 1].start() if index + 1 < len(headers) else len(body))].strip("\n")}
        for index, header in enumerate(headers)
    }


def _validate_example(text: str, project: Path) -> list[str]:
    return validate_analysis(parse_analysis(text), project, frozenset(), task_format=2,
                             sources=_example_sources())


def test_example_data_matches_the_task_header_format() -> None:
    sources = _example_sources()
    assert list(sources) == ["S1", "S2", "S3"]
    assert "customer is null" in sources["S3"]["text"]


def test_analysis_examples_that_are_ok_pass_validation(project) -> None:
    examples = _examples("analysis-format.md", "analysis-ok")
    assert len(examples) >= 4
    for _, text in examples:
        parsed = parse_analysis(text)
        assert _validate_example(text, project) == [], text
        assert parsed.category is not None


def test_analysis_examples_that_are_wrong_give_the_documented_error(project) -> None:
    examples = _examples("analysis-format.md", "analysis-error")
    assert len(examples) >= 9
    for expected, text in examples:
        errors = _validate_example(text, project)
        assert any(expected in error for error in errors), (expected, errors)


def test_proposal_examples_pass_and_fail_as_documented(project) -> None:
    (_, fix_text), = _examples("proposal-format.md", "proposal-fix")
    fix = parse_proposal(fix_text)
    assert fix.is_fix and validate_proposal(fix, project) == []
    (_, skip_text), = _examples("proposal-format.md", "proposal-skip")
    skip = parse_proposal(skip_text)
    assert skip.decision == "skip" and validate_proposal(skip, project) == []

    errors_examples = _examples("proposal-format.md", "proposal-error")
    assert len(errors_examples) >= 4
    for expected, text in errors_examples:
        errors = validate_proposal(parse_proposal(text), project)
        assert any(expected in error for error in errors), (expected, errors)


def test_feedback_example_parses_into_a_savable_recipe() -> None:
    (_, text), = _examples("feedback.md", "feedback-ok")
    parsed = parse_analysis(text)
    assert parsed.category in {"тест", "приложение", "окружение", "данные"}
    assert parsed.title and parsed.fix.strip() and parsed.fingerprint


def _summary_shape_problems(text: str) -> list[str]:
    paragraphs = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    problems = []
    if not 2 <= len(paragraphs) <= 4:
        problems.append("нужно 2–4 абзаца")
    if re.search(r"^\s*(#|[-*•]\s|\d+[.)]\s)", text, re.MULTILINE):
        problems.append("заголовки и списки не нужны")
    if re.search(r"кластер|сигнатур|трейс", text, re.IGNORECASE):
        problems.append("служебные слова")
    return problems


def test_summary_examples_show_the_documented_shape() -> None:
    for _, text in _examples("summary-format.md", "summary-ok"):
        assert _summary_shape_problems(text) == []
    for _, text in _examples("summary-format.md", "summary-bad"):
        assert len(_summary_shape_problems(text)) >= 2


# --- задания и ответы скрипта содержат правила исполнителя -------------------------


def _clusters(run: dict) -> tuple[str, str]:
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    return order, login


def test_cluster_task_carries_rules_and_reference(project, testops: FakeTestOps, capsys) -> None:
    run_dir, run, out = _prepare(project, capsys)
    order, _ = _clusters(run)
    task = _read(run_dir / "clusters" / f"{order}.md")
    assert EXECUTOR_RULES in task
    assert "references/analysis-format.md" in task
    assert out.startswith("STATUS: analyze\n") and "references/analysis-format.md" in out


def test_fix_output_points_to_the_reference_and_warns_before_the_last_attempt(
    project, testops: FakeTestOps, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, _ = _clusters(run)
    path = run_dir / "analyses" / f"{order}.md"
    path.write_text("ПРИЧИНА: баг\nбез формата", encoding="utf-8")
    first = _next(run_dir, capsys)
    assert first.startswith("STATUS: fix") and "попытка 1 из 3" in first
    assert "references/analysis-format.md" in first and FORMAT_MISMATCH_NOTE in first
    assert LAST_ATTEMPT_ANALYSIS not in first
    path.write_text("ПРИЧИНА: баг\nбез формата, версия 2", encoding="utf-8")
    second = _next(run_dir, capsys)
    assert "попытка 2 из 3" in second and LAST_ATTEMPT_ANALYSIS in second


def test_propose_fix_summary_and_done_outputs_point_to_their_references(
    project, testops: FakeTestOps, capsys
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = _clusters(run)
    (run_dir / "analyses" / f"{order}.md").write_text(TEST_ANALYSIS, encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: propose")
    assert "references/proposal-format.md" in out and "не больше 3 файлов" in out

    proposal = run_dir / "proposals" / f"{order}.md"
    proposal.write_text("РЕШЕНИЕ: исправить | не трогать\nПОЧЕМУ: x\n", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: fix") and "«РЕШЕНИЕ:» должно быть" in out
    assert "references/proposal-format.md" in out and FORMAT_MISMATCH_NOTE in out
    proposal.write_text(PROPOSAL.replace("assertEquals(200", "assertEqual(200", 1), encoding="utf-8")
    out = _next(run_dir, capsys)
    assert LAST_ATTEMPT_PROPOSAL in out

    proposal.write_text(PROPOSAL, encoding="utf-8")
    (run_dir / "analyses" / f"{login}.md").write_text(MARKDOWN_ANALYSIS, encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: summary") and "references/summary-format.md" in out
    summary_task = _read(run_dir / "summary_task.md")
    assert EXECUTOR_RULES in summary_task and "references/summary-format.md" in summary_task

    (run_dir / "summary.md").write_text("Итог прогона.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done")
    assert "Проблемы скилла" in out and "references/problem-report.md" in out
    assert "references/feedback.md" in out


def test_batch_task_carries_rules_reference_and_problem_line(
    project, testops: FakeTestOps, small_batches: None, capsys
) -> None:
    run_dir, _, out = _prepare(project, capsys)
    assert out.startswith("STATUS: analyze_batch")
    assert "Проблема скилла" in out  # основной агент переносит такие строки дословно
    batch = _read(run_dir / "batches" / "1.md")
    assert EXECUTOR_RULES in batch
    assert "references/analysis-format.md" in batch
    assert f"«{PROBLEM_PREFIX} <где и что не так>»" in batch
