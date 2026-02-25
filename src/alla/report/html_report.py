"""Генератор self-contained HTML-отчёта для alla."""

from __future__ import annotations

import html as _html
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
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.6/dist/purify.min.js"></script>
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

  <script>
    document.addEventListener("DOMContentLoaded", function() {{
      marked.setOptions({{
        breaks: true,
        gfm: true
      }});
      
      document.querySelectorAll('.markdown-source').forEach(function(el) {{
        var rendered = el.nextElementSibling;
        var html = marked.parse(el.value);
        rendered.innerHTML = DOMPurify.sanitize(html);
      }});
    }});
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Секции отчёта
# ---------------------------------------------------------------------------

def _render_stats(
    triage: "TriageReport",
    clustering: "ClusteringReport | None",
) -> str:
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
        f'<div class="llm-summary">{content}</div>'
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
            '<div class="empty">Кластеры отсутствуют.</div>'
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
        f'<div class="clusters-list">{body}</div>'
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

    # --- LLM analysis ---
    llm_html = ""
    if llm_text:
        content = _render_llm_text(llm_text)
        llm_html = (
            '<div class="block">'
            '<div class="block-title">AI Анализ</div>'
            f'<div class="llm-analysis">{content}</div>'
            "</div>"
        )

    # --- error example ---
    error_html = ""
    if cluster.example_message:
        snippet = cluster.example_message[:2000]
        if len(cluster.example_message) > 2000:
            snippet += "\n…"
        error_html = (
            '<div class="block">'
            '<div class="block-title">Пример ошибки</div>'
            '<div class="error-block">'
            f"<pre>{_e(snippet)}</pre>"
            "</div>"
            "</div>"
        )

    # --- KB matches ---
    kb_html = ""
    if kb_matches:
        entries_html = "".join(
            _render_kb_entry(m) for m in kb_matches
        )
        kb_html = (
            '<div class="block">'
            '<div class="block-title">База знаний</div>'
            f'<div class="kb-matches">{entries_html}</div>'
            "</div>"
        )

    # --- test IDs ---
    _MAX_IDS = 60
    shown_ids = cluster.member_test_ids[:_MAX_IDS]
    links: list[str] = []
    for tid in shown_ids:
        test = test_by_id.get(tid)
        if test and test.link:
            links.append(f'<a href="{_e(test.link)}" target="_blank" class="test-id">{tid}</a>')
        else:
            links.append(f'<span class="test-id no-link">{tid}</span>')

    tests_html = ""
    if links:
        extra = ""
        if len(cluster.member_test_ids) > _MAX_IDS:
            extra = f'<span class="test-more">и ещё {len(cluster.member_test_ids) - _MAX_IDS}…</span>'
        tests_html = (
            '<div class="block">'
            '<div class="block-title">Затронутые тесты</div>'
            f'<div class="test-list">{ "".join(links) }{extra}</div>'
            "</div>"
        )

    return (
        '<div class="cluster">'
        '<div class="cluster-header">'
        f'<span class="cluster-num">#{idx}</span>'
        f'<span class="cluster-label">{_e(cluster.label)}</span>'
        f'<span class="cluster-count">{cluster.member_count} тест(ов)</span>'
        "</div>"
        '<div class="cluster-body">'
        f"{llm_html}{error_html}{kb_html}{tests_html}"
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
        f'<span class="kb-score">{(m.score * 100):.0f}%</span>'
        f'<span class="kb-title">{_e(m.entry.title)}</span>'
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
    """Подготовить LLM-текст для рендеринга через marked.js на клиенте."""
    safe_text = _html.escape(text)
    return f'<textarea class="markdown-source" style="display:none;">{safe_text}</textarea><div class="markdown-rendered"></div>'


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
    :root {
      --bg: #f8fafc;
      --surface: #ffffff;
      --border: #e2e8f0;
      --text: #0f172a;
      --text-muted: #64748b;
      --primary: #2563eb;
      --primary-light: #dbeafe;
      --danger: #dc2626;
      --danger-light: #fef2f2;
      --success: #16a34a;
      --success-light: #dcfce7;
      --warning: #d97706;
      --warning-light: #fffbeb;
      --info: #0284c7;
      --info-light: #f0f9ff;
      --radius: 12px;
      --radius-sm: 8px;
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.6;
      font-size: 14px;
      -webkit-font-smoothing: antialiased;
    }

    .container {
      max-width: 1200px;
      margin: 0 auto;
      padding: 2rem 1.5rem;
    }

    /* ---- Header ---- */
    .header {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.5rem 2rem;
      margin-bottom: 2rem;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    .header-brand {
      font-size: 0.75rem;
      font-weight: 700;
      color: var(--primary);
      text-transform: uppercase;
      letter-spacing: 0.1em;
    }
    .header-title {
      font-size: 1.5rem;
      font-weight: 700;
      color: var(--text);
      line-height: 1.2;
    }
    .header-meta {
      font-size: 0.875rem;
      color: var(--text-muted);
    }

    /* ---- Stats ---- */
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 1rem;
      margin-bottom: 2rem;
    }
    .stat-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.25rem 1rem;
      text-align: center;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      transition: transform 0.2s, box-shadow 0.2s;
    }
    .stat-card:hover {
      transform: translateY(-2px);
      box-shadow: 0 4px 6px rgba(0,0,0,0.05);
    }
    .stat-value {
      font-size: 2rem;
      font-weight: 700;
      line-height: 1;
      margin-bottom: 0.5rem;
      color: var(--text);
    }
    .stat-label {
      font-size: 0.75rem;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .stat-card.danger .stat-value { color: var(--danger); }
    .stat-card.warning .stat-value { color: var(--warning); }
    .stat-card.success .stat-value { color: var(--success); }
    .stat-card.info .stat-value { color: var(--info); }
    .stat-card.muted .stat-value { color: var(--text-muted); }

    /* ---- Sections ---- */
    .section {
      margin-bottom: 2.5rem;
    }
    .section-title {
      font-size: 1.25rem;
      font-weight: 700;
      margin-bottom: 1.25rem;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .empty {
      color: var(--text-muted);
      font-style: italic;
      background: var(--surface);
      padding: 2rem;
      border-radius: var(--radius);
      text-align: center;
      border: 1px dashed var(--border);
    }

    /* ---- LLM Summary ---- */
    .llm-summary {
      background: var(--surface);
      border: 1px solid var(--border);
      border-left: 4px solid var(--primary);
      border-radius: var(--radius);
      padding: 1.5rem 2rem;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }

    /* ---- Clusters ---- */
    .cluster {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      margin-bottom: 1.5rem;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      overflow: hidden;
    }
    .cluster-header {
      background: #f8fafc;
      border-bottom: 1px solid var(--border);
      padding: 1rem 1.5rem;
      display: flex;
      align-items: center;
      gap: 1rem;
      flex-wrap: wrap;
    }
    .cluster-num {
      background: var(--primary-light);
      color: var(--primary);
      font-weight: 700;
      font-size: 0.875rem;
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
    }
    .cluster-label {
      font-weight: 600;
      font-size: 1.125rem;
      flex: 1;
      min-width: 200px;
      word-break: break-word;
    }
    .cluster-count {
      background: var(--border);
      color: var(--text);
      font-size: 0.875rem;
      font-weight: 600;
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
    }
    .cluster-body {
      padding: 1.5rem;
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }

    /* ---- Blocks ---- */
    .block {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    .block-title {
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
    }

    /* ---- Error Block ---- */
    .error-block {
      background: var(--danger-light);
      border: 1px solid #fca5a5;
      border-radius: var(--radius-sm);
      padding: 1rem;
    }
    .error-block pre {
      margin: 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.8125rem;
      color: var(--danger);
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 400px;
      overflow-y: auto;
    }

    /* ---- LLM Analysis ---- */
    .llm-analysis {
      background: var(--info-light);
      border: 1px solid #bae6fd;
      border-radius: var(--radius-sm);
      padding: 1.25rem 1.5rem;
    }

    /* ---- KB Matches ---- */
    .kb-matches {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    .kb-entry {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 1rem 1.25rem;
      box-shadow: 0 1px 2px rgba(0,0,0,0.02);
    }
    .kb-entry-header {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 0.75rem;
      flex-wrap: wrap;
    }
    .kb-score {
      background: var(--success-light);
      color: var(--success);
      font-size: 0.75rem;
      font-weight: 700;
      padding: 0.125rem 0.5rem;
      border-radius: 9999px;
    }
    .kb-title {
      font-weight: 600;
      font-size: 1rem;
      flex: 1;
    }
    .kb-category {
      font-size: 0.7rem;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      background: var(--bg);
      border: 1px solid var(--border);
      padding: 0.125rem 0.5rem;
      border-radius: 9999px;
    }
    .kb-steps {
      margin: 0;
      padding-left: 1.25rem;
      font-size: 0.875rem;
      color: var(--text);
    }
    .kb-steps li { margin-bottom: 0.25rem; }

    /* ---- Test IDs ---- */
    .test-list {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
    }
    .test-id {
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text);
      font-size: 0.8125rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      padding: 0.25rem 0.5rem;
      border-radius: 6px;
      text-decoration: none;
      transition: all 0.2s;
    }
    .test-id:hover {
      background: var(--primary-light);
      border-color: #bfdbfe;
      color: var(--primary);
    }
    .test-id.no-link {
      cursor: default;
    }
    .test-id.no-link:hover {
      background: var(--bg);
      border-color: var(--border);
      color: var(--text);
    }
    .test-more {
      font-size: 0.8125rem;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      padding: 0 0.5rem;
    }

    /* ---- Markdown Rendered Styles ---- */
    .markdown-rendered {
      font-size: 0.9375rem;
      line-height: 1.6;
      color: var(--text);
    }
    .markdown-rendered p { margin-top: 0; margin-bottom: 1rem; }
    .markdown-rendered p:last-child { margin-bottom: 0; }
    .markdown-rendered strong { font-weight: 600; color: #000; }
    .markdown-rendered code {
      background: rgba(0,0,0,0.05);
      padding: 0.2em 0.4em;
      border-radius: 4px;
      font-size: 0.875em;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    .markdown-rendered pre {
      background: #1e293b;
      color: #f8fafc;
      padding: 1rem;
      border-radius: var(--radius-sm);
      overflow-x: auto;
      font-size: 0.875em;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      margin-top: 0;
      margin-bottom: 1rem;
    }
    .markdown-rendered pre code {
      background: transparent;
      padding: 0;
      color: inherit;
    }
    .markdown-rendered ul, .markdown-rendered ol {
      margin-top: 0;
      margin-bottom: 1rem;
      padding-left: 1.5rem;
    }
    .markdown-rendered li { margin-bottom: 0.25rem; }
    .markdown-rendered h1, .markdown-rendered h2, .markdown-rendered h3, .markdown-rendered h4 {
      margin-top: 1.5rem;
      margin-bottom: 0.75rem;
      font-weight: 600;
      line-height: 1.25;
    }
    .markdown-rendered h1 { font-size: 1.5rem; }
    .markdown-rendered h2 { font-size: 1.25rem; }
    .markdown-rendered h3 { font-size: 1.125rem; }
    .markdown-rendered h4 { font-size: 1rem; }
    .markdown-rendered h1:first-child, .markdown-rendered h2:first-child, .markdown-rendered h3:first-child {
      margin-top: 0;
    }
    .markdown-rendered blockquote {
      border-left: 4px solid var(--border);
      padding-left: 1rem;
      margin-left: 0;
      margin-right: 0;
      color: var(--text-muted);
      font-style: italic;
    }

    /* ---- Footer ---- */
    .footer {
      text-align: center;
      font-size: 0.8125rem;
      color: var(--text-muted);
      margin-top: 3rem;
      padding: 1.5rem;
      border-top: 1px solid var(--border);
    }

    @media (max-width: 768px) {
      .container { padding: 1rem; }
      .header { padding: 1.25rem; }
      .stats { grid-template-columns: repeat(2, 1fr); }
      .cluster-header { flex-direction: column; align-items: flex-start; gap: 0.5rem; }
      .cluster-label { min-width: 100%; }
    }
"""
