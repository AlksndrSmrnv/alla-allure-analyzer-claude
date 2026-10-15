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
from alla_skill_lib.known_issues import KnownIssue, KnownIssues, KnownRecord
from alla_skill_lib.proposals import Proposal, weakening_warnings
from alla_skill_lib.related import Related, RelatedGroup
from alla_skill_lib.retries import MAX_REPORT_PASSED, passed_after_retry, report_line, retry_facts
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
# Известные проблемы в данных сводки: самые большие группы строкой, остальные — одной строкой.
MAX_SUMMARY_GROUPS = 10
# Свёрнутая известная или объединённая проблема в кратком разборе: номера, дальше «и ещё N».
MAX_BRIEF_GROUP_NUMBERS = 5
# Другие проблемы той же известной проблемы в карточке проблемы report.md, дальше «и ещё N».
MAX_REPORT_GROUP_NUMBERS = 10
# Номера проблем одной известной проблемы в данных сводки, дальше «и ещё N».
MAX_SUMMARY_GROUP_NUMBERS = 20
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
Если в данных есть блок «Известные проблемы из базы знаний», проблемы из одной его строки —
одна причина: называй их одной проблемой с номерами из блока («проблема 3 и 7 и 12 — одна
известная проблема: …»), а не разными сбоями.
Если в данных есть блок «Объединённые проблемы», проблемы из одной его строки агент счёл одной
причиной, сравнив разборы, и отчёт показывает их одной проблемой: называй их так же, одной
проблемой с номерами из строки («проблема 4 и 9: …»), но не известной проблемой —
пользователь этого не подтверждал. Сколько всего проблем — бери из строки «Проблем после
объединения», а не из шапки данных. Последняя строка этого блока «Кроме них, ещё N таких
групп…» — разные причины, их между собой не объединяй.
Проблема со строкой «СОГЛАСОВАННОСТЬ: разные проблемы» — не одна проблема: в её тестах,
похоже, разные сбои. Так и скажи, назови их причины из этой строки и не выдавай одну из них
за причину всей проблемы.
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
    # Запись базы знаний, подтверждённая для разбора, и расхождение категории с ней.
    known: KnownRecord | None = None
    mismatch: KnownRecord | None = None
    group: KnownIssue | None = None
    rejected: KnownRecord | None = None
    # Группа «похоже, одна причина» (решение модели после сравнения разборов).
    related: RelatedGroup | None = None
    # Вывод расширения, который подтвердил пользователь (``confirmed/NN.json``).
    confirmed: dict[str, Any] | None = None

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
        if self.confirmed is not None:
            return CATEGORY_LABELS[self.confirmed["category"]]
        if self.flagged or self.analysis.category is None:
            return FLAGGED_LABEL
        return CATEGORY_LABELS[self.analysis.category]

    @property
    def tag(self) -> str:
        """Категория ASCII-меткой для краткого разбора."""
        if self.confirmed is not None:
            return CATEGORY_TAGS[self.confirmed["category"]]
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
    known: KnownIssues | None = None,
    related: Related | None = None,
    separate: set[str] | None = None,
    confirmed: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Общий блок данных для задания сводки и проверки её актуальности.

    Полные разборы занимали почти всё задание, а сводке нужны только причина,
    суть и первый шаг исправления. Подробно (``compact``) идут самые большие
    проблемы, следующие — одной строкой причины, остальные — одной сводной
    строкой по причинам: размер задания ограничен при любом числе проблем.
    «ЗАДАНИЕ» ядра (перечислять все проблемы) заменено своим: список проблем в
    отчёте строит код. ``separate`` — проблемы с правкой агента: в отчёте они не
    объединяются (у каждой своя команда ``apply``), от этого зависит число проблем.
    """
    triage, clustering = load_models(run)
    assert clustering is not None
    largest = sorted(run["clusters"], key=lambda entry: -entry["member_count"])
    detailed = {entry["file_id"] for entry in largest[:MAX_SUMMARY_DETAILED]}
    top = {entry["file_id"] for entry in largest[:MAX_SUMMARY_LISTED]}
    listed = [entry for entry in run["clusters"] if entry["file_id"] in top]  # порядок номеров
    listed_ids = {entry["cluster_id"] for entry in listed}
    rejected = known.rejected if known else {}
    cluster_analyses = {
        entry["cluster_id"]: LLMClusterAnalysis(
            cluster_id=entry["cluster_id"],
            analysis_text=_summary_text(
                analyses[entry["file_id"]],
                entry["file_id"] in flagged,
                entry["file_id"] in detailed,
            ) + (f"\n({REJECTED_SUMMARY_NOTE})" if entry["file_id"] in rejected else "")
            + _confirmed_summary((confirmed or {}).get(entry["file_id"])),
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
        *(["", _summary_rest(rest, analyses, flagged, rejected)] if rest else []),
        *(["", *_summary_known(known)] if known and known.groups else []),
        *(["", *_summary_related(related, run, separate or set())] if related and related.groups else []),
    ])


def build_summary_task(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    paths: RunPaths,
    *,
    data: str | None = None,
    known: KnownIssues | None = None,
) -> str:
    """Данные сводки с правилами, путями и заданием для агента."""
    if data is None:
        data = build_summary_data(run, analyses, flagged, known)
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
    rejected: dict[str, KnownRecord],
) -> str:
    """Проблемы, не вошедшие в задание по одной, — одной строкой: сколько их и по каким причинам.

    Разборы по отвергнутой записи считаются и здесь: иначе ``reject`` маленькой проблемы
    не менял бы данные сводки, и она осталась бы прежней.
    """
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
    unconfirmed = sum(1 for entry in rest if entry["file_id"] in rejected)
    note = (
        f"; у {unconfirmed} из них разбор опирался на запись базы знаний, которую пользователь "
        "потом отверг, — их причина не подтверждена" if unconfirmed else ""
    )
    return (
        f"--- Ещё {len(rest)} {_plural(len(rest), 'проблема', 'проблемы', 'проблем')} поменьше "
        f"({total} {_plural(total, 'тест', 'теста', 'тестов')}), по причинам: "
        + "; ".join(parts) + note + " ---"
    )


def _summary_known(known: KnownIssues) -> list[str]:
    """Известные проблемы из базы знаний: какие проблемы отчёта — одна причина."""
    lines = ["--- Известные проблемы из базы знаний (подтверждены пользователем) ---"]
    for group in known.groups[:MAX_SUMMARY_GROUPS]:
        record = group.record
        category = f" [{record.category}]" if record.category else ""
        step = _truncate(_one_line(record.first_step), SUMMARY_LABEL_CHARS)
        lines.append(
            f"«{_truncate(_one_line(record.title), SUMMARY_LABEL_CHARS)}»{category}: "
            f"{_numbers_phrase(group.file_ids, MAX_SUMMARY_GROUP_NUMBERS)} — {group.size} "
            f"{_plural(group.size, 'тест', 'теста', 'тестов')}"
            + (f"; первый шаг рецепта: {step}" if step else "")
        )
    rest = known.groups[MAX_SUMMARY_GROUPS:]
    if rest:
        problems = sum(len(group.file_ids) for group in rest)
        tests = sum(group.size for group in rest)
        count = len(rest)
        kinds = _plural(count, "известная проблема", "известные проблемы", "известных проблем")
        verb = "объединяет" if _plural(count, "1", "", "") else "объединяют"
        lines.append(
            f"Кроме них, ещё {count} {kinds} поменьше (у каждой своя причина) {verb} "
            f"{problems} {_plural(problems, 'проблему', 'проблемы', 'проблем')} "
            f"({tests} {_plural(tests, 'тест', 'теста', 'тестов')})."
        )
    return lines


def merged_count(run: dict[str, Any], related: Related, separate: set[str]) -> int:
    """Число проблем в отчёте после объединения: проблемы одной группы — одна проблема.

    Проблемы из ``separate`` (правка агента) не объединяются. Остальные проблемы группы
    одной категории и попадают в один раздел отчёта — там они и становятся одним пунктом.
    """
    count = len(run["clusters"])
    for group in related.groups:
        merged = [file_id for file_id in group.file_ids if file_id not in separate]
        count -= max(len(merged) - 1, 0)
    return count


def _summary_related(related: Related, run: dict[str, Any], separate: set[str]) -> list[str]:
    """Объединённые проблемы: какие проблемы агент счёл одной причиной, сравнив разборы.

    Проблемы с правкой агента (``separate``) отчёт не объединяет — строка называет их отдельно,
    иначе сводка говорила бы об одной проблеме там, где отчёт показывает две.
    """
    sizes = {entry["file_id"]: int(entry["member_count"]) for entry in run["clusters"]}
    lines = [
        "--- Объединённые проблемы (агент счёл одной причиной, сравнив разборы; пользователь "
        "не подтверждал) ---",
        f"Проблем после объединения: {merged_count(run, related, separate)}",
    ]
    for group in related.groups[:MAX_SUMMARY_GROUPS]:
        fixed = [file_id for file_id in group.file_ids if file_id in separate]
        merged = [file_id for file_id in group.file_ids if file_id not in separate]
        cause = _truncate(_one_line(group.cause), SUMMARY_LABEL_CHARS)
        if len(merged) >= 2:
            # Тесты — того пункта, что показывает отчёт; у проблем с правкой — своё число.
            size = sum(sizes[file_id] for file_id in merged)
            line = (f"{_merged_title(merged, MAX_SUMMARY_GROUP_NUMBERS).lower()} — {cause} — {size} "
                    f"{_plural(size, 'тест', 'теста', 'тестов')}")
            if fixed:
                others = sum(sizes[file_id] for file_id in fixed)
                line += (f"; та же причина {_at_problems(fixed, MAX_SUMMARY_GROUP_NUMBERS)} (ещё "
                         f"{others} {_plural(others, 'тест', 'теста', 'тестов')}), у них своя правка "
                         "агента, в отчёте они отдельно")
        else:
            # Объединять в отчёте нечего: правка есть не у всех, называем только тех, у кого есть.
            size = sum(sizes[file_id] for file_id in group.file_ids)
            line = (f"{_numbers_phrase(group.file_ids, MAX_SUMMARY_GROUP_NUMBERS)} — {cause} — {size} "
                    f"{_plural(size, 'тест', 'теста', 'тестов')}; в отчёте они отдельно: своя правка "
                    f"агента {_at_problems(fixed, MAX_SUMMARY_GROUP_NUMBERS)}")
        lines.append(line)
    rest = related.groups[MAX_SUMMARY_GROUPS:]
    if rest:
        problems = sum(len(group.file_ids) for group in rest)
        lines.append(
            f"Кроме них, ещё {len(rest)} {_plural(len(rest), 'такая группа', 'такие группы', 'таких групп')} "
            f"({problems} {_plural(problems, 'проблема', 'проблемы', 'проблем')}), у каждой своя причина."
        )
    return lines


def _merged_title(file_ids: list[str], limit: int | None = None) -> str:
    """«Проблема 4 и 5 и 8»; с лимитом — «Проблема 1 и 3 и 4 и 7 и 9 и ещё 3»."""
    numbers = [str(int(file_id)) for file_id in file_ids]
    shown = numbers if limit is None else numbers[:limit]
    text = "Проблема " + " и ".join(shown)
    if len(numbers) > len(shown):
        text += f" и ещё {len(numbers) - len(shown)}"
    return text


def _numbers_phrase(file_ids: list[str], limit: int | None = None) -> str:
    """«проблемы 3, 7, 12»; с лимитом — «проблемы 3, 7, 12, 15, 20 и ещё 4»."""
    numbers = [str(int(file_id)) for file_id in file_ids]
    shown = numbers if limit is None else numbers[:limit]
    text = "проблемы " + ", ".join(shown)
    if len(numbers) > len(shown):
        text += f" и ещё {len(numbers) - len(shown)}"
    return text


CONFIRMED_SUMMARY_NOTE = "подтверждено инженером по коду приложения"


def _confirmed_summary(confirmed: dict[str, Any] | None) -> str:
    """Подтверждённый вывод в данных сводки: он важнее разбора модели и меняет хэш сводки."""
    if confirmed is None:
        return ""
    cause = _truncate(_one_line(str(confirmed["cause"])), SUMMARY_SHORT_CAUSE_CHARS)
    return f"\n({CONFIRMED_SUMMARY_NOTE}: {confirmed['category']} — {cause})"


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
    known: KnownIssues | None = None,
    related: Related | None = None,
    confirmed: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Вернуть (краткий текст для консоли, полный текст report.md).

    ``proposals`` — принятые предложения (и «исправить», и «не трогать»);
    ``apply_states`` — ``applied_state`` каждой правки (нет записи = не применена);
    ``not_proposed`` — почему по проблеме «тест» правку не предлагали;
    ``known`` — подтверждённые известные проблемы (``known_issues``); без него ссылка
    разбора на запись базы знаний показывается как есть, без групп;
    ``related`` — группы «одна причина» (``related.py``): проблемы группы в одном разделе
    показываются одним пунктом «Проблема 4 и 5 и 8» и считаются одной проблемой;
    ``confirmed`` — выводы расширений, подтверждённые пользователем (``confirmed.py``): их
    категория важнее разбора модели.
    """
    proposals = proposals or {}
    apply_states = apply_states or {}
    tests = _Tests.of(run)
    problems = [
        _problem(entry, analyses[entry["file_id"]], flagged, proposals, apply_states, known,
                 (confirmed or {}).get(entry["file_id"]))
        for entry in run["clusters"]
    ]
    if related is not None:
        for problem in problems:
            problem.related = related.group_of(problem.entry["file_id"])
    reasons = not_proposed or {}
    link = _report_link(paths)

    groups = [
        (bucket, title, _units(bucket, [p for p in problems if p.bucket == bucket]))
        for bucket, title, _ in SECTIONS
    ]
    groups = [(bucket, title, units) for bucket, title, units in groups if units]
    count = sum(len(units) for _, _, units in groups)
    brief = _brief_header(run, groups, count)
    summary = _plain_text(summary)
    brief += ["", "### Коротко", summary, "", "---"]
    for bucket, title, group in groups:
        brief += ["", *_brief_section(bucket, title, group, tests, reasons)]
    if notes:
        brief += ["", "### Замечания", *_brief_lines(notes, "- ", "замечаний")]
    brief += ["", "---", "", FEEDBACK_INVITATION, "", link]

    full = _header(run, count)
    full += ["", "### Коротко", summary]
    if known and known.groups:
        full += ["", *_known_section(known, problems)]
    hints = {bucket: hint for bucket, _, hint in SECTIONS}
    for bucket, title, units in groups:
        full += ["", *_section(title, hints[bucket], units, tests, reasons)]
    full += _passed_after_retry_section(run)
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
    lines = [*_header(run), "", message, *_passed_after_retry_section(run)]
    console = "\n".join([*lines, "", _report_link(paths)])
    return console, "\n".join(lines) + "\n"


def _problem(
    entry: dict[str, Any],
    analysis: ClusterAnalysis,
    flagged: set[str],
    proposals: dict[str, Proposal],
    apply_states: dict[str, str],
    known: KnownIssues | None = None,
    confirmed: dict[str, Any] | None = None,
) -> _Problem:
    file_id = entry["file_id"]
    is_flagged = file_id in flagged
    proposal = proposals.get(file_id)
    state = apply_states.get(file_id, "not_applied")
    if proposal is not None and proposal.is_fix and not is_flagged:
        # Состояние неизвестно: apply её не применит, обещать «агент поправит» нельзя.
        bucket = MANUAL if state == "unknown" else AGENT
    elif confirmed is not None:
        # Пользователь подтвердил вывод расширения: раздел — по его категории.
        bucket = {"приложение": ATTENTION, "тест": MANUAL}.get(confirmed["category"], ENVIRONMENT)
    elif (is_flagged or analysis.category in (None, "приложение", "неизвестно")
          or analysis.consistency_kind == "different"):
        bucket = ATTENTION
    elif analysis.category == "тест":
        bucket = MANUAL
    else:
        bucket = ENVIRONMENT
    if known is not None:
        return _Problem(
            entry, analysis, is_flagged, proposal, state, bucket,
            known=known.refs.get(file_id),
            mismatch=known.mismatched.get(file_id),
            group=known.group_of(file_id),
            rejected=known.rejected.get(file_id),
            confirmed=confirmed,
        )
    # Без проверки по базе знаний (вызов без known): ссылка разбора как есть.
    ref = None if is_flagged or not analysis.kb_ref else KnownRecord(
        kb_dir="", id=analysis.kb_ref, title=analysis.kb_ref, category=None, description="",
        first_step="", verified=False,
    )
    return _Problem(entry, analysis, is_flagged, proposal, state, bucket, known=ref,
                    confirmed=confirmed)


def _sort_key(problem: _Problem) -> tuple[int, int, int]:
    """Внутри раздела: сначала ошибки приложения, затем большие проблемы."""
    likely_bug = problem.bucket == ATTENTION and not problem.flagged and (
        problem.analysis.category == "приложение"
    )
    return (0 if likely_bug else 1, -problem.size, problem.number)


def _units(bucket: str, group: list[_Problem]) -> list[list[_Problem]]:
    """Пункты раздела: проблема или проблемы одной группы «одна причина» (``related``).

    Проблемы группы в разделе становятся одним пунктом. В «Агент может поправить сам» не
    объединяются: у каждой своя правка и своя команда ``apply``.
    """
    units: list[list[_Problem]] = []
    placed: dict[str, list[_Problem]] = {}
    for problem in sorted(group, key=_sort_key):
        key = problem.related.file_ids[0] if problem.related is not None and bucket != AGENT else None
        if key is not None and key in placed:
            placed[key].append(problem)
            continue
        unit = [problem]
        units.append(unit)
        if key is not None:
            placed[key] = unit
    return sorted(units, key=lambda unit: (
        min(_sort_key(p)[0] for p in unit), -_size(unit), min(p.number for p in unit),
    ))


def _size(unit: list[_Problem]) -> int:
    return sum(p.size for p in unit)


def _largest(unit: list[_Problem]) -> _Problem:
    """Самая большая проблема пункта: её разбор и тест представляют объединённый пункт."""
    return max(unit, key=lambda p: (p.size, -p.number))


def _merged_scope(run: dict[str, Any], count: int | None) -> str:
    """«-> 5 проблем (8 до объединения похожих)»: число проблем отчёта."""
    total = len(run["clusters"])
    problems = total if count is None else count
    text = f"{problems} {_plural(problems, 'проблема', 'проблемы', 'проблем')}"
    if problems < total:
        text += f" ({total} до объединения похожих)"
    return text


def _header(run: dict[str, Any], count: int | None = None) -> list[str]:
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
    if run["clusters"]:
        active = counts["active_failures"]
        scope = (
            f"В разборе: {active} {_plural(active, 'упавший тест', 'упавших теста', 'упавших тестов')}"
            f" → {_merged_scope(run, count)}"
        )
        lines.append(scope + _muted_note(counts) + ".")
    if passed := len(passed_after_retry(run)):
        lines.append(f"Прошли после повтора: {passed} — в разбор не входят, список ниже.")
    return lines + [f"Внимание: {warning}" for warning in run.get("warnings", [])]


def _known_section(known: KnownIssues, problems: list[_Problem]) -> list[str]:
    """«Известные проблемы из базы знаний» в report.md: какие проблемы — одна причина."""
    labels = {p.number: p.label for p in problems}
    lines = [
        f"### Известные проблемы из базы знаний ({len(known.groups)})",
        ("Причина этих проблем уже записана в базе знаний проекта, а разборы её подтвердили. "
         "Ниже проблемы остаются под своими номерами."),
    ]
    for group in known.groups:
        record = group.record
        label = labels.get(int(group.file_ids[0]), "")
        where = f", модуль {record.module}" if known.several_modules and record.module else ""
        size = f"{group.size} {_plural(group.size, 'тест', 'теста', 'тестов')}"
        lines += [
            "",
            (f"- «{_one_line(record.title)}» (`{record.id}`{where}; {label}) — "
             f"{_numbers_phrase(group.file_ids)} · {size}"),
        ]
        if record.description:
            lines.append(f"   Причина: {record.description}")
        if record.first_step:
            lines.append(f"   Что делать: {_one_line(record.first_step)}")
    return lines


def _section(
    title: str,
    hint: str,
    units: list[list[_Problem]],
    tests: _Tests,
    not_proposed: dict[str, str],
) -> list[str]:
    lines = [f"### {title} ({len(units)})", hint]
    for unit in units:
        item = _item(unit[0], tests, not_proposed) if len(unit) == 1 else _merged_item(unit, tests, not_proposed)
        lines += ["", *item]
    return lines


MERGED_TAG = "объединил агент"


def _merged_item(unit: list[_Problem], tests: _Tests, not_proposed: dict[str, str]) -> list[str]:
    """Проблемы одной причины в report.md: общая причина из решения агента, разбор и шаги
    самой большой проблемы, места в коде и тесты всех. Разбор каждой — в подробностях."""
    group = unit[0].related
    assert group is not None
    largest = _largest(unit)
    analysis = largest.analysis
    size = _size(unit)
    numbers = [p.entry["file_id"] for p in sorted(unit, key=lambda p: p.number)]
    unconfirmed = next((_unconfirmed(p) for p in unit if _unconfirmed(p)), "")
    lines = [
        f"**{_merged_title(numbers)}** — {size} {_plural(size, 'тест', 'теста', 'тестов')} · {MERGED_TAG}",
        f"- Общая причина: {largest.label} — {_one_line(group.cause)}{unconfirmed}",
    ]
    # Текст — из разбора одной проблемы: подпись говорит, из какой (её тестов меньше, чем в пункте).
    what = _one_line(analysis.what)
    if what:
        lines.append(f"- Что случилось (по разбору проблемы {largest.number}): {what}")
    lines += _fix_lines(analysis, f"Что делать (по разбору проблемы {largest.number}):")
    code = list(dict.fromkeys(line for p in unit for line in p.analysis.code))
    if code:
        lines.append("- Где в коде: " + "; ".join(code))
    if largest.bucket == MANUAL:
        lines.append(f"- Почему агент не правил сам: {_not_fixed_reason(largest, not_proposed)}")
    others = _outside(group, unit)
    if others:
        lines.append(f"- Та же причина, по мнению агента, и {_at_problems(others, MAX_REPORT_GROUP_NUMBERS)}")
    listed = _test_links([test for p in unit for test in tests.of_cluster(p.entry)], size)
    if listed:
        lines.append(f"- Тесты: {listed}")
    lines.append(f"- Разбор каждой проблемы — в подробностях ниже: {_numbers_phrase(numbers)}")
    return lines


def _at_problems(file_ids: list[str], limit: int) -> str:
    """«у проблемы 4» / «у проблем 4, 5 и ещё 2»."""
    numbers = [str(int(file_id)) for file_id in file_ids]
    text = ("у проблемы " if len(numbers) == 1 else "у проблем ") + ", ".join(numbers[:limit])
    if len(numbers) > limit:
        text += f" и ещё {len(numbers) - limit}"
    return text


def _outside(group: RelatedGroup, unit: list[_Problem]) -> list[str]:
    """Проблемы группы, оставшиеся в другом пункте (например, с правкой агента)."""
    inside = {p.entry["file_id"] for p in unit}
    return [file_id for file_id in group.file_ids if file_id not in inside]


def _item(problem: _Problem, tests: _Tests, not_proposed: dict[str, str]) -> list[str]:
    analysis = problem.analysis
    lines = [_item_title(problem)]
    if problem.flagged:
        lines.append("- Разбор не прошёл проверку формата, его текст — в подробностях ниже.")
    else:
        what = _one_line(analysis.what)
        if what:
            lines.append(f"- Что случилось: {what}")
        lines.append(f"- {_opinion_label(problem)} {_opinion(problem, _reason(problem))}{_unconfirmed(problem)}")
        lines += _consistency_lines(analysis, "- ")
        lines += _evidence_lines(analysis, "- Наблюдения:", "- Не хватает: ")
    lines += _confirmed_lines(problem)
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
        lines += _fix_lines(analysis, "Разбор предлагал:" if problem.rejected else "Что делать:")
        if analysis.code:
            lines.append("- Где в коде: " + "; ".join(analysis.code))
    if problem.bucket == MANUAL:
        lines.append(f"- Почему агент не правил сам: {_not_fixed_reason(problem, not_proposed)}")
    listed = _test_links(tests.of_cluster(problem.entry), problem.size)
    if listed:
        lines.append(f"- Тесты: {listed}")
    lines += _history_lines(problem)
    return lines


CONFIRMED_TAG = "подтверждено по коду"
CONFIRMED_SOURCES = {"sut-code": "разбор по коду приложения"}


def _confirmed_lines(problem: _Problem) -> list[str]:
    """Подтверждённый пользователем вывод расширения: причина, детали и шаги."""
    confirmed = problem.confirmed
    if confirmed is None:
        return []
    source = CONFIRMED_SOURCES.get(str(confirmed.get("extension")),
                                  f"расширение {confirmed.get('extension')}")
    when = f", {confirmed['confirmed']}" if confirmed.get("confirmed") else ""
    lines = [f"- **Подтверждено инженером** ({source}{when}): "
             f"{CATEGORY_LABELS[confirmed['category']]} — {_one_line(str(confirmed['cause']))}"]
    lines += [f"   - {_one_line(str(row))}" for row in confirmed.get("details", [])]
    steps = [_one_line(str(step)) for step in confirmed.get("fix", []) if str(step).strip()]
    if steps:
        lines += ["- Как исправить (подтверждено):",
                  *(f"   {number}. {step}" for number, step in enumerate(steps, start=1))]
    return lines


def _fix_lines(analysis: ClusterAnalysis, label: str = "Что делать:") -> list[str]:
    """«Что делать»: все шаги разбора."""
    steps = [line for line in analysis.fix.splitlines() if line.strip()]
    if len(steps) <= 1:  # единственный шаг — в строку, без номера «1.»
        return [f"- {label} {analysis.first_fix_step()}"] if steps else []
    return [f"- {label}", *(f"   {line.strip()}" for line in steps)]


# --- краткий разбор для терминала -------------------------------------------


def _muted_note(counts: dict[str, Any]) -> str:
    muted = counts.get("muted_failures", 0)
    if not muted:
        return ""
    noun = _plural(muted, "отключённый muted-тест", "отключённых muted-теста", "отключённых muted-тестов")
    return f" (ещё {muted} {noun} не учитываем)"


def _brief_header(
    run: dict[str, Any],
    groups: list[tuple[str, str, list[list[_Problem]]]],
    count: int,
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
    if passed := len(passed_after_retry(run)):
        lines.append(f"Прошли после повтора: {passed} (список в полном разборе)")
    if run["clusters"]:
        active = counts["active_failures"]
        scope = (
            f"{active} {_plural(active, 'упавший тест', 'упавших теста', 'упавших тестов')}"
            f" -> {_merged_scope(run, count)}"
        )
        lines.append(scope + _muted_note(counts) + ":")
        width = max(len(name) for _, name in BRIEF_SECTIONS.values()) + 4
        for bucket, _, units in groups:
            icon, name = BRIEF_SECTIONS[bucket]
            size = sum(_size(unit) for unit in units)
            lines.append(
                f"   {icon} {name} {'.' * (width - len(name))} {len(units)} "
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
    units: list[list[_Problem]],
    tests: _Tests,
    not_proposed: dict[str, str],
) -> list[str]:
    icon, _ = BRIEF_SECTIONS[bucket]
    lines = [f"### {icon} {title} ({len(units)})"]
    folds = _brief_units(bucket, units)
    for fold in folds[:MAX_BRIEF_ITEMS]:
        if len(fold) > 1:
            item = _brief_known([unit[0] for unit in fold], tests)
        elif len(fold[0]) > 1:
            item = _brief_merged(fold[0], tests, not_proposed)
        else:
            item = _brief_item(fold[0][0], tests, not_proposed)
        lines += ["", *item]
    rest = [unit for fold in folds[MAX_BRIEF_ITEMS:] for unit in fold]
    if rest:
        # Без номеров: голый список номеров ничего не говорит, а при тысячах проблем
        # рос бы с каждым next. Сколько их и где смотреть — достаточно.
        count = sum(_size(unit) for unit in rest)
        lines += [
            "",
            (
                f"- … и ещё {len(rest)} {_plural(len(rest), 'проблема', 'проблемы', 'проблем')} "
                f"({count} {_plural(count, 'тест', 'теста', 'тестов')}) — "
                f"полный список в report.md, раздел «{title}»"
            ),
        ]
    return lines


def _brief_units(bucket: str, units: list[list[_Problem]]) -> list[list[list[_Problem]]]:
    """Пункты раздела брифа: пункт отчёта или несколько проблем одной известной проблемы.

    Проблемы одной известной проблемы в разделе сворачиваются в один пункт на месте первой
    из них; каждая остаётся проблемой в счётчиках. Объединённых пунктов это не касается:
    у известной проблемы группы «одна причина» нет (``related.candidate_groups``).
    В «Агент может поправить сам» не сворачиваются: у каждой своя правка и своя команда.
    """
    folds: list[list[list[_Problem]]] = []
    placed: dict[tuple[str, str], list[list[_Problem]]] = {}
    for unit in units:
        problem = unit[0]
        known = len(unit) == 1 and problem.group is not None and bucket != AGENT
        key = problem.group.record.key if known and problem.group is not None else None
        if key is not None and key in placed:
            placed[key].append(unit)
            continue
        fold = [unit]
        folds.append(fold)
        if key is not None:
            placed[key] = fold
    return folds


def _brief_known(unit: list[_Problem], tests: _Tests) -> list[str]:
    """Свёрнутая известная проблема: номера, запись базы знаний, её первый шаг и пример теста."""
    first = unit[0]
    assert first.group is not None
    record = first.group.record
    size = _size(unit)
    numbers = _merged_title([p.entry["file_id"] for p in unit], MAX_BRIEF_GROUP_NUMBERS)
    lines = [
        (f"**{numbers}** · {size} {_plural(size, 'тест', 'теста', 'тестов')} "
         f"· [известная проблема: {record.id}]"),
        _truncate(f"Одна известная проблема: «{_plain_line(_one_line(record.title))}»", BRIEF_TEXT_CHARS),
    ]
    reason = record.description or _one_line(_reason(first))
    lines.append(_brief_field("База знаний:", f"[{first.tag}] {_truncate(_plain_line(reason), BRIEF_CAUSE_CHARS)}"))
    step = _one_line(record.first_step) or first.analysis.first_fix_step()
    if step:
        lines.append(_brief_field("Что делать:", _truncate(_plain_line(step), BRIEF_STEP_CHARS)))
    return lines + _brief_example(_largest(unit), tests)


def _brief_merged(unit: list[_Problem], tests: _Tests, not_proposed: dict[str, str]) -> list[str]:
    """Проблемы одной причины в терминале: номера, общая причина из решения агента, шаг и
    тест самой большой проблемы."""
    group = unit[0].related
    assert group is not None
    largest = _largest(unit)
    size = _size(unit)
    numbers = [p.entry["file_id"] for p in sorted(unit, key=lambda p: p.number)]
    tags = [MERGED_TAG, *_merged_tags(unit)]
    others = _outside(group, unit)
    if others:
        tags.append(f"та же причина {_at_problems(others, MAX_BRIEF_GROUP_NUMBERS)}")
    lines = [
        # Все номера (группа — не больше MAX_RELATE_GROUP): по ним пользователь даёт обратную связь.
        f"**{_merged_title(numbers)}** · {size} "
        f"{_plural(size, 'тест', 'теста', 'тестов')}" + "".join(f" · [{tag}]" for tag in tags)
    ]
    what = first_sentence(_plain_line(_one_line(largest.analysis.what)))
    if what:
        lines.append(_truncate(what, BRIEF_TEXT_CHARS))
    cause = _truncate(_plain_line(_one_line(group.cause)), BRIEF_CAUSE_CHARS)
    lines.append(_brief_field("Общая причина:", f"[{largest.tag}] {cause}"))
    step = largest.analysis.first_fix_step()
    if step:
        lines.append(_brief_field("Что делать:", _truncate(step, BRIEF_STEP_CHARS)))
    if largest.bucket == MANUAL:
        reason = _truncate(_one_line(_not_fixed_reason(largest, not_proposed)), BRIEF_REASON_CHARS)
        lines.append(_brief_field("Почему не сам:", reason))
    return lines + _brief_example(largest, tests)


def _merged_tags(unit: list[_Problem]) -> list[str]:
    """Метки участников объединённого пункта без повторов; повтор — по самому частому."""
    tags = [tag for p in unit for tag in _brief_tags(p, related=False, history=False)]
    repeated = max(unit, key=lambda p: (p.entry.get("history") or {}).get("launches", 0))
    return list(dict.fromkeys([*tags, *_history_tags(repeated)]))


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
    if problem.flagged and problem.confirmed is None:
        text = f"[{problem.tag}] разбор не прошёл проверку формата, текст — в report.md"
        lines.append(_brief_field("Агент считает:", text))
        return lines + _brief_example(problem, tests)
    # Строка без подписи: разметку модели в начале убираем до выбора первого предложения,
    # иначе «1. Заказ…» дало бы предложение «1.».
    what = first_sentence(_plain_line(_one_line(analysis.what)))
    if what:
        lines.append(_truncate(what, BRIEF_TEXT_CHARS))
    if problem.confirmed is not None:
        cause = _truncate(_one_line(str(problem.confirmed["cause"])), BRIEF_CAUSE_CHARS)
        lines.append(_brief_field("Подтверждено:", f"[{problem.tag}] {cause}"))
        steps = [str(step) for step in problem.confirmed.get("fix", []) if str(step).strip()]
        if steps:
            lines.append(_brief_field("Что делать:", _truncate(_one_line(steps[0]), BRIEF_STEP_CHARS)))
        if problem.bucket == MANUAL:
            reason = _truncate(_one_line(_not_fixed_reason(problem, not_proposed)), BRIEF_REASON_CHARS)
            lines.append(_brief_field("Почему не сам:", reason))
        return lines + _brief_example(problem, tests)
    reason = _truncate(_one_line(_reason(problem)), BRIEF_CAUSE_CHARS) or problem.label
    lines.append(_brief_field(_opinion_label(problem), f"[{problem.tag}] {reason}"))
    proposal = problem.proposal
    if problem.bucket == AGENT and proposal is not None:
        status = "уже применено" if problem.applied else "ждёт вашего «да»"
        lines.append(_brief_field("Правка:", f"`{proposal.location}` — {status}"))
        lines += [
            _brief_field("Проверьте:", _truncate(warning, BRIEF_WARNING_CHARS))
            for warning in weakening_warnings(proposal.before, proposal.after)[:1]
        ]
        return lines + _brief_example(problem, tests)
    # Шаги разбора по отвергнутой записи лечат не подтверждённую причину — не советуем их.
    step = analysis.first_fix_step() if problem.rejected is None else ""
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
    name = _one_line(str(members[0].get("name") or members[0]["test_result_id"]))
    return [_brief_field("Например:" if problem.size > 1 else "Тест:", name)]


def _reason(problem: _Problem) -> str:
    """Обоснование причины: у правки — её «почему» (почему это дефект теста), иначе — из разбора."""
    proposal = problem.proposal
    if problem.bucket == AGENT and proposal is not None:
        return proposal.why
    return problem.analysis.cause_reason


def _opinion_label(problem: _Problem) -> str:
    """Подпись мнения агента: о разборе по отвергнутой записи — в прошедшем времени."""
    return "Агент считал:" if problem.rejected is not None else "Агент считает:"


def _opinion(problem: _Problem, reason: str) -> str:
    """Что агент думает о причине: категория и обоснование."""
    text = _one_line(reason)
    return f"{problem.label} — {text}" if text else problem.label


def _unconfirmed(problem: _Problem) -> str:
    return f" ({UNCONFIRMED_NOTE})" if not problem.flagged and problem.analysis.unconfirmed_by_log else ""


MIXED_GROUP_NOTE = "в группе, похоже, несколько проблем"
UNCHECKED_GROUP_NOTE = "однородность группы не проверена: недостаточно данных"
SOURCES_LOST_NOTE = "цитата не сверена с данными: файл источников задания не найден"


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
    # Без реестра (разбор auto-кластера) источник не назвать — цитаты не показываем.
    # Реестр потерян — цитаты показываем, но предупреждаем, что они не сверены с данными.
    if analysis.observations and (analysis.sources is not None or analysis.sources_lost):
        lines.append(title)
        for item in analysis.observations:
            record = (analysis.sources or {}).get(item.source_id)
            where = describe(record) if record else f"{item.source_id}, {SOURCES_LOST_NOTE}"
            lines.append(f"   - «{_one_line(item.quote)}» — {where}")
    if analysis.missing_text:
        lines.append(f"{missing_title}{analysis.missing_text}")
    return lines


def _history_tags(problem: _Problem) -> list[str]:
    history = problem.entry.get("history")
    if not history:
        return []
    launches = history["launches"]
    return [
        f"повторяется: уже была в {launches} "
        f"{_plural(launches, 'другом прогоне', 'других прогонах', 'других прогонах')}"
    ]


def _brief_tags(problem: _Problem, *, related: bool = True, history: bool = True) -> list[str]:
    tags: list[str] = [CONFIRMED_TAG] if problem.confirmed is not None else []
    if not problem.flagged and problem.analysis.consistency_kind == "different":
        tags.append(MIXED_GROUP_NOTE)
    if not problem.flagged and problem.analysis.unconfirmed_by_log:
        tags.append("не подтверждено логом")
    if history:
        tags += _history_tags(problem)
    if problem.known is not None:
        tags.append(f"известная проблема: {problem.known.id}")
    if problem.mismatch is not None:
        tags.append(f"расходится с базой знаний: {problem.mismatch.id}")
    if problem.rejected is not None:
        tags.append(REJECTED_TAG)
    if related and problem.related is not None:
        others = _others(problem.related, problem.number)
        tags.append(f"та же причина {_at_problems(others, MAX_BRIEF_GROUP_NUMBERS)} ({MERGED_TAG})")
    return tags


def _others(group: RelatedGroup, number: int) -> list[str]:
    return [file_id for file_id in group.file_ids if int(file_id) != number]


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
    if problem.confirmed is not None:
        return "причину подтвердил инженер по коду приложения — правку автотеста агент не готовил"
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
    lines += _known_lines(problem)
    if problem.related is not None:
        others = _others(problem.related, problem.number)
        lines.append(
            f"- Агент счёл одной причиной с {_with_problems(others)}: {_one_line(problem.related.cause)}"
        )
    return lines


def _with_problems(file_ids: list[str]) -> str:
    """«проблемой 4» / «проблемами 4, 5» (группа — не больше ``MAX_RELATE_GROUP`` проблем)."""
    numbers = ", ".join(str(int(file_id)) for file_id in file_ids)
    return ("проблемой " if len(file_ids) == 1 else "проблемами ") + numbers


REJECTED_TAG = "разбор опирался на отвергнутую запись базы знаний"
REJECTED_SUMMARY_NOTE = (
    "разбор опирался на запись базы знаний, которую пользователь потом отверг для этой "
    "проблемы: причина не подтверждена, не называй её установленной"
)


def _known_lines(problem: _Problem) -> list[str]:
    """Известная проблема у проблемы в report.md: запись, группа или расхождение с записью."""
    if problem.group is not None and problem.known is not None:
        # У каждой проблемы — не больше MAX_REPORT_GROUP_NUMBERS номеров: иначе отчёт рос бы
        # квадратично с группой. Полный список — в разделе известных проблем.
        others = [str(int(file_id)) for file_id in problem.group.file_ids if int(file_id) != problem.number]
        shown = others[:MAX_REPORT_GROUP_NUMBERS]
        together = ("с проблемой " if len(others) == 1 else "с проблемами ") + ", ".join(shown)
        if len(others) > len(shown):
            together += (
                f" и ещё {len(others) - len(shown)} (все — в разделе «Известные проблемы "
                "из базы знаний»)"
            )
        return [(
            f"- Известная проблема: {problem.known.id} — «{_one_line(problem.known.title)}», "
            f"вместе {together}"
        )]
    if problem.known is not None:
        return [f"- Известная проблема: {problem.known.id} (есть в базе знаний проекта)"]
    if problem.rejected is not None:
        return [(
            f"- Разбор опирался на запись базы знаний {problem.rejected.id} "
            f"(«{_one_line(problem.rejected.title)}»), но пользователь отверг её для этой проблемы — "
            "причина не подтверждена; если она известна, сохраните её в базу знаний"
        )]
    record = problem.mismatch
    if record is None:
        return []
    named = problem.analysis.category or "не названа"
    return [(
        f"- Разбор ссылается на известную проблему {record.id} («{_one_line(record.title)}»), "
        f"но называет другую причину ({named}; в базе знаний — {record.category}) — "
        "проверьте разбор или запись"
    )]


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
                (f"- **{_opinion_label(problem)}** {_opinion(problem, analysis.cause_reason)}"
                 f"{_unconfirmed(problem)}"),
                *_consistency_lines(analysis, "- **", bold=True),
                *_evidence_lines(analysis, "- **Наблюдения:**", "- **Не хватает:** "),
                "- **Разбор предлагал:**" if problem.rejected else "- **Как исправить:**",
                *(f"   {line}" for line in analysis.fix.splitlines()),
            ]
            if analysis.code:
                lines.append("- **Где в коде:** " + "; ".join(analysis.code))
        if problem.bucket == MANUAL:
            reason = _not_fixed_reason(problem, not_proposed)
            lines.append(f"- **Почему агент не правил сам:** {reason}")
        lines += _history_lines(problem)
        members = tests.of_cluster(problem.entry)
        retries = report_line(retry_facts(members))
        if retries:
            lines.append(f"- **Повторы:** {retries}")
        lines.append("- **Тесты:**")
        for test in members[:MAX_DETAIL_TESTS]:
            lines.append(f"   - {_test_link(test)}")
        if problem.size > len(members[:MAX_DETAIL_TESTS]):
            hidden = problem.size - len(members[:MAX_DETAIL_TESTS])
            lines.append(f"   - и ещё {hidden} — полный список в TestOps: {launch_url}")
    return lines


def _passed_after_retry_section(run: dict[str, Any]) -> list[str]:
    """«Прошли после повтора» в report.md: тесты, которые в разбор не входят."""
    passed = passed_after_retry(run)
    if not passed:
        return []
    lines = [
        "",
        f"### Прошли после повтора ({len(passed)})",
        ("Эти тесты упали, но прошли при повторе в этом же прогоне. В разбор они не входят; "
         "нестабильность стоит проверить отдельно."),
    ]
    for item in passed[:MAX_REPORT_PASSED]:
        count = int(item.get("failed_attempts") or 0)
        attempts = _plural(count, "неудачная попытка", "неудачные попытки", "неудачных попыток")
        line = f"- {_test_link(item)} — {count} {attempts}"
        if item.get("message"):
            line += f": «{_truncate(_one_line(str(item['message'])), 200)}»"
        lines.append(line)
    if len(passed) > MAX_REPORT_PASSED:
        lines.append(f"- … и ещё {len(passed) - MAX_REPORT_PASSED}")
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
