"""Задание на общий анализ прогона и итоговый отчёт ``report.md``.

Отчёт читает человек, поэтому проблемы разложены по тому, кто что делает:
что требует внимания инженера, какие автотесты агент может поправить сам,
что нужно править вручную и что похоже на стенд или данные. Раздел определяет
код (категория разбора и наличие принятой правки), а не модель.

В терминал агент выводит краткий разбор: шапка с полоской прошедших тестов и
обзором разделов, общий анализ и по блоку на проблему в каждом разделе — что
случилось, что думает агент о причине и что делать, — а в конце ссылка на
``report.md``. Эмодзи — только у разделов (срочность цветом), остальное — ASCII.
Полный разбор (тексты целиком, все шаги, все тесты, правки БЫЛО/СТАЛО) лежит
в ``report.md``. Краткий текст повторно попадает в контекст модели при каждом ``next``, поэтому
на него действуют потолки; файл в контекст не попадает и ничем не ограничен.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from alla_core.models.clustering import ClusteringReport
from alla_core.models.llm import LLMAnalysisResult, LLMClusterAnalysis
from alla_core.models.testops import TriageReport
from alla_core.services.prompt_builder_service import build_launch_summary_prompt

from alla_skill_lib.agent_rules import EXECUTOR_RULES, SUMMARY_FORMAT_REF, reference_line
from alla_skill_lib.analysis_format import UNCONFIRMED_NOTE, ClusterAnalysis, first_sentence
from alla_skill_lib.cluster_task import UNTRUSTED_NOTE
from alla_skill_lib.history import format_date
from alla_skill_lib.proposals import Proposal, weakening_warnings
from alla_skill_lib.sources import describe
from alla_skill_lib.workspace import RunPaths

MAX_FLAGGED_SUMMARY_CHARS = 300
FEEDBACK_INVITATION = (
    "Обратная связь: если причина указана неверно или вы знаете точную — назовите номер "
    "проблемы и что на самом деле, я сохраню это в базу знаний проекта (alla-kb/)."
)
# Краткий разбор в терминале: по несколько строк на проблему, не больше пяти на раздел.
MAX_BRIEF_ITEMS = 5
BRIEF_TEXT_CHARS = 160  # «что случилось»
BRIEF_CAUSE_CHARS = 200  # «агент считает»
BRIEF_STEP_CHARS = 140  # «что делать»
BRIEF_REASON_CHARS = 160  # «почему агент не правил сам»
BRIEF_TEST_NAME_CHARS = 60
BRIEF_WARNING_CHARS = 140
BRIEF_BAR_CELLS = 10  # полоска прошедших тестов: [####------]
BRIEF_LABEL_WIDTH = 15  # подписи строк проблемы выровнены в колонку
# Предупреждения и замечания в терминале: число и длина ограничены — их повторяет каждый next.
MAX_BRIEF_NOTES = 5
BRIEF_NOTE_CHARS = 300
MAX_REPORT_TESTS = 5
# Подробности в report.md: список тестов проблемы целиком, но не бесконечный.
MAX_DETAIL_TESTS = 200
# Общий анализ: подробный разбор — у самых больших проблем, у следующих — одна строка,
# остальные — одной сводной строкой по причинам, чтобы задание не росло с числом проблем.
MAX_SUMMARY_DETAILED = 10
MAX_SUMMARY_LISTED = 40
SUMMARY_SHORT_CAUSE_CHARS = 160
SUMMARY_LABEL_CHARS = 120

SUMMARY_RULES = """\
- Ты — инженер по анализу сбоев автотестов. Напиши короткий итог по прогону по-русски,
  простым языком, как коллеге, который не открывал отчёт. Опирайся только на данные
  ниже (включая разборы проблем), ничего не додумывай.
- Обычный текст в 2–4 коротких абзаца, без markdown-заголовков, списков и вступления.
  Без жаргона: не «кластер», «сигнатура», «трейс», а «проблема», «ошибка». Будь
  лаконичен."""

SUMMARY_TASK = """\
Напиши итог по прогону:
1. Одно-два предложения: что случилось и насколько это серьёзно (сколько тестов
   упало и сколько разных проблем).
2. Самые важные 1–3 проблемы: что упало и почему. Сначала те, что затрагивают
   больше тестов или похожи на ошибку самого приложения. Причина в разборе
   «неизвестно» — так и скажи и назови, каких данных не хватает; своих версий
   причины (окружение, сборка, сеть) не придумывай. «Что упало» — не больше, чем
   сказано в разборе: название шага — не результат (при голом assertion — «не
   прошла проверка на шаге «Выгрузить отчёт»», а не «отчёт не выгрузился»).
3. С чего начать инженеру: 1–2 конкретных действия.
Полный список проблем строит отчёт — не перечисляй все проблемы и не пересказывай
каждую."""

ATTENTION = "attention"
AGENT = "agent"
MANUAL = "manual"
ENVIRONMENT = "environment"

# (раздел, заголовок, пояснение под заголовком в report.md)
SECTIONS = (
    (
        ATTENTION,
        "Требуют вашего внимания",
        "Похоже на ошибку приложения или причину не удалось определить — нужен взгляд инженера.",
    ),
    (
        AGENT,
        "Агент может поправить сам",
        "Дефект в самом автотесте, исправление понятно. Агент покажет изменения и "
        "запишет их только после вашего «да».",
    ),
    (
        MANUAL,
        "Автотест сломан, но править вручную",
        "Виноват автотест, но агент не стал править его сам — нужна ваша правка.",
    ),
    (
        ENVIRONMENT,
        "Стенд и тестовые данные",
        "Тесты падают из-за стенда или данных — сначала проверьте окружение, "
        "код автотестов, скорее всего, ни при чём.",
    ),
)
# Краткий разбор: значок раздела (срочность цветом) и короткое имя для обзора в шапке.
# Эмодзи без селектора U+FE0F: с ним в части терминалов съезжает ширина строки.
BRIEF_SECTIONS = {
    ATTENTION: ("🔴", "требуют вашего внимания"),
    AGENT: ("🟢", "агент поправит сам"),
    MANUAL: ("🟡", "править вручную"),
    ENVIRONMENT: ("🔵", "стенд и данные"),
}
# Категория в кратком разборе — ASCII-меткой: «Агент считает: [ПРИЛОЖЕНИЕ] …».
CATEGORY_TAGS = {
    "тест": "АВТОТЕСТ",
    "приложение": "ПРИЛОЖЕНИЕ",
    "окружение": "СТЕНД",
    "данные": "ДАННЫЕ",
    "неизвестно": "НЕ ЯСНО",
}
FLAGGED_TAG = "ФОРМАТ НАРУШЕН"
# Блочная разметка в начале строки: Qwen Code рисует Markdown построчно, и строка, которая
# начинается с ``` или ~~~, открывает блок кода до конца ответа — с отчётом и ссылкой.
# Заголовки, цитаты, таблицы, списки, линии и формулы меняют вид строки.
_BLOCK_MARKUP_RE = re.compile(
    r"^(?:\s*(?:`{3,}|~{3,}|\${2}|#{1,6}(?=\s|$)|>|\||(?:[-*_] *){3,}(?=\s|$)|[-*+](?=\s|$)|\d+[.)](?=\s|$)))+\s*"
)
# Строка-ограда целиком: ``` или ~~~ с необязательным языком («```text»).
_FENCE_LINE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*[\w+-]*\s*$")
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

    @property
    def tag(self) -> str:
        """Категория ASCII-меткой для краткого разбора."""
        if self.flagged or self.analysis.category is None:
            return FLAGGED_TAG
        return CATEGORY_TAGS[self.analysis.category]


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


def build_summary_data(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
) -> str:
    """Общий блок данных для задания сводки и проверки её актуальности.

    Полные разборы занимали почти всё задание, а сводке нужны только причина,
    суть и первый шаг исправления. Подробно (``compact``) идут самые большие
    проблемы, следующие — одной строкой причины, остальные — одной сводной
    строкой по причинам: размер задания ограничен при любом числе проблем.
    «ЗАДАНИЕ» ядра (перечислять все проблемы) заменено своим: список проблем в
    отчёте строит код.
    """
    triage, clustering = load_models(run)
    assert clustering is not None
    largest = sorted(run["clusters"], key=lambda entry: -entry["member_count"])
    detailed = {entry["file_id"] for entry in largest[:MAX_SUMMARY_DETAILED]}
    top = {entry["file_id"] for entry in largest[:MAX_SUMMARY_LISTED]}
    listed = [entry for entry in run["clusters"] if entry["file_id"] in top]  # порядок номеров
    listed_ids = {entry["cluster_id"] for entry in listed}
    cluster_analyses = {
        entry["cluster_id"]: LLMClusterAnalysis(
            cluster_id=entry["cluster_id"],
            analysis_text=_summary_text(
                analyses[entry["file_id"]],
                entry["file_id"] in flagged,
                entry["file_id"] in detailed,
            ),
        )
        for entry in listed
    }
    llm_result = LLMAnalysisResult(
        total_clusters=len(cluster_analyses),
        analyzed_count=len(cluster_analyses),
        failed_count=0,
        skipped_count=0,
        cluster_analyses=cluster_analyses,
    )
    # cluster_count остаётся полным: в шапке данных — число всех проблем прогона.
    clustering = clustering.model_copy(update={"clusters": [
        cluster.model_copy(update={"label": _truncate(_one_line(cluster.label), SUMMARY_LABEL_CHARS)})
        for cluster in clustering.clusters
        if cluster.cluster_id in listed_ids
    ]})
    numbers = {entry["cluster_id"]: int(entry["file_id"]) for entry in listed}
    prompt = build_launch_summary_prompt(clustering, triage, llm_result, numbers)
    rest = largest[MAX_SUMMARY_LISTED:]
    return "\n".join([
        prompt.user_prompt,
        *(["", _summary_rest(rest, analyses, flagged)] if rest else []),
    ])


def build_summary_task(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    paths: RunPaths,
    *,
    data: str | None = None,
) -> str:
    """Данные сводки с правилами, путями и заданием для агента."""
    if data is None:
        data = build_summary_data(run, analyses, flagged)
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
        data,
        "",
        "## Задание",
        SUMMARY_TASK,
        "",
        reference_line(SUMMARY_FORMAT_REF, "Формат итога с примером"),
    ]) + "\n"


def _summary_rest(
    rest: list[dict[str, Any]],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
) -> str:
    """Проблемы, не вошедшие в задание по одной, — одной строкой: сколько их и по каким причинам."""
    problems: Counter[str] = Counter()
    tests: Counter[str] = Counter()
    for entry in rest:
        category = analyses[entry["file_id"]].category
        key = "не ясна" if entry["file_id"] in flagged or category is None else category
        problems[key] += 1
        tests[key] += int(entry["member_count"])
    total = sum(tests.values())
    parts = [
        f"{key} — {count} {_plural(count, 'проблема', 'проблемы', 'проблем')} "
        f"({tests[key]} {_plural(tests[key], 'тест', 'теста', 'тестов')})"
        for key, count in sorted(problems.items(), key=lambda item: (-tests[item[0]], item[0]))
    ]
    return (
        f"--- Ещё {len(rest)} {_plural(len(rest), 'проблема', 'проблемы', 'проблем')} поменьше "
        f"({total} {_plural(total, 'тест', 'теста', 'тестов')}), по причинам: "
        + "; ".join(parts) + " ---"
    )


def _summary_text(analysis: ClusterAnalysis, flagged: bool, detailed: bool) -> str:
    if flagged:  # формат нарушен — разделов нет, берём начало текста как есть
        limit = MAX_FLAGGED_SUMMARY_CHARS if detailed else SUMMARY_SHORT_CAUSE_CHARS
        return _truncate(_one_line(analysis.raw), limit)
    if detailed:
        return analysis.compact()
    note = f" ({UNCONFIRMED_NOTE})" if analysis.unconfirmed_by_log else ""
    return f"ПРИЧИНА: {_truncate(_one_line(analysis.cause), SUMMARY_SHORT_CAUSE_CHARS)}{note}"


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

    groups = [
        (bucket, title, sorted((p for p in problems if p.bucket == bucket), key=_sort_key))
        for bucket, title, _ in SECTIONS
    ]
    groups = [(bucket, title, group) for bucket, title, group in groups if group]
    brief = _brief_header(run, groups)
    summary = _plain_text(summary)
    brief += ["", "### Коротко", summary, "", "---"]
    for bucket, title, group in groups:
        brief += ["", *_brief_section(bucket, title, group, tests, reasons)]
    if notes:
        brief += ["", "### Замечания", *_brief_lines(notes, "- ", "замечаний")]
    brief += ["", "---", "", FEEDBACK_INVITATION, "", link]

    full = _header(run)
    full += ["", "### Коротко", summary]
    for bucket, title, hint in SECTIONS:
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
    return f"Полный разбор: [{report.name}]({report.as_uri()})\nФайл: {report}"


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
    elif (is_flagged or analysis.category in (None, "приложение", "неизвестно")
          or analysis.consistency_kind == "different"):
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


def _header(run: dict[str, Any]) -> list[str]:
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
        lines.append(scope + _muted_note(counts) + ".")
    return lines + [f"Внимание: {warning}" for warning in run.get("warnings", [])]


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
        lines.append(f"- Агент считает: {_opinion(problem, _reason(problem))}{_unconfirmed(problem)}")
        lines += _consistency_lines(analysis, "- ")
        lines += _evidence_lines(analysis, "- Наблюдения:", "- Не хватает: ")
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
        if analysis.code:
            lines.append("- Где в коде: " + "; ".join(analysis.code))
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
    if len(steps) <= 1:  # единственный шаг — в строку, без номера «1.»
        return [f"- Что делать: {analysis.first_fix_step()}"] if steps else []
    return ["- Что делать:", *(f"   {line.strip()}" for line in steps)]


# --- краткий разбор для терминала -------------------------------------------


def _muted_note(counts: dict[str, Any]) -> str:
    muted = counts.get("muted_failures", 0)
    if not muted:
        return ""
    noun = _plural(muted, "отключённый muted-тест", "отключённых muted-теста", "отключённых muted-тестов")
    return f" (ещё {muted} {noun} не учитываем)"


def _brief_header(
    run: dict[str, Any],
    groups: list[tuple[str, str, list[_Problem]]],
) -> list[str]:
    """Шапка краткого разбора: полоска прошедших тестов, счётчики без нулей и обзор разделов."""
    counts = run["counts"]
    title = f"## Разбор прогона #{run['launch_id']}"
    if run.get("launch_name"):
        title += f" — {run['launch_name']}"
    total = counts["total"]
    failed = counts["failed"] + counts["broken"]
    parts = [f"прошло {counts['passed']}", f"упало {failed}"]
    if counts["broken"]:
        parts[-1] += f" (из них broken {counts['broken']})"
    if counts["skipped"]:
        parts.append(f"пропущено {counts['skipped']}")
    if counts.get("unknown"):
        parts.append(f"статус не определён {counts['unknown']}")
    stats = f"{total} {_plural(total, 'тест', 'теста', 'тестов')}: " + ", ".join(parts)
    lines = [title, "", f"{_pass_bar(counts['passed'], total)} {stats}" if total else stats]
    problems = len(run["clusters"])
    if problems:
        active = counts["active_failures"]
        scope = (
            f"{active} {_plural(active, 'упавший тест', 'упавших теста', 'упавших тестов')}"
            f" -> {problems} {_plural(problems, 'проблема', 'проблемы', 'проблем')}"
        )
        lines.append(scope + _muted_note(counts) + ":")
        width = max(len(name) for _, name in BRIEF_SECTIONS.values()) + 4
        for bucket, _, group in groups:
            icon, name = BRIEF_SECTIONS[bucket]
            size = sum(p.size for p in group)
            lines.append(
                f"   {icon} {name} {'.' * (width - len(name))} {len(group)} "
                f"({size} {_plural(size, 'тест', 'теста', 'тестов')})"
            )
    lines.append(f"TestOps: {run['launch_url']}")
    return lines + _brief_lines(run.get("warnings", []), "(!) ", "предупреждений")


def _pass_bar(passed: int, total: int) -> str:
    """«[####------]»: доля прошедших тестов.

    Пустая — только если не прошёл ни один, полная — только если не упал ни один.
    """
    cells = round(BRIEF_BAR_CELLS * passed / total)
    if passed and not cells:
        cells = 1
    if passed < total and cells == BRIEF_BAR_CELLS:
        cells -= 1
    return "[" + "#" * cells + "-" * (BRIEF_BAR_CELLS - cells) + "]"


def _brief_section(
    bucket: str,
    title: str,
    group: list[_Problem],
    tests: _Tests,
    not_proposed: dict[str, str],
) -> list[str]:
    icon, _ = BRIEF_SECTIONS[bucket]
    lines = [f"### {icon} {title} ({len(group)})"]
    for problem in group[:MAX_BRIEF_ITEMS]:
        lines += ["", *_brief_item(problem, tests, not_proposed)]
    rest = group[MAX_BRIEF_ITEMS:]
    if rest:
        # Без номеров: голый список номеров ничего не говорит, а при тысячах проблем
        # рос бы с каждым next. Сколько их и где смотреть — достаточно.
        count = sum(p.size for p in rest)
        lines += [
            "",
            (
                f"- … и ещё {len(rest)} {_plural(len(rest), 'проблема', 'проблемы', 'проблем')} "
                f"({count} {_plural(count, 'тест', 'теста', 'тестов')}) — "
                f"полный список в report.md, раздел «{title}»"
            ),
        ]
    return lines


def _brief_lines(items: list[str], prefix: str, what: str) -> list[str]:
    """Предупреждения/замечания для терминала: не больше пяти, каждое не длиннее лимита."""
    lines = [prefix + _truncate(_one_line(item), BRIEF_NOTE_CHARS) for item in items[:MAX_BRIEF_NOTES]]
    rest = len(items) - MAX_BRIEF_NOTES
    if rest > 0:
        lines.append(f"{prefix}… и ещё {rest} {what} — в полном разборе")
    return lines


def _brief_item(problem: _Problem, tests: _Tests, not_proposed: dict[str, str]) -> list[str]:
    """Проблема в терминале — блок: «Проблема N · размер · [метки]», что случилось и строки
    с подписями в колонку: что думает агент, что делать, пример теста.

    Для правки вместо «что делать» — где она и в каком состоянии, плюс «Проверьте»,
    если есть; для «править вручную» — ещё и почему агент не правил сам.
    """
    analysis = problem.analysis
    size = f"{problem.size} {_plural(problem.size, 'тест', 'теста', 'тестов')}"
    tags = "".join(f" · [{tag}]" for tag in _brief_tags(problem))
    lines = [f"**Проблема {problem.number}** · {size}{tags}"]
    if problem.flagged:
        text = f"[{problem.tag}] разбор не прошёл проверку формата, текст — в report.md"
        lines.append(_brief_field("Агент считает:", text))
        return lines + _brief_example(problem, tests)
    # Строка без подписи: разметку модели в начале убираем до выбора первого предложения,
    # иначе «1. Заказ…» дало бы предложение «1.».
    what = first_sentence(_plain_line(_one_line(analysis.what)))
    if what:
        lines.append(_truncate(what, BRIEF_TEXT_CHARS))
    reason = _truncate(_one_line(_reason(problem)), BRIEF_CAUSE_CHARS) or problem.label
    lines.append(_brief_field("Агент считает:", f"[{problem.tag}] {reason}"))
    proposal = problem.proposal
    if problem.bucket == AGENT and proposal is not None:
        status = "уже применено" if problem.applied else "ждёт вашего «да»"
        lines.append(_brief_field("Правка:", f"`{proposal.location}` — {status}"))
        lines += [
            _brief_field("Проверьте:", _truncate(warning, BRIEF_WARNING_CHARS))
            for warning in weakening_warnings(proposal.before, proposal.after)[:1]
        ]
        return lines + _brief_example(problem, tests)
    step = analysis.first_fix_step()
    if step:
        lines.append(_brief_field("Что делать:", _truncate(step, BRIEF_STEP_CHARS)))
    if problem.bucket == MANUAL:
        reason = _truncate(_one_line(_not_fixed_reason(problem, not_proposed)), BRIEF_REASON_CHARS)
        lines.append(_brief_field("Почему не сам:", reason))
    return lines + _brief_example(problem, tests)


def _plain_line(text: str) -> str:
    """Текст модели для строки без подписи: без блочной разметки в начале."""
    return _BLOCK_MARKUP_RE.sub("", text)


def _plain_text(text: str) -> str:
    """Многострочный текст модели («Коротко»): без блочной разметки в начале строк.

    Пустые строки между абзацами сохраняются, но не больше одной подряд (строка из одной
    ограды ``` становится пустой).
    """
    lines: list[str] = []
    for raw in text.strip().splitlines():
        line = "" if _FENCE_LINE_RE.match(raw) else _plain_line(raw.strip())
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def _brief_field(label: str, text: str) -> str:
    return f"   {label:<{BRIEF_LABEL_WIDTH}}{text}"


def _brief_example(problem: _Problem, tests: _Tests) -> list[str]:
    """Один узнаваемый тест проблемы: «Например:» или «Тест:», если он единственный."""
    members = tests.of_cluster(problem.entry)
    if not members:
        return []
    name = _truncate(_one_line(str(members[0].get("name") or members[0]["test_result_id"])), BRIEF_TEST_NAME_CHARS)
    return [_brief_field("Например:" if problem.size > 1 else "Тест:", name)]


def _reason(problem: _Problem) -> str:
    """Обоснование причины: у правки — её «почему» (почему это дефект теста), иначе — из разбора."""
    proposal = problem.proposal
    if problem.bucket == AGENT and proposal is not None:
        return proposal.why
    return problem.analysis.cause_reason


def _opinion(problem: _Problem, reason: str) -> str:
    """Что агент думает о причине: категория и обоснование."""
    text = _one_line(reason)
    return f"{problem.label} — {text}" if text else problem.label


def _unconfirmed(problem: _Problem) -> str:
    return f" ({UNCONFIRMED_NOTE})" if not problem.flagged and problem.analysis.unconfirmed_by_log else ""


MIXED_GROUP_NOTE = "в группе, похоже, несколько проблем"
UNCHECKED_GROUP_NOTE = "однородность группы не проверена: недостаточно данных"


def _consistency_lines(analysis: ClusterAnalysis, prefix: str, *, bold: bool = False) -> list[str]:
    """Пометка о неоднородной группе: примеры говорят о разных проблемах или сравнить нечем."""
    kind = analysis.consistency_kind
    if kind == "different":
        title = "В группе, похоже, несколько проблем"
        text = analysis.consistency_detail
    elif kind == "insufficient":
        title = "Однородность группы не проверена"
        text = "недостаточно данных, чтобы сравнить примеры"
    else:
        return []
    return [f"{prefix}{title}:** {text}" if bold else f"{prefix}{title}: {text}"]


def _evidence_lines(analysis: ClusterAnalysis, title: str, missing_title: str) -> list[str]:
    """Цитаты с понятным источником и чего не хватает — отдельно от предположения о причине."""
    lines: list[str] = []
    # Без реестра (папка до наблюдений) источник не назвать — цитаты не показываем.
    if analysis.observations and analysis.sources is not None:
        lines.append(title)
        for item in analysis.observations:
            record = analysis.sources.get(item.source_id)
            where = describe(record) if record else item.source_id
            lines.append(f"   - «{_one_line(item.quote)}» — {where}")
    if analysis.missing_text:
        lines.append(f"{missing_title}{analysis.missing_text}")
    return lines


def _brief_tags(problem: _Problem) -> list[str]:
    tags: list[str] = []
    if not problem.flagged and problem.analysis.consistency_kind == "different":
        tags.append(MIXED_GROUP_NOTE)
    if not problem.flagged and problem.analysis.unconfirmed_by_log:
        tags.append("не подтверждено логом")
    history = problem.entry.get("history")
    if history:
        launches = history["launches"]
        tags.append(
            f"повторяется: уже была в {launches} "
            f"{_plural(launches, 'другом прогоне', 'других прогонах', 'других прогонах')}"
        )
    if not problem.flagged and problem.analysis.kb_ref:
        tags.append(f"известная проблема: {problem.analysis.kb_ref}")
    return tags


def _item_title(problem: _Problem) -> str:
    size = f"{problem.size} {_plural(problem.size, 'тест', 'теста', 'тестов')}"
    if problem.bucket == AGENT and problem.proposal is not None:
        proposal = problem.proposal
        return f"**Проблема {problem.number}** — `{proposal.location}` · {size}"
    return f"**Проблема {problem.number}** — {size}"


def _not_fixed_reason(problem: _Problem, not_proposed: dict[str, str]) -> str:
    """Почему агент не правил тест сам."""
    proposal = problem.proposal
    if proposal is not None and proposal.is_fix and problem.state == "unknown":
        return (
            f"правка в `{proposal.location}` применялась командой apply, но файл потом менялся и "
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
                f"- **Агент считает:** {_opinion(problem, analysis.cause_reason)}"
                f"{_unconfirmed(problem)}",
                *_consistency_lines(analysis, "- **", bold=True),
                *_evidence_lines(analysis, "- **Наблюдения:**", "- **Не хватает:** "),
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
        lines += [
            "",
            f"### Проблема {problem.number}: {proposal.location}{state}",
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
