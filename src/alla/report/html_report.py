"""Генератор self-contained HTML-отчёта для alla."""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alla.knowledge.models import KBMatchResult
    from alla.models.clustering import ClusteringReport, FailureCluster
    from alla.models.llm import LLMAnalysisResult, LLMLaunchSummary
    from alla.models.testops import FailedTestSummary, TriageReport
    from alla.orchestrator import AnalysisResult


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def generate_html_report(result: "AnalysisResult", endpoint: str = "") -> str:
    """Сгенерировать self-contained HTML-отчёт из AnalysisResult."""
    from alla import __version__

    triage = result.triage_report
    clustering = result.clustering_report
    kb_results = result.kb_results or {}
    llm_result = result.llm_result
    llm_summary = result.llm_launch_summary

    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    launch_title = f"Прогон #{triage.launch_id}"
    if triage.launch_name:
        launch_title += f" — {triage.launch_name}"

    stats_html = _render_stats(triage, clustering)
    summary_html = _render_launch_summary(llm_summary)
    clusters_html = _render_clusters(
        clustering, kb_results, llm_result, triage.failed_tests
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>alla — {_e(launch_title)}</title>
  <style>
{_CSS}
  </style>
</head>
<body>
  <div class="container">

    <header class="header">
      <div class="header-brand">alla · AI Test Analysis</div>
      <div class="header-title">{_e(launch_title)}</div>
      <div class="header-meta">Сгенерировано: {generated_at} · alla v{_e(__version__)}</div>
    </header>

    {stats_html}
    {summary_html}
    {clusters_html}

    <footer class="footer">
      alla v{_e(__version__)} · AI Test Failure Triage Agent · {generated_at}
    </footer>

  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Секции отчёта
# ---------------------------------------------------------------------------

def _render_stats(
    triage: "TriageReport",
    clustering: "ClusteringReport | None",
) -> str:
    failure_count = triage.failed_count + triage.broken_count
    cluster_count = str(clustering.cluster_count) if clustering else "—"

    cards: list[tuple[str, str, str]] = [
        ("Всего", str(triage.total_results), ""),
        ("Успешно", str(triage.passed_count), "success" if triage.passed_count else "muted"),
        ("Упало", str(triage.failed_count), "danger" if triage.failed_count else "muted"),
        ("Сломано", str(triage.broken_count), "warning" if triage.broken_count else "muted"),
        ("Пропущено", str(triage.skipped_count), "muted"),
        ("Кластеров", cluster_count, "info"),
    ]

    cards_html = "".join(
        f'<div class="stat-card {cls}">'
        f'<div class="stat-value">{_e(val)}</div>'
        f'<div class="stat-label">{_e(label)}</div>'
        f"</div>"
        for label, val, cls in cards
    )
    return f'<div class="stats">{cards_html}</div>'


def _render_launch_summary(llm_summary: "LLMLaunchSummary | None") -> str:
    if not llm_summary or not llm_summary.summary_text:
        return ""

    content = _render_llm_text(llm_summary.summary_text)
    return (
        '<div class="section">'
        '<div class="section-title">Итоговый анализ прогона</div>'
        f'<div class="section-body"><div class="llm-summary">{content}</div></div>'
        "</div>"
    )


def _render_clusters(
    clustering: "ClusteringReport | None",
    kb_results: dict[str, list["KBMatchResult"]],
    llm_result: "LLMAnalysisResult | None",
    failed_tests: "list[FailedTestSummary]",
) -> str:
    if not clustering:
        return ""

    test_by_id = {t.test_result_id: t for t in (failed_tests or [])}

    if not clustering.clusters:
        return (
            '<div class="section">'
            '<div class="section-title">Кластеры падений</div>'
            '<div class="section-body"><p class="empty">Кластеры отсутствуют.</p></div>'
            "</div>"
        )

    title = (
        f"Кластеры падений "
        f"({clustering.cluster_count} уникальных "
        f"{'проблема' if clustering.cluster_count == 1 else 'проблем'} "
        f"из {clustering.total_failures} падений)"
    )

    body = "".join(
        _render_cluster(i, cluster, kb_results, llm_result, test_by_id)
        for i, cluster in enumerate(clustering.clusters, 1)
    )

    return (
        '<div class="section">'
        f'<div class="section-title">{_e(title)}</div>'
        f'<div class="section-body">{body}</div>'
        "</div>"
    )


def _render_cluster(
    idx: int,
    cluster: "FailureCluster",
    kb_results: dict[str, list["KBMatchResult"]],
    llm_result: "LLMAnalysisResult | None",
    test_by_id: "dict[int, FailedTestSummary]",
) -> str:
    kb_matches = kb_results.get(cluster.cluster_id, [])

    llm_text: str | None = None
    if llm_result:
        analysis = llm_result.cluster_analyses.get(cluster.cluster_id)
        if analysis and analysis.analysis_text:
            llm_text = analysis.analysis_text

    # --- error example ---
    error_html = ""
    if cluster.example_message:
        snippet = cluster.example_message[:1200]
        if len(cluster.example_message) > 1200:
            snippet += "\n…"
        error_html = (
            '<div class="error-block">'
            f"<pre>{_e(snippet)}</pre>"
            "</div>"
        )

    # --- LLM analysis ---
    llm_html = ""
    if llm_text:
        content = _render_llm_text(llm_text)
        llm_html = (
            '<div class="block-label">LLM-анализ</div>'
            f'<div class="llm-analysis">{content}</div>'
        )

    # --- KB matches ---
    kb_html = ""
    if kb_matches:
        entries_html = "".join(
            _render_kb_entry(m) for m in kb_matches
        )
        kb_html = (
            '<div class="block-label">База знаний</div>'
            f'<div class="kb-matches">{entries_html}</div>'
        )

    # --- test IDs ---
    _MAX_IDS = 60
    shown_ids = cluster.member_test_ids[:_MAX_IDS]
    links: list[str] = []
    for tid in shown_ids:
        test = test_by_id.get(tid)
        if test and test.link:
            links.append(f'<a href="{_e(test.link)}" target="_blank">{tid}</a>')
        else:
            links.append(_e(str(tid)))

    tests_html = ""
    if links:
        extra = ""
        if len(cluster.member_test_ids) > _MAX_IDS:
            extra = f' и ещё {len(cluster.member_test_ids) - _MAX_IDS}…'
        tests_html = (
            f'<div class="test-ids">Тесты: {", ".join(links)}{extra}</div>'
        )

    return (
        '<div class="cluster">'
        '<div class="cluster-header">'
        f'<span class="cluster-num">#{idx}</span>'
        f'<span class="cluster-label">{_e(cluster.label)}</span>'
        f'<span class="cluster-count">{cluster.member_count} тест(ов)</span>'
        "</div>"
        '<div class="cluster-body">'
        f"{error_html}{llm_html}{kb_html}{tests_html}"
        "</div>"
        "</div>"
    )


def _render_kb_entry(m: "KBMatchResult") -> str:
    steps_html = ""
    if m.entry.resolution_steps:
        items = "".join(f"<li>{_e(s)}</li>" for s in m.entry.resolution_steps)
        steps_html = f'<ul class="kb-steps">{items}</ul>'

    return (
        '<div class="kb-entry">'
        '<div class="kb-entry-header">'
        f'<span class="kb-score">{m.score:.2f}</span>'
        f'<span class="kb-entry-title">{_e(m.entry.title)}</span>'
        f'<span class="kb-category">{_e(m.entry.category.value)}</span>'
        "</div>"
        f"{steps_html}"
        "</div>"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _e(s: object) -> str:
    """HTML-escape строку."""
    return _html.escape(str(s))


def _render_llm_text(text: str) -> str:
    """Конвертировать LLM-текст в HTML с базовым форматированием.

    Правила:
    - HTML-escape всего текста (защита от XSS)
    - **LABEL:** → <strong>LABEL:</strong>
    - Параграфы разделены двойным переводом строки (\n\n)
    - Одиночные переводы строки → <br>
    """
    escaped = _html.escape(text)
    # **bold** → <strong>bold</strong>
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)

    paragraphs = [p.strip() for p in escaped.split("\n\n") if p.strip()]
    html_parts = [f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs]
    return "\n".join(html_parts)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                   "Helvetica Neue", Arial, sans-serif;
      background: #f0f2f5;
      color: #1a202c;
      font-size: 14px;
      line-height: 1.65;
    }

    .container { max-width: 1080px; margin: 0 auto; padding: 28px 16px; }

    /* ---- Header ---- */
    .header {
      background: linear-gradient(135deg, #1a202c 0%, #2d3748 100%);
      color: #fff;
      border-radius: 12px;
      padding: 28px 32px;
      margin-bottom: 20px;
    }
    .header-brand {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: #63b3ed;
      margin-bottom: 8px;
    }
    .header-title { font-size: 22px; font-weight: 600; margin-bottom: 6px; }
    .header-meta { font-size: 12px; color: rgba(255,255,255,0.5); }

    /* ---- Stats ---- */
    .stats {
      display: flex;
      gap: 12px;
      margin-bottom: 20px;
      flex-wrap: wrap;
    }
    .stat-card {
      flex: 1;
      min-width: 110px;
      background: #fff;
      border-radius: 10px;
      padding: 16px 12px;
      text-align: center;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }
    .stat-value { font-size: 28px; font-weight: 700; color: #2d3748; }
    .stat-label {
      font-size: 11px;
      color: #a0aec0;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-top: 4px;
    }
    .stat-card.danger  .stat-value { color: #e53e3e; }
    .stat-card.warning .stat-value { color: #dd6b20; }
    .stat-card.success .stat-value { color: #38a169; }
    .stat-card.muted   .stat-value { color: #a0aec0; }
    .stat-card.info    .stat-value { color: #3182ce; }

    /* ---- Section ---- */
    .section {
      background: #fff;
      border-radius: 10px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
      margin-bottom: 20px;
      overflow: hidden;
    }
    .section-title {
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.7px;
      padding: 14px 20px;
      border-bottom: 1px solid #edf2f7;
      color: #718096;
    }
    .section-body { padding: 20px; }
    .empty { color: #a0aec0; font-style: italic; }

    /* ---- LLM Summary ---- */
    .llm-summary {
      background: #ebf8ff;
      border-left: 4px solid #3182ce;
      border-radius: 0 8px 8px 0;
      padding: 16px 20px;
    }
    .llm-summary p { margin-bottom: 10px; }
    .llm-summary p:last-child { margin-bottom: 0; }

    /* ---- Cluster ---- */
    .cluster {
      border: 1px solid #e2e8f0;
      border-radius: 10px;
      margin-bottom: 14px;
      overflow: hidden;
    }
    .cluster:last-child { margin-bottom: 0; }

    .cluster-header {
      background: #f7fafc;
      padding: 12px 18px;
      display: flex;
      align-items: baseline;
      gap: 10px;
      border-bottom: 1px solid #e2e8f0;
      flex-wrap: wrap;
    }
    .cluster-num {
      font-size: 11px;
      font-weight: 700;
      color: #3182ce;
      text-transform: uppercase;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .cluster-label {
      font-size: 14px;
      font-weight: 600;
      color: #1a202c;
      flex: 1;
      min-width: 0;
      word-break: break-word;
    }
    .cluster-count {
      font-size: 11px;
      color: #a0aec0;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .cluster-body { padding: 16px 18px; }

    /* ---- Error block ---- */
    .error-block {
      background: #fff5f5;
      border: 1px solid #fed7d7;
      border-radius: 6px;
      padding: 12px;
      margin-bottom: 14px;
      overflow-x: auto;
    }
    .error-block pre {
      font-family: "JetBrains Mono", "Consolas", "Courier New", monospace;
      font-size: 12px;
      color: #c53030;
      white-space: pre-wrap;
      word-break: break-word;
    }

    /* ---- Block label ---- */
    .block-label {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #718096;
      margin-bottom: 8px;
      margin-top: 4px;
    }

    /* ---- LLM analysis ---- */
    .llm-analysis {
      background: #ebf8ff;
      border-left: 3px solid #3182ce;
      border-radius: 0 6px 6px 0;
      padding: 12px 16px;
      margin-bottom: 14px;
    }
    .llm-analysis p { font-size: 13px; margin-bottom: 8px; }
    .llm-analysis p:last-child { margin-bottom: 0; }

    /* ---- KB matches ---- */
    .kb-matches { margin-bottom: 14px; }
    .kb-entry {
      background: #f7fafc;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      padding: 10px 14px;
      margin-bottom: 6px;
    }
    .kb-entry:last-child { margin-bottom: 0; }
    .kb-entry-header {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 6px;
      flex-wrap: wrap;
    }
    .kb-score {
      background: #ebf8ff;
      color: #2b6cb0;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 10px;
      white-space: nowrap;
    }
    .kb-entry-title { font-size: 13px; font-weight: 600; flex: 1; }
    .kb-category {
      font-size: 11px;
      color: #a0aec0;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .kb-steps { margin-top: 6px; padding-left: 18px; }
    .kb-steps li { font-size: 12px; color: #4a5568; margin-bottom: 3px; }

    /* ---- Test IDs ---- */
    .test-ids { font-size: 12px; color: #a0aec0; margin-top: 10px; line-height: 1.8; }
    .test-ids a { color: #3182ce; text-decoration: none; }
    .test-ids a:hover { text-decoration: underline; }

    /* ---- Footer ---- */
    .footer {
      text-align: center;
      font-size: 11px;
      color: #cbd5e0;
      margin-top: 28px;
      padding: 12px;
    }

    @media (max-width: 600px) {
      .stats { gap: 8px; }
      .stat-card { min-width: 80px; padding: 12px 8px; }
      .stat-value { font-size: 22px; }
      .header { padding: 20px 18px; }
      .header-title { font-size: 18px; }
    }
"""
