"""Text output helpers for the alla CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alla.models.clustering import ClusteringReport
    from alla.models.llm import LLMAnalysisResult
    from alla.models.testops import FailedTestSummary, TriageReport


def print_text_report(report: "TriageReport") -> None:
    """Вывод человекочитаемого отчёта триажа в stdout."""
    print()
    print("=== Отчёт триажа Allure ===")
    launch_label = f"Запуск: #{report.launch_id}"
    if report.launch_name:
        launch_label += f" ({report.launch_name})"
    print(launch_label)
    stats = (
        f"Всего: {report.total_results}"
        f" | Успешно: {report.passed_count}"
        f" | Провалено: {report.failed_count}"
        f" | Сломано: {report.broken_count}"
        f" | Пропущено: {report.skipped_count}"
        f" | Неизвестно: {report.unknown_count}"
    )
    if report.muted_failure_count:
        stats += f" | Muted: {report.muted_failure_count}"
    print(stats)
    print()

    if report.failed_tests:
        print(f"Падения ({len(report.failed_tests)}):")
        for test in report.failed_tests:
            print(
                f"  [{test.status.upper()}]  {test.name} "
                f"(ID: {test.test_result_id})"
            )
            if test.link:
                print(f"            {test.link}")
            if test.failed_step_path:
                print(f"            Шаг: {test.failed_step_path}")
            if test.status_message:
                message = test.status_message
                if len(message) > 200:
                    message = message[:200] + "..."
                print(f"            {message}")
            if test.log_snippet:
                log_lines = test.log_snippet.strip().splitlines()
                print(f"            [LOG] {len(log_lines)} строк лога")
    else:
        print("Падения не найдены.")

    print()


def print_launch_summary(summary_text: str) -> None:
    """Вывод итогового LLM-отчёта по прогону в stdout."""
    print()
    print("=== Итоговый отчёт ===")
    print()
    print(summary_text)
    print()
    print("=" * 22)
    print()


def print_clustering_report(
    report: "ClusteringReport",
    kb_results: dict[str, list[Any]] | None = None,
    llm_result: "LLMAnalysisResult | None" = None,
    failed_tests: "list[FailedTestSummary] | None" = None,
) -> None:
    """Вывод отчёта кластеризации ошибок в stdout."""
    from alla.utils.log_utils import has_explicit_errors, parse_log_sections

    test_by_id: dict[int, FailedTestSummary] = {}
    if failed_tests:
        test_by_id = {test.test_result_id: test for test in failed_tests}

    print(
        f"=== Кластеры падений "
        f"({report.cluster_count} уникальных проблем из {report.total_failures} падений) ==="
    )
    print()

    for index, cluster in enumerate(report.clusters, 1):
        cluster_lines = [f"Кластер #{index} ({cluster.member_count} тестов)"]
        if cluster.example_step_path:
            cluster_lines.append(f"Шаг: {cluster.example_step_path}")
        if cluster.example_message:
            message = _normalize_single_line(cluster.example_message)
            if len(message) > 200:
                message = message[:200] + "..."
            cluster_lines.append(f"Пример: {message}")
        cluster_lines.extend(_wrap_test_ids(cluster.member_test_ids))

        if test_by_id and cluster.representative_test_id is not None:
            representative = test_by_id.get(cluster.representative_test_id)
            if (
                representative is not None
                and representative.log_snippet
                and representative.log_snippet.strip()
            ):
                log_snippet = representative.log_snippet
                has_errors = has_explicit_errors(log_snippet)
                total_lines = len(log_snippet.strip().splitlines())
                error_label = ", содержит ошибки" if has_errors else ""
                sections = parse_log_sections(log_snippet)
                cluster_lines.append(f"Лог ({total_lines} строк{error_label}):")
                for log_filename, log_body in sections:
                    if log_filename:
                        cluster_lines.append(f"  --- {log_filename} ---")
                    file_lines = log_body.splitlines()
                    for line in file_lines[:3]:
                        text = line if len(line) <= 120 else line[:117] + "..."
                        cluster_lines.append(f"  | {text}")
                    if len(file_lines) > 3:
                        cluster_lines.append(
                            f"  | ... ещё {len(file_lines) - 3} строк"
                        )
            else:
                cluster_lines.append("Лог: отсутствует")

        matches = (kb_results or {}).get(cluster.cluster_id, [])
        if matches:
            cluster_lines.append("")
            count_label = _pluralize_matches(len(matches))
            cluster_lines.append(f"База знаний ({count_label}):")
            for match in matches:
                cluster_lines.append(f"  [{match.score:.2f}] {match.entry.title}")
                cluster_lines.append(
                    f"         Категория: {match.entry.category}"
                )
                for step in match.entry.resolution_steps[:2]:
                    step_text = step if len(step) <= 80 else step[:77] + "..."
                    cluster_lines.append(f"         -> {step_text}")

        if llm_result is not None:
            analysis = llm_result.cluster_analyses.get(cluster.cluster_id)
            if analysis and analysis.analysis_text:
                cluster_lines.append("")
                cluster_lines.append("LLM-анализ:")
                for line in analysis.analysis_text.split("\n"):
                    cluster_lines.append(f"  {line}")

        for line in _render_box(cluster_lines):
            print(line)
        print()


def _wrap_test_ids(
    test_ids: list[int],
    max_width: int = 80,
) -> list[str]:
    """Отформатировать список ID тестов с переносом строк."""
    prefix = "Тесты: "
    indent = " " * len(prefix)

    all_ids = [str(test_id) for test_id in test_ids]
    lines: list[str] = []
    current = prefix

    for index, test_id in enumerate(all_ids):
        separator = ", " if index > 0 else ""
        candidate = current + separator + test_id

        if len(candidate) > max_width and current not in (prefix, indent):
            lines.append(current + ",")
            current = indent + test_id
        else:
            current = candidate

    lines.append(current)
    return lines


def _normalize_single_line(value: str) -> str:
    """Схлопнуть переводы строк/табуляцию в одну строку для рамочного вывода."""
    return " ".join(value.replace("\t", " ").split())


def _pluralize_matches(count: int) -> str:
    """Склонение слова 'совпадение' по числу."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} совпадение"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} совпадения"
    return f"{count} совпадений"


def _wrap_text(text: str, max_width: int, indent: str = "") -> list[str]:
    """Разбить длинную строку на несколько строк с переносом по словам."""
    if max_width <= 0:
        return [text]
    if len(text) <= max_width:
        return [text]

    stripped = text.lstrip()
    first_line_indent = text[: len(text) - len(stripped)]
    words = stripped.split()
    lines: list[str] = []
    current_line = ""

    for word in words:
        current_indent = first_line_indent if not lines and not current_line else ""
        candidate = current_indent + word if not current_line else current_line + " " + word

        if len(candidate) <= max_width:
            current_line = candidate
            continue

        if current_line:
            lines.append(current_line)
        word_indent = first_line_indent if not lines else indent
        current_line = word_indent + word
        safe_indent = indent if len(indent) < max_width else ""
        while len(current_line) > max_width:
            lines.append(current_line[:max_width])
            current_line = safe_indent + current_line[max_width:]

    if current_line:
        lines.append(current_line)

    return lines if lines else [text]


def _render_box(lines: list[str], max_width: int = 100) -> list[str]:
    """Отрендерить список строк в Unicode-рамку с переносом длинных строк."""
    if not lines:
        return []

    wrapped_lines: list[str] = []
    for line in lines:
        if len(line) <= max_width:
            wrapped_lines.append(line)
            continue
        stripped = line.lstrip()
        leading_spaces = len(line) - len(stripped)
        indent = " " * leading_spaces if leading_spaces > 0 else "  "
        wrapped_lines.extend(_wrap_text(line, max_width, indent))

    width = max(len(line) for line in wrapped_lines) if wrapped_lines else 0
    top = f"╔{'═' * (width + 2)}╗"
    bottom = f"╚{'═' * (width + 2)}╝"
    body = [f"║ {line.ljust(width)} ║" for line in wrapped_lines]

    return [top, *body, bottom]
