"""Задание на общий анализ прогона и итоговый отчёт ``report.md``.

Краткий текст (шапка, общий анализ, строка на кластер, итоги по категориям)
агент выводит пользователю; ``report.md`` содержит его же плюс детали по
каждому кластеру.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from alla_core.models.clustering import ClusteringReport
from alla_core.models.llm import LLMAnalysisResult, LLMClusterAnalysis
from alla_core.models.testops import TriageReport
from alla_core.services.prompt_builder_service import build_launch_summary_prompt

from alla_skill_lib.analysis_format import CATEGORIES, ClusterAnalysis
from alla_skill_lib.cluster_task import UNTRUSTED_NOTE
from alla_skill_lib.history import format_date
from alla_skill_lib.proposals import Proposal
from alla_skill_lib.workspace import RunPaths

MAX_FLAGGED_SUMMARY_CHARS = 300
FEEDBACK_INVITATION = (
    "Обратная связь: назовите номер проблемы и её причину или рецепт исправления — "
    "сохраню в базу знаний проекта (alla-kb/) для следующих разборов."
)
MAX_CONSOLE_CLUSTERS = 20
MAX_REPORT_TESTS = 5
MAX_CAUSE_CHARS = 220

SUMMARY_RULES = """\
- Ты — инженер по анализу сбоев автотестов. Подготовь краткий итоговый отчёт
  по прогону на русском языке.
- Пиши только то, что видишь в данных ниже (включая разборы кластеров).
  Не додумывай.
- Обычный текст, 2–4 абзаца, без markdown-заголовков и без вступления."""


def load_models(run: dict[str, Any]) -> tuple[TriageReport, ClusteringReport | None]:
    triage = TriageReport.model_validate(run["triage"])
    clustering = (
        ClusteringReport.model_validate(run["clustering"]) if run.get("clustering") else None
    )
    return triage, clustering


def build_summary_task(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    paths: RunPaths,
) -> str:
    """Задание на общий анализ: серверный launch summary prompt + сжатые разборы.

    Полные разборы занимали почти всё задание, а сводке нужны только причина,
    суть и первый шаг исправления по каждому кластеру.
    """
    triage, clustering = load_models(run)
    assert clustering is not None
    cluster_analyses = {
        entry["cluster_id"]: LLMClusterAnalysis(
            cluster_id=entry["cluster_id"],
            analysis_text=_summary_text(analyses[entry["file_id"]], entry["file_id"] in flagged),
        )
        for entry in run["clusters"]
    }
    llm_result = LLMAnalysisResult(
        total_clusters=len(cluster_analyses),
        analyzed_count=len(cluster_analyses),
        failed_count=0,
        skipped_count=0,
        cluster_analyses=cluster_analyses,
    )
    prompt = build_launch_summary_prompt(clustering, triage, llm_result)
    return "\n".join([
        f"# Общий анализ прогона #{run['launch_id']}",
        "",
        "Запиши итоговый отчёт в файл (абсолютный путь, инструментом записи файлов):",
        str(paths.summary),
        "",
        "Затем выполни:",
        paths.next_command(),
        "",
        UNTRUSTED_NOTE,
        "",
        "## Правила",
        SUMMARY_RULES,
        "",
        prompt.user_prompt,
    ]) + "\n"


def render_report(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    summary: str,
    paths: RunPaths,
    fixes: dict[str, Proposal] | None = None,
    applied: set[str] | None = None,
    notes: list[str] | None = None,
) -> tuple[str, str]:
    """Вернуть (краткий текст для консоли, полный текст report.md)."""
    fixes = fixes or {}
    applied = applied or set()
    brief = _header(run)
    brief += ["", "### Общий анализ", summary.strip(), "", "### Кластеры"]
    for position, entry in enumerate(run["clusters"], start=1):
        if position > MAX_CONSOLE_CLUSTERS:
            rest = len(run["clusters"]) - MAX_CONSOLE_CLUSTERS
            brief.append(f"… и ещё {rest} — см. полный отчёт")
            break
        analysis = analyses[entry["file_id"]]
        brief.append(_cluster_title(position, entry, analysis, entry["file_id"] in flagged))
        reason = " ".join(analysis.cause_reason.split())
        if reason:
            brief.append(f"   {_truncate(reason, MAX_CAUSE_CHARS)}")
    brief += ["", _category_totals(run, analyses)]
    if fixes:
        brief += ["", "### Можно исправить в автотестах"]
        for file_id, proposal in fixes.items():
            state = " (уже применено)" if file_id in applied else ""
            location = f"{proposal.file}:{proposal.line}" if proposal.line else str(proposal.file)
            brief.append(
                f"{int(file_id)}. {location} — {_truncate(proposal.why, MAX_CAUSE_CHARS)}{state}"
            )
    if notes:
        brief += ["", "### Замечания", *(f"- {note}" for note in notes)]
    brief += ["", FEEDBACK_INVITATION]

    console = "\n".join([*brief, "", f"Полный отчёт: {paths.report}"])
    full = "\n".join([
        *brief,
        "",
        "---",
        "",
        "## Детали кластеров",
        *_cluster_details(run, analyses, flagged),
        *_proposal_details(fixes, applied),
    ])
    return console, full + "\n"


def render_green_report(run: dict[str, Any], paths: RunPaths) -> tuple[str, str]:
    """Отчёт для прогона без активных падений."""
    lines = [*_header(run), "", "Активных падений нет — анализировать нечего."]
    console = "\n".join([*lines, "", f"Полный отчёт: {paths.report}"])
    return console, "\n".join(lines) + "\n"


def _header(run: dict[str, Any]) -> list[str]:
    counts = run["counts"]
    title = f"## Разбор прогона #{run['launch_id']}"
    if run.get("launch_name"):
        title += f" — {run['launch_name']}"
    stats = (
        f"Тестов: {counts['total']} · passed {counts['passed']} · "
        f"failed {counts['failed']} · broken {counts['broken']} · "
        f"skipped {counts['skipped']}"
    )
    if counts.get("unknown"):
        stats += f" · unknown {counts['unknown']}"
    active = (
        f"Активных падений: {counts['active_failures']} → "
        f"кластеров: {len(run['clusters'])}"
    )
    if counts.get("muted_failures"):
        active += f" (muted-падений исключено: {counts['muted_failures']})"
    lines = [title, f"TestOps: {run['launch_url']}", stats, active]
    lines += [f"Внимание: {warning}" for warning in run.get("warnings", [])]
    return lines


def _cluster_title(
    position: int,
    entry: dict[str, Any],
    analysis: ClusterAnalysis,
    flagged: bool,
) -> str:
    category = analysis.category or "?"
    label = " ".join(str(entry["label"]).split())
    size = entry["member_count"]
    title = f"{position}. [{category}] {label} — {size} {_plural(size, 'тест', 'теста', 'тестов')}"
    if not flagged and analysis.kb_ref:
        title += f" · известная: {analysis.kb_ref}"
    history = entry.get("history")
    if history:
        launches = history["launches"]
        title += (
            f" · повтор: {launches} {_plural(launches, 'прогон', 'прогона', 'прогонов')} "
            f"с {format_date(history['first_date'])}"
        )
    if flagged:
        title += " (формат разбора нарушен)"
    return title


def _category_totals(run: dict[str, Any], analyses: dict[str, ClusterAnalysis]) -> str:
    clusters: Counter[str] = Counter()
    tests: Counter[str] = Counter()
    for entry in run["clusters"]:
        category = analyses[entry["file_id"]].category or "?"
        clusters[category] += 1
        tests[category] += entry["member_count"]
    order = [*CATEGORIES, "?"]
    parts = [
        f"{category} — {clusters[category]} "
        f"{_plural(clusters[category], 'кластер', 'кластера', 'кластеров')}, "
        f"{tests[category]} {_plural(tests[category], 'тест', 'теста', 'тестов')}"
        for category in sorted(clusters, key=order.index)
    ]
    return "По категориям: " + "; ".join(parts)


def _cluster_details(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
) -> list[str]:
    tests_by_id = {test["test_result_id"]: test for test in run["triage"]["failed_tests"]}
    clusters_by_id = {
        cluster["cluster_id"]: cluster for cluster in (run.get("clustering") or {}).get("clusters", [])
    }
    lines: list[str] = []
    for position, entry in enumerate(run["clusters"], start=1):
        analysis = analyses[entry["file_id"]]
        is_flagged = entry["file_id"] in flagged
        lines += ["", "### " + _cluster_title(position, entry, analysis, is_flagged)]
        if is_flagged:
            lines.append("_Разбор не прошёл проверку формата, текст приведён как есть._")
            lines += ["", analysis.raw]
        else:
            lines += [f"**Что сломалось:** {analysis.what}", f"**Причина:** {analysis.cause}"]
            lines += ["**Как исправить:**", analysis.fix]
            if analysis.code:
                lines.append("**Код:** " + "; ".join(analysis.code))
        member_ids = clusters_by_id.get(entry["cluster_id"], {}).get("member_test_ids", [])
        lines.append("**Тесты:**")
        for test_id in member_ids[:MAX_REPORT_TESTS]:
            test = tests_by_id.get(test_id)
            if test is None:
                continue
            name = test.get("name") or str(test_id)
            lines.append(f"- [{name}]({test['link']})" if test.get("link") else f"- {name}")
        if len(member_ids) > MAX_REPORT_TESTS:
            lines.append(f"- … и ещё {len(member_ids) - MAX_REPORT_TESTS}")
    return lines


def _proposal_details(fixes: dict[str, Proposal], applied: set[str]) -> list[str]:
    if not fixes:
        return []
    lines = ["", "## Предложенные правки автотестов"]
    for file_id, proposal in fixes.items():
        state = " — уже применено" if file_id in applied else ""
        location = f"{proposal.file}:{proposal.line}" if proposal.line else str(proposal.file)
        lines += [
            "",
            f"### Проблема {int(file_id)}: {location}{state}",
            f"**Почему:** {proposal.why}",
            "**Было:**",
            "```",
            *proposal.before,
            "```",
            "**Стало:**",
            "```",
            *proposal.after,
            "```",
        ]
    return lines


def _summary_text(analysis: ClusterAnalysis, flagged: bool) -> str:
    if flagged:  # формат нарушен — разделов нет, берём начало текста как есть
        return _truncate(" ".join(analysis.raw.split()), MAX_FLAGGED_SUMMARY_CHARS)
    return analysis.compact()


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
