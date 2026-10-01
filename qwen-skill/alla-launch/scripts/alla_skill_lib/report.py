"""Задание на общий анализ прогона и итоговый отчёт ``report.md``.

Отчёт читает человек, поэтому проблемы разложены по тому, кто что делает:
что требует внимания инженера, какие автотесты агент может поправить сам,
что нужно править вручную и что похоже на стенд или данные. Раздел определяет
код (категория разбора и наличие принятой правки), а не модель.

В терминал агент выводит краткий разбор: шапка, общий анализ и по одной строке
на проблему в каждом разделе, а в конце — ссылка на ``report.md``. Полный разбор
(тексты целиком, все шаги, все тесты, правки БЫЛО/СТАЛО) лежит в ``report.md``.
Краткий текст повторно попадает в контекст модели при каждом ``next``, поэтому
на него действуют потолки; файл в контекст не попадает и ничем не ограничен.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alla_core.models.clustering import ClusteringReport
from alla_core.models.llm import LLMAnalysisResult, LLMClusterAnalysis
from alla_core.models.testops import TriageReport
from alla_core.services.prompt_builder_service import build_launch_summary_prompt

from alla_skill_lib.agent_rules import EXECUTOR_RULES, SUMMARY_FORMAT_REF, reference_line
from alla_skill_lib.analysis_format import ClusterAnalysis
from alla_skill_lib.cluster_task import UNTRUSTED_NOTE, split_prompt
from alla_skill_lib.history import format_date
from alla_skill_lib.proposals import Proposal, weakening_warnings
from alla_skill_lib.workspace import RunPaths

MAX_FLAGGED_SUMMARY_CHARS = 300
FEEDBACK_INVITATION = (
    "Обратная связь: если причина указана неверно или вы знаете точную — назовите номер "
    "проблемы и что на самом деле, я сохраню это в базу знаний проекта (alla-kb/)."
)
# Краткий разбор в терминале: по строке на проблему, не больше пяти на раздел.
MAX_BRIEF_ITEMS = 5
MAX_BRIEF_NUMBERS = 8
BRIEF_TEXT_CHARS = 160
BRIEF_WARNING_CHARS = 140
# Предупреждения и замечания в терминале: число и длина ограничены — их повторяет каждый next.
MAX_BRIEF_NOTES = 5
BRIEF_NOTE_CHARS = 300
MAX_REPORT_TESTS = 5
# Подробности в report.md: список тестов проблемы целиком, но не бесконечный.
MAX_DETAIL_TESTS = 200
# Общий анализ: подробный разбор — у самых больших проблем, у остальных — одна строка.
MAX_SUMMARY_DETAILED = 30
SUMMARY_SHORT_CAUSE_CHARS = 160
SUMMARY_LABEL_CHARS = 120

SUMMARY_RULES = """\
- Ты — инженер по анализу сбоев автотестов. Подготовь короткий итог по прогону
  на русском языке, простым языком — как коллеге, который не открывал отчёт.
- Пиши только то, что видишь в данных ниже (включая разборы проблем).
  Не додумывай.
- Обычный текст, 2–4 коротких абзаца, без markdown-заголовков, списков и
  вступления. Без жаргона: не «кластер», «сигнатура», «трейс», а «проблема»,
  «ошибка»."""

SUMMARY_TASK = """\
Напиши итог по прогону:
1. Одно-два предложения: что случилось и насколько это серьёзно (сколько тестов
   упало и сколько разных проблем).
2. Самые важные 1–3 проблемы: что упало и почему, по-простому. Сначала те, что
   затрагивают больше тестов или похожи на ошибку самого приложения.
3. С чего начать инженеру: 1–2 конкретных действия.
Полный список проблем с подробностями выводится отчётом отдельно — не
перечисляй все проблемы и не пересказывай каждую. Будь лаконичен."""

ATTENTION = "attention"
AGENT = "agent"
MANUAL = "manual"
ENVIRONMENT = "environment"

# (раздел, заголовок, строка обзора, пояснение под заголовком)
SECTIONS = (
    (
        ATTENTION,
        "Требуют вашего внимания",
        "Посмотреть самим",
        "Похоже на ошибку приложения или причину не удалось определить — нужен взгляд инженера.",
    ),
    (
        AGENT,
        "Агент может поправить сам",
        "Агент поправит автотесты",
        "Дефект в самом автотесте, исправление понятно. Агент покажет изменения и "
        "запишет их только после вашего «да».",
    ),
    (
        MANUAL,
        "Автотест сломан, но править вручную",
        "Править автотесты вручную",
        "Виноват автотест, но агент не стал править его сам — нужна ваша правка.",
    ),
    (
        ENVIRONMENT,
        "Стенд и тестовые данные",
        "Проверить стенд и тестовые данные",
        "Тесты падают из-за стенда или данных — сначала проверьте окружение, "
        "код автотестов, скорее всего, ни при чём.",
    ),
)
CATEGORY_LABELS = {
    "тест": "ошибка в автотесте",
    "приложение": "возможная ошибка приложения",
    "окружение": "проблема стенда или окружения",
    "данные": "проблема с тестовыми данными",
    "неизвестно": "причина не ясна",
}
FLAGGED_LABEL = "причина не ясна — разбор не прошёл проверку формата"


@dataclass
class _Problem:
    """Одна проблема (кластер падений) с разбором и решением о правке."""

    entry: dict[str, Any]
    analysis: ClusterAnalysis
    flagged: bool
    proposal: Proposal | None
    state: str  # applied_state правки: applied | not_applied | unknown
    bucket: str

    @property
    def applied(self) -> bool:
        return self.state == "applied"

    @property
    def number(self) -> int:
        return int(self.entry["file_id"])

    @property
    def size(self) -> int:
        return int(self.entry["member_count"])

    @property
    def label(self) -> str:
        if self.flagged or self.analysis.category is None:
            return FLAGGED_LABEL
        return CATEGORY_LABELS[self.analysis.category]


@dataclass
class _Tests:
    """Тесты прогона для ссылок в отчёте."""

    by_id: dict[Any, dict[str, Any]]
    members: dict[str, list[Any]]

    @classmethod
    def of(cls, run: dict[str, Any]) -> _Tests:
        clusters = (run.get("clustering") or {}).get("clusters", [])
        return cls(
            by_id={test["test_result_id"]: test for test in run["triage"]["failed_tests"]},
            members={cluster["cluster_id"]: cluster.get("member_test_ids", []) for cluster in clusters},
        )

    def of_cluster(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        found = (self.by_id.get(test_id) for test_id in self.members.get(entry["cluster_id"], []))
        return [test for test in found if test is not None]


def load_models(run: dict[str, Any]) -> tuple[TriageReport, ClusteringReport | None]:
    triage = TriageReport.model_validate(run["triage"])
    clustering = (
        ClusteringReport.model_validate(run["clustering"]) if run.get("clustering") else None
    )
    return triage, clustering


# ---------------------------------------------------------------------------
# Задание на общий анализ
# ---------------------------------------------------------------------------


def build_summary_task(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    paths: RunPaths,
) -> str:
    """Задание на общий анализ: данные промпта ядра + сжатые разборы.

    Полные разборы занимали почти всё задание, а сводке нужны только причина,
    суть и первый шаг исправления. Подробно (``compact``) идут самые большие
    проблемы, остальным — одна строка причины, чтобы задание не росло вместе с
    числом проблем. «ЗАДАНИЕ» ядра (перечислять все проблемы) заменено
    своим: список проблем в отчёте строит код.
    """
    triage, clustering = load_models(run)
    assert clustering is not None
    largest = sorted(run["clusters"], key=lambda entry: -entry["member_count"])
    detailed = {entry["file_id"] for entry in largest[:MAX_SUMMARY_DETAILED]}
    cluster_analyses = {
        entry["cluster_id"]: LLMClusterAnalysis(
            cluster_id=entry["cluster_id"],
            analysis_text=_summary_text(
                analyses[entry["file_id"]],
                entry["file_id"] in flagged,
                entry["file_id"] in detailed,
            ),
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
    clustering = clustering.model_copy(update={"clusters": [
        cluster.model_copy(update={"label": _truncate(_one_line(cluster.label), SUMMARY_LABEL_CHARS)})
        for cluster in clustering.clusters
    ]})
    prompt = build_launch_summary_prompt(clustering, triage, llm_result)
    data_part, _server_task = split_prompt(prompt.user_prompt)
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
        EXECUTOR_RULES,
        "",
        data_part,
        "",
        "## Задание",
        SUMMARY_TASK,
        "",
        reference_line(SUMMARY_FORMAT_REF, "Формат итога с примером"),
    ]) + "\n"


def _summary_text(analysis: ClusterAnalysis, flagged: bool, detailed: bool) -> str:
    if flagged:  # формат нарушен — разделов нет, берём начало текста как есть
        limit = MAX_FLAGGED_SUMMARY_CHARS if detailed else SUMMARY_SHORT_CAUSE_CHARS
        return _truncate(_one_line(analysis.raw), limit)
    if detailed:
        return analysis.compact()
    return f"ПРИЧИНА: {_truncate(_one_line(analysis.cause), SUMMARY_SHORT_CAUSE_CHARS)}"


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------


def render_report(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    summary: str,
    paths: RunPaths,
    proposals: dict[str, Proposal] | None = None,
    apply_states: dict[str, str] | None = None,
    notes: list[str] | None = None,
    not_proposed: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Вернуть (краткий текст для консоли, полный текст report.md).

    ``proposals`` — принятые предложения (и «исправить», и «не трогать»);
    ``apply_states`` — ``applied_state`` каждой правки (нет записи = не применена);
    ``not_proposed`` — почему по проблеме «тест» правку не предлагали.
    """
    proposals = proposals or {}
    apply_states = apply_states or {}
    tests = _Tests.of(run)
    problems = [
        _problem(entry, analyses[entry["file_id"]], flagged, proposals, apply_states)
        for entry in run["clusters"]
    ]
    reasons = not_proposed or {}
    link = _report_link(paths)

    brief = _header(run, brief=True)
    brief += ["", "### Коротко", summary.strip()]
    for bucket, title, _, _ in SECTIONS:
        group = sorted((p for p in problems if p.bucket == bucket), key=_sort_key)
        if group:
            brief += ["", *_brief_section(title, group)]
    if notes:
        brief += ["", "### Замечания", *_brief_lines(notes, "- ", "замечаний")]
    brief += ["", FEEDBACK_INVITATION, "", link]

    full = _header(run)
    full += ["", "### Коротко", summary.strip()]
    full += ["", *_overview(problems)]
    for bucket, title, _, hint in SECTIONS:
        group = sorted((p for p in problems if p.bucket == bucket), key=_sort_key)
        if group:
            full += ["", *_section(title, hint, group, tests, reasons)]
    if notes:
        full += ["", "### Замечания", *(f"- {note}" for note in notes)]
    full += [
        "",
        FEEDBACK_INVITATION,
        "",
        "---",
        "",
        "## Подробности по проблемам",
        *_details(problems, tests, reasons, run["launch_url"]),
        *_proposal_details(problems, proposals),
    ]
    return "\n".join(brief), "\n".join(full) + "\n"


def _report_link(paths: RunPaths) -> str:
    """Ссылка на полный разбор: кликабельная ``file://`` и путь, который можно скопировать."""
    report = paths.report.absolute()
    return f"Полный разбор со всеми подробностями: [{report.name}]({report.as_uri()})\nФайл: {report}"


def render_green_report(run: dict[str, Any], paths: RunPaths) -> tuple[str, str]:
    """Отчёт для прогона без активных падений."""
    muted = run["counts"].get("muted_failures", 0)
    if muted:
        message = f"Упавших тестов нет, кроме отключённых (muted): {muted} — разбирать нечего."
    else:
        message = "Упавших тестов нет — разбирать нечего."
    lines = [*_header(run), "", message]
    console = "\n".join([*lines, "", _report_link(paths)])
    return console, "\n".join(lines) + "\n"


def _problem(
    entry: dict[str, Any],
    analysis: ClusterAnalysis,
    flagged: set[str],
    proposals: dict[str, Proposal],
    apply_states: dict[str, str],
) -> _Problem:
    file_id = entry["file_id"]
    is_flagged = file_id in flagged
    proposal = proposals.get(file_id)
    state = apply_states.get(file_id, "not_applied")
    if proposal is not None and proposal.is_fix and not is_flagged:
        # Состояние неизвестно: apply её не применит, обещать «агент поправит» нельзя.
        bucket = MANUAL if state == "unknown" else AGENT
    elif is_flagged or analysis.category in (None, "приложение", "неизвестно"):
        bucket = ATTENTION
    elif analysis.category == "тест":
        bucket = MANUAL
    else:
        bucket = ENVIRONMENT
    return _Problem(entry, analysis, is_flagged, proposal, state, bucket)


def _sort_key(problem: _Problem) -> tuple[int, int, int]:
    """Внутри раздела: сначала ошибки приложения, затем большие проблемы."""
    likely_bug = problem.bucket == ATTENTION and not problem.flagged and (
        problem.analysis.category == "приложение"
    )
    return (0 if likely_bug else 1, -problem.size, problem.number)


def _header(run: dict[str, Any], brief: bool = False) -> list[str]:
    counts = run["counts"]
    title = f"## Разбор прогона #{run['launch_id']}"
    if run.get("launch_name"):
        title += f" — {run['launch_name']}"
    failed = counts["failed"] + counts["broken"]
    stats = (
        f"Всего тестов: {counts['total']} — прошло {counts['passed']}, упало {failed} "
        f"(failed {counts['failed']}, broken {counts['broken']}), пропущено {counts['skipped']}"
    )
    if counts.get("unknown"):
        stats += f", статус не определён {counts['unknown']}"
    lines = [title, f"TestOps: {run['launch_url']}", stats + "."]
    problems = len(run["clusters"])
    if problems:
        active = counts["active_failures"]
        scope = (
            f"В разборе: {active} {_plural(active, 'упавший тест', 'упавших теста', 'упавших тестов')}"
            f" → {problems} {_plural(problems, 'проблема', 'проблемы', 'проблем')}"
        )
        muted = counts.get("muted_failures", 0)
        if muted:
            scope += (
                f" (ещё {muted} "
                f"{_plural(muted, 'отключённый muted-тест', 'отключённых muted-теста', 'отключённых muted-тестов')}"
                " не учитываем)"
            )
        lines.append(scope + ".")
    warnings = [f"Внимание: {warning}" for warning in run.get("warnings", [])]
    return lines + (_brief_lines(warnings, "", "предупреждений") if brief else warnings)


def _overview(problems: list[_Problem]) -> list[str]:
    lines = ["### Что делать"]
    for bucket, _, overview, _ in SECTIONS:
        group = sorted((p for p in problems if p.bucket == bucket), key=_sort_key)
        if not group:
            continue
        tests = sum(p.size for p in group)
        lines.append(
            f"- {overview}: {len(group)} {_plural(len(group), 'проблема', 'проблемы', 'проблем')} "
            f"({tests} {_plural(tests, 'тест', 'теста', 'тестов')}) — {_numbers(group)}"
        )
    return lines


def _numbers(group: list[_Problem]) -> str:
    word = "проблема" if len(group) == 1 else "проблемы"
    return f"{word} {', '.join(str(p.number) for p in group)}"


def _section(
    title: str,
    hint: str,
    group: list[_Problem],
    tests: _Tests,
    not_proposed: dict[str, str],
) -> list[str]:
    lines = [f"### {title} ({len(group)})", hint]
    for problem in group:
        lines += ["", *_item(problem, tests, not_proposed)]
    return lines


def _item(problem: _Problem, tests: _Tests, not_proposed: dict[str, str]) -> list[str]:
    analysis = problem.analysis
    lines = [_item_title(problem)]
    if problem.flagged:
        lines.append("- Разбор не прошёл проверку формата, его текст — в подробностях ниже.")
    else:
        what = _one_line(analysis.what)
        if what:
            lines.append(f"- Что случилось: {what}")
        if problem.bucket == AGENT and problem.proposal is not None:
            lines.append(f"- Почему это ошибка теста: {_one_line(problem.proposal.why)}")
        else:
            reason = _one_line(analysis.cause_reason)
            if reason:
                lines.append(f"- Почему: {reason}")
    if problem.bucket == AGENT and problem.proposal is not None:
        lines += [
            f"- Обратите внимание: {warning}"
            for warning in weakening_warnings(problem.proposal.before, problem.proposal.after)
        ]
        lines.append(
            "- Статус: уже применено — запустите тест заново, чтобы убедиться, что он проходит"
            if problem.applied
            else "- Статус: ждёт вашего «да» — агент покажет изменения перед записью"
        )
    elif not problem.flagged:
        lines += _fix_lines(analysis)
    if problem.bucket == MANUAL:
        lines.append(f"- Почему агент не правил сам: {_not_fixed_reason(problem, not_proposed)}")
    listed = _test_links(tests.of_cluster(problem.entry), problem.size)
    if listed:
        lines.append(f"- Тесты: {listed}")
    lines += _history_lines(problem)
    return lines


def _fix_lines(analysis: ClusterAnalysis) -> list[str]:
    """«Что делать»: все шаги разбора."""
    steps = [line for line in analysis.fix.splitlines() if line.strip()]
    if len(steps) <= 1:
        return [f"- Что делать: {_one_line(analysis.fix)}"] if steps else []
    return ["- Что делать:", *(f"   {line.strip()}" for line in steps)]


# --- краткий разбор для терминала -------------------------------------------


def _brief_section(title: str, group: list[_Problem]) -> list[str]:
    lines = [f"### {title} ({len(group)})"]
    for problem in group[:MAX_BRIEF_ITEMS]:
        lines += _brief_item(problem)
    rest = group[MAX_BRIEF_ITEMS:]
    if rest:
        # Номера — только первые несколько: при тысячах проблем список рос бы с каждым next.
        numbers = ", ".join(str(p.number) for p in rest[:MAX_BRIEF_NUMBERS])
        more = " и др." if len(rest) > MAX_BRIEF_NUMBERS else ""
        lines.append(f"- … и ещё {len(rest)} (проблемы {numbers}{more}) — в полном разборе")
    return lines


def _brief_lines(items: list[str], prefix: str, what: str) -> list[str]:
    """Предупреждения/замечания для терминала: не больше пяти, каждое не длиннее лимита."""
    lines = [prefix + _truncate(_one_line(item), BRIEF_NOTE_CHARS) for item in items[:MAX_BRIEF_NOTES]]
    rest = len(items) - MAX_BRIEF_NOTES
    if rest > 0:
        lines.append(f"{prefix}… и ещё {rest} {what} — в полном разборе")
    return lines


def _brief_item(problem: _Problem) -> list[str]:
    """Проблема одной строкой (для правки — плюс строка «Проверьте», если она есть)."""
    size = f"{problem.size} {_plural(problem.size, 'тест', 'теста', 'тестов')}"
    head = f"- **Проблема {problem.number}** · {size} · "
    extra: list[str] = []
    proposal = problem.proposal
    if problem.bucket == AGENT and proposal is not None:
        location = f"{proposal.file}:{proposal.line}" if proposal.line else str(proposal.file)
        status = "уже применено" if problem.applied else "ждёт вашего «да»"
        line = f"{head}`{location}` — {_truncate(_one_line(proposal.why), BRIEF_TEXT_CHARS)} ({status})"
        extra = [
            f"  Проверьте: {_truncate(warning, BRIEF_WARNING_CHARS)}"
            for warning in weakening_warnings(proposal.before, proposal.after)[:1]
        ]
    else:
        line = head + problem.label
        what = "" if problem.flagged else problem.analysis.what_first_sentence()
        if what:
            line += f" — {_truncate(what, BRIEF_TEXT_CHARS)}"
    tags = _brief_tags(problem)
    return [line + "".join(f" · {tag}" for tag in tags), *extra]


def _brief_tags(problem: _Problem) -> list[str]:
    tags: list[str] = []
    history = problem.entry.get("history")
    if history:
        launches = history["launches"]
        tags.append(
            f"повторяется (уже была в {launches} "
            f"{_plural(launches, 'другом прогоне', 'других прогонах', 'других прогонах')})"
        )
    if not problem.flagged and problem.analysis.kb_ref:
        tags.append(f"известная проблема: {problem.analysis.kb_ref}")
    return tags


def _item_title(problem: _Problem) -> str:
    size = f"{problem.size} {_plural(problem.size, 'тест', 'теста', 'тестов')}"
    if problem.bucket == AGENT and problem.proposal is not None:
        proposal = problem.proposal
        location = f"{proposal.file}:{proposal.line}" if proposal.line else str(proposal.file)
        return f"**Проблема {problem.number}** — `{location}` · {size}"
    return f"**Проблема {problem.number}** — {size} · {problem.label}"


def _not_fixed_reason(problem: _Problem, not_proposed: dict[str, str]) -> str:
    """Почему агент не правил тест сам."""
    proposal = problem.proposal
    if proposal is not None and proposal.is_fix and problem.state == "unknown":
        location = f"{proposal.file}:{proposal.line}" if proposal.line else str(proposal.file)
        return (
            f"правка в `{location}` применялась командой apply, но файл потом менялся и "
            "участок правки изменён — стоит ли она, неизвестно. Проверьте файл (git diff); "
            "apply повторно её не применит"
        )
    if proposal is not None and not proposal.is_fix:
        return f"агент решил не трогать код: {_one_line(proposal.why)}"
    reason = not_proposed.get(problem.entry["file_id"])
    if reason:
        return reason
    if not problem.analysis.code:
        return "в разборе нет ссылки на строку кода автотеста — агенту негде править"
    return "правка не предлагалась"


def _test_links(tests: list[dict[str, Any]], total: int) -> str:
    text = ", ".join(_test_link(test) for test in tests[:MAX_REPORT_TESTS])
    rest = total - min(len(tests), MAX_REPORT_TESTS)
    if text and rest > 0:
        text += f" и ещё {rest} (список — в подробностях ниже)"
    return text


def _test_link(test: dict[str, Any]) -> str:
    name = _one_line(str(test.get("name") or test["test_result_id"]))
    return f"[{name}]({test['link']})" if test.get("link") else name


def _history_lines(problem: _Problem) -> list[str]:
    lines: list[str] = []
    history = problem.entry.get("history")
    if history:
        launches = history["launches"]
        where = _plural(launches, "другом прогоне", "других прогонах", "других прогонах")
        lines.append(
            f"- Повторяется: уже была в {launches} {where}, "
            f"впервые {format_date(history['first_date'])}"
        )
    if not problem.flagged and problem.analysis.kb_ref:
        lines.append(f"- Известная проблема: {problem.analysis.kb_ref} (есть в базе знаний проекта)")
    return lines


# ---------------------------------------------------------------------------
# Подробности (только report.md)
# ---------------------------------------------------------------------------


def _details(
    problems: list[_Problem],
    tests: _Tests,
    not_proposed: dict[str, str],
    launch_url: str,
) -> list[str]:
    lines: list[str] = []
    for problem in problems:
        analysis = problem.analysis
        size = f"{problem.size} {_plural(problem.size, 'тест', 'теста', 'тестов')}"
        lines += ["", f"### Проблема {problem.number} — {size} · {problem.label}"]
        lines.append(f"- **Ошибка в TestOps:** {_one_line(str(problem.entry['label']))}")
        if problem.flagged:
            lines.append("- Разбор не прошёл проверку формата, текст приведён как есть:")
            lines += ["", analysis.raw, ""]
        else:
            lines += [
                f"- **Что случилось:** {_one_line(analysis.what)}",
                f"- **Почему:** {_one_line(analysis.cause_reason)}",
                "- **Как исправить:**",
                *(f"   {line}" for line in analysis.fix.splitlines()),
            ]
            if analysis.code:
                lines.append("- **Где в коде:** " + "; ".join(analysis.code))
        if problem.bucket == MANUAL:
            reason = _not_fixed_reason(problem, not_proposed)
            lines.append(f"- **Почему агент не правил сам:** {reason}")
        lines += _history_lines(problem)
        lines.append("- **Тесты:**")
        members = tests.of_cluster(problem.entry)
        for test in members[:MAX_DETAIL_TESTS]:
            lines.append(f"   - {_test_link(test)}")
        if problem.size > len(members[:MAX_DETAIL_TESTS]):
            hidden = problem.size - len(members[:MAX_DETAIL_TESTS])
            lines.append(f"   - и ещё {hidden} — полный список в TestOps: {launch_url}")
    return lines


def _proposal_details(problems: list[_Problem], proposals: dict[str, Proposal]) -> list[str]:
    fixes = [
        p for p in problems
        if p.proposal is not None and p.proposal.is_fix and not p.flagged
    ]
    if not fixes:
        return []
    lines = ["", "## Правки автотестов, которые агент может применить"]
    for problem in fixes:
        proposal = proposals[problem.entry["file_id"]]
        state = {
            "applied": " — уже применено",
            "unknown": " — состояние правки неизвестно (файл менялся после применения)",
        }.get(problem.state, "")
        location = f"{proposal.file}:{proposal.line}" if proposal.line else str(proposal.file)
        lines += [
            "",
            f"### Проблема {problem.number}: {location}{state}",
            f"**Почему:** {proposal.why}",
            "",
            "**Было:**",
            "```",
            *proposal.before,
            "```",
            "**Стало:**",
            "```",
            *proposal.after,
            "```",
            *(f"**Проверь:** {warning}" for warning in weakening_warnings(proposal.before, proposal.after)),
        ]
    return lines


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
