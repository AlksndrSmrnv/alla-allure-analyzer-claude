"""Сборка блоков «Данные» для заданий агенту: кластер и итог прогона.

Только данные. Правила и «Задание» (что и в каком формате писать) принадлежат
скиллу: ``alla_skill_lib.cluster_task`` (``clusters/NN.md``) и
``alla_skill_lib.report`` (``summary_task.md``).

Основные функции:

* :func:`build_cluster_analysis_prompt` — данные одного кластера.
* :func:`build_launch_summary_prompt` — данные для итога по прогону.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from alla_core.models.clustering import ClusteringReport, FailureCluster
from alla_core.models.llm import LLMAnalysisResult
from alla_core.models.testops import TriageReport
from alla_core.utils.log_focus import log_pieces, split_source_mark
from alla_core.utils.text_normalization import normalize_text_for_llm

__all__ = [
    "ClusterAnalysisPrompt",
    "PromptExample",
    "PromptSource",
    "build_cluster_examples_prompt",
    "LaunchSummaryPrompt",
    "build_cluster_analysis_prompt",
    "build_launch_summary_prompt",
    "DEFAULT_MESSAGE_MAX_CHARS",
    "DEFAULT_TRACE_MAX_CHARS",
    "DEFAULT_LOG_MAX_CHARS",
]

DEFAULT_MESSAGE_MAX_CHARS = 2000
DEFAULT_TRACE_MAX_CHARS = 400
DEFAULT_LOG_MAX_CHARS = 8000

DATA_HEADING = "## Данные"
_TRUNCATION_SUFFIX = "...[обрезано]"


_SECTION_HEADER_RE = re.compile(r"^--- \[(?P<kind>[^\]:\s][^\]:]*?): (?P<name>.+?)\] ---$")
_LOG_KIND_LABELS = {"файл": "лог", "HTTP": "HTTP", "журнал": "журнал"}


@dataclass(frozen=True)
class PromptSource:
    """Кусок данных задания под своим id (``S1``, ``S2``…): ровно тот текст, что видит модель.

    ``kind`` — ``message`` | ``trace`` | ``log``; у фрагмента лога — вложение
    (``attachment``, имя из заголовка секции, и ``section`` — её вид: лог, HTTP, журнал)
    и строки источника (``lines``: «строки 120–134 · повторялось …», если блок помечен).
    """

    id: str
    kind: str
    text: str
    test_name: str | None = None
    section: str | None = None
    attachment: str | None = None
    lines: str | None = None
    test_result_id: int | None = None

    def header(self) -> str:
        what = {"message": "сообщение об ошибке", "trace": "стек-трейс"}.get(self.kind)
        if what is None:
            what = f"{self.section} {self.attachment}" if self.attachment else (self.section or "лог")
        parts = [self.id, what, self.lines, f"тест {self.test_name}" if self.test_name else None]
        return "--- [" + " · ".join(part for part in parts if part) + "] ---"


@dataclass(frozen=True)
class ClusterAnalysisPrompt:
    """Данные одного кластера для задания агенту.

    Атрибуты:
        user_prompt: блок «Данные» (заголовок, кластер, ошибка, трейс, лог).
        message_chars: фактическая длина сообщения после truncation.
        trace_chars: фактическая длина трейса после truncation.
        log_chars: фактическая длина лога после truncation.
    """

    user_prompt: str
    message_chars: int
    trace_chars: int
    log_chars: int
    sources: tuple[PromptSource, ...] = ()

    @property
    def has_symptom(self) -> bool:
        """Есть сообщение об ошибке или стек-трейс (то, что увидел тест)."""
        return self.message_chars > 0 or self.trace_chars > 0

    @property
    def has_log(self) -> bool:
        """Есть фрагмент лога приложения."""
        return self.log_chars > 0


@dataclass(frozen=True)
class LaunchSummaryPrompt:
    """Данные для итогового отчёта по прогону."""

    user_prompt: str
    cluster_count: int
    analyses_used: int


# ---------------------------------------------------------------------------
# Cluster data
# ---------------------------------------------------------------------------


def build_cluster_analysis_prompt(
    cluster: FailureCluster,
    log_snippet: str | None = None,
    full_trace: str | None = None,
    *,
    message_max_chars: int = DEFAULT_MESSAGE_MAX_CHARS,
    trace_max_chars: int = DEFAULT_TRACE_MAX_CHARS,
    log_max_chars: int = DEFAULT_LOG_MAX_CHARS,
    normalize_evidence: bool = True,
    source_ids: bool = False,
    message_test: str | None = None,
    log_test: str | None = None,
    message_test_id: int | None = None,
    log_test_id: int | None = None,
) -> ClusterAnalysisPrompt:
    """Собрать данные одного кластера.

    ``normalize_evidence=False`` оставляет в трейсе и логе ID, время и IP как
    есть (без ``<ID>``/``<TS>``/``<IP>``): по ним агент связывает падение
    с конкретной операцией и восстанавливает порядок событий.

    ``source_ids=True`` — каждый кусок данных идёт под своим id: ``S1`` сообщение,
    ``S2`` трейс, дальше фрагменты лога (блоки отбора) с вложением и строками
    источника; ``message_test`` / ``log_test`` — чьи это данные. Куски возвращаются
    в ``sources`` ровно с тем текстом, что попал в задание.
    """
    parts: list[str] = [
        DATA_HEADING,
        "",
        f"Кластер: {cluster.label}",
        f"Затронуто тестов: {cluster.member_count}",
    ]
    if cluster.example_step_path:
        parts.append(f"Шаг теста: {cluster.example_step_path}")
    sources: list[PromptSource] = []

    def add_source(kind: str, text: str, test: str | None, **where: str | None) -> None:
        test_id = log_test_id if kind == "log" else message_test_id
        source = PromptSource(f"S{len(sources) + 1}", kind, text, test,
                              test_result_id=test_id, **where)
        sources.append(source)
        parts.append(f"\n{source.header()}\n{text}")

    message_chars = 0
    if cluster.example_message:
        msg = _truncate_prompt_text(cluster.example_message, message_max_chars)
        message_chars = len(msg)
        if source_ids:
            add_source("message", msg, message_test)
        else:
            parts.append(f"\n--- Сообщение об ошибке ---\n{msg}")

    trace_text = full_trace or cluster.example_trace_snippet
    trace_chars = 0
    if trace_text:
        if normalize_evidence:
            trace_text = normalize_text_for_llm(trace_text)
        trace_text = _truncate_prompt_text(trace_text, trace_max_chars)
        trace_chars = len(trace_text)
        if source_ids:
            add_source("trace", trace_text, message_test)
        else:
            parts.append(f"\n--- Стек-трейс ---\n{trace_text}")

    log_chars = 0
    if log_snippet:
        log_text = normalize_text_for_llm(log_snippet) if normalize_evidence else log_snippet
        log_text = _truncate_prompt_text(log_text, log_max_chars)
        log_chars = len(log_text)
        if source_ids:
            for piece in log_pieces(log_text):
                if piece.meta:
                    parts.append(f"\n{piece.text}")
                    continue
                mark, body = split_source_mark(piece.text)
                header = _SECTION_HEADER_RE.match(piece.header or "")
                kind = header.group("kind") if header else "файл"
                add_source(
                    "log", body, log_test,
                    section=_LOG_KIND_LABELS.get(kind, kind),
                    attachment=header.group("name") if header else None,
                    lines=mark[1:-1] if mark else None,
                )
        else:
            parts.append(f"\n--- Фрагмент лога ---\n{log_text}")

    return ClusterAnalysisPrompt(
        user_prompt="\n".join(parts),
        message_chars=message_chars,
        trace_chars=trace_chars,
        log_chars=log_chars,
        sources=tuple(sources),
    )


@dataclass(frozen=True)
class PromptExample:
    """Пример кластера для задания: тест и его данные (лог уже отобран под свой лимит)."""

    role: str  # подпись для модели: «типичный», «наиболее отличающийся»…
    test_result_id: int
    test_name: str
    step: str | None
    message: str | None
    trace: str | None
    log: str | None


def build_cluster_examples_prompt(
    cluster: FailureCluster,
    examples: list[PromptExample],
    *,
    message_max_chars: int = DEFAULT_MESSAGE_MAX_CHARS,
    trace_max_chars: int = DEFAULT_TRACE_MAX_CHARS,
    log_max_chars: int = DEFAULT_LOG_MAX_CHARS,
) -> ClusterAnalysisPrompt:
    """Данные кластера по примерам: у каждого свой блок, id кусков общие (S1, S2…).

    Один пример — тот же вид, что у :func:`build_cluster_analysis_prompt`. Несколько —
    лимиты сообщения, трейса и лога делятся: первому (типичному) половина, остальным
    поровну из остатка (:func:`example_shares`). Сообщение и трейс, совпадающие с
    первым примером, не повторяются. Лог примера ожидается уже отобранным под его долю.
    """
    parts: list[str] = [
        DATA_HEADING,
        "",
        f"Кластер: {cluster.label}",
        f"Затронуто тестов: {cluster.member_count}",
    ]
    multi = len(examples) > 1
    if not multi and examples and examples[0].step:
        parts.append(f"Шаг теста: {examples[0].step}")
    if multi:
        roles = ", ".join(example.role for example in examples)
        parts.append(f"Примеров в данных: {len(examples)} ({roles}) — тесты группы отличаются, "
                     "сравни их")
    sources: list[PromptSource] = []
    chars = {"message": 0, "trace": 0, "log": 0}

    def add_source(kind: str, text: str, example: PromptExample, **where: str | None) -> None:
        source = PromptSource(f"S{len(sources) + 1}", kind, text, example.test_name,
                              test_result_id=example.test_result_id, **where)
        sources.append(source)
        parts.append(f"\n{source.header()}\n{text}")
        chars[kind] += len(text)

    first = examples[0] if examples else None
    for number, example in enumerate(examples, start=1):
        share = example_shares(len(examples), number)
        if multi:
            parts.append(f"\n### Пример {number} — {example.role} · тест {example.test_name}")
            if example.step:
                parts.append(f"Шаг теста: {example.step}")
        same_message = (number > 1 and first is not None and bool(example.message)
                        and example.message == first.message)
        if same_message:
            parts.append("Сообщение об ошибке — такое же, как в примере 1.")
        elif example.message:
            add_source("message", _truncate_prompt_text(
                example.message, max(1, int(message_max_chars * share))), example)
        same_trace = (number > 1 and first is not None and bool(example.trace)
                      and _trace_key(example.trace) == _trace_key(first.trace))
        if same_trace:
            parts.append("Стек-трейс — такой же, как в примере 1.")
        elif example.trace:
            add_source("trace", _truncate_prompt_text(
                example.trace, max(1, int(trace_max_chars * share))), example)
        if example.log:
            log_text = _truncate_prompt_text(example.log, max(1, int(log_max_chars * share)))
            for piece in log_pieces(log_text):
                if piece.meta:
                    parts.append(f"\n{piece.text}")
                    continue
                mark, body = split_source_mark(piece.text)
                header = _SECTION_HEADER_RE.match(piece.header or "")
                kind = header.group("kind") if header else "файл"
                add_source(
                    "log", body, example,
                    section=_LOG_KIND_LABELS.get(kind, kind),
                    attachment=header.group("name") if header else None,
                    lines=mark[1:-1] if mark else None,
                )
    return ClusterAnalysisPrompt(
        user_prompt="\n".join(parts),
        message_chars=chars["message"],
        trace_chars=chars["trace"],
        log_chars=chars["log"],
        sources=tuple(sources),
    )


def example_shares(count: int, number: int) -> float:
    """Доля лимитов примера ``number`` из ``count``: первому половина, остальным поровну."""
    if count <= 1:
        return 1.0
    return 0.5 if number == 1 else 0.5 / (count - 1)


def _trace_key(trace: str | None) -> str:
    """Трейс без времени и ID: тот же — только при полном совпадении кадров."""
    return normalize_text_for_llm(trace or "").strip()


# ---------------------------------------------------------------------------
# Launch summary data
# ---------------------------------------------------------------------------


def build_launch_summary_prompt(
    clustering_report: ClusteringReport,
    triage_report: TriageReport,
    llm_result: LLMAnalysisResult | None = None,
    problem_numbers: Mapping[str, int] | None = None,
) -> LaunchSummaryPrompt:
    """Собрать данные для итогового отчёта по прогону.

    ``llm_result`` — источник разборов проблем (в скилле это сжатые разборы
    агента); у проблемы без разбора идут шаг, сообщение и трейс кластера.
    ``problem_numbers`` — номера проблем по ``cluster_id``, когда в
    ``clustering_report`` передана только часть кластеров (номер из отчёта
    должен совпадать с номером в задании); без него — порядковый номер.
    """
    parts: list[str] = [DATA_HEADING, ""]

    launch_label = f"Запуск: #{triage_report.launch_id}"
    if triage_report.launch_name:
        launch_label += f" ({triage_report.launch_name})"
    parts.append(launch_label)
    parts.append(
        f"Всего тестов: {triage_report.total_results}"
        f" | Упало: {triage_report.failure_count}"
    )
    parts.append(
        f"Уникальных проблем (кластеров): {clustering_report.cluster_count}"
    )

    analyses_used = 0
    for index, cluster in enumerate(clustering_report.clusters, 1):
        number = (problem_numbers or {}).get(cluster.cluster_id, index)
        parts.append("")
        parts.append(
            f"--- Проблема {number}: {cluster.label} "
            f"({cluster.member_count} тестов) ---"
        )
        if llm_result is not None:
            analysis = llm_result.cluster_analyses.get(cluster.cluster_id)
            if analysis and analysis.analysis_text:
                parts.append(analysis.analysis_text)
                analyses_used += 1
                continue

        if cluster.example_step_path:
            parts.append(f"Шаг теста: {cluster.example_step_path}")
        if cluster.example_message:
            msg = cluster.example_message
            if len(msg) > 500:
                msg = msg[:500] + _TRUNCATION_SUFFIX
            parts.append(f"Сообщение: {msg}")
        if cluster.example_trace_snippet:
            trace = cluster.example_trace_snippet
            if len(trace) > 800:
                trace = trace[:800] + _TRUNCATION_SUFFIX
            parts.append(f"Трейс: {trace}")

    return LaunchSummaryPrompt(
        user_prompt="\n".join(parts),
        cluster_count=clustering_report.cluster_count,
        analyses_used=analyses_used,
    )


def _truncate_prompt_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATION_SUFFIX
