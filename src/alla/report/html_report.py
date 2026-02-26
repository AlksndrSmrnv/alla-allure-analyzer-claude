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

def generate_html_report(
    result: "AnalysisResult",
    endpoint: str = "",
    feedback_api_url: str = "",
) -> str:
    """Сгенерировать self-contained HTML-отчёт из AnalysisResult."""
    from alla import __version__

    triage = result.triage_report
    clustering = result.clustering_report
    kb_results = result.kb_results or {}
    llm_result = result.llm_result
    llm_summary = result.llm_launch_summary
    error_fingerprints = result.error_fingerprints or {}

    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    launch_title = f"Прогон #{triage.launch_id}"
    if triage.launch_name:
        launch_title += f" — {triage.launch_name}"

    stats_html = _render_stats(triage, clustering)
    summary_html = _render_launch_summary(llm_summary)
    clusters_html = _render_clusters(
        clustering,
        kb_results,
        llm_result,
        triage.failed_tests,
        feedback_api_url=feedback_api_url,
        error_fingerprints=error_fingerprints,
        launch_id=triage.launch_id,
        project_id=triage.project_id,
    )

    feedback_css = _FEEDBACK_CSS if feedback_api_url else ""
    feedback_js = _build_feedback_js(feedback_api_url) if feedback_api_url else ""

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>alla — {_e(launch_title)}</title>
  <script src="https://cdn.jsdelivr.net/npm/marked@15.0.4/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.6/dist/purify.min.js"></script>
  <style>
{_CSS}
{feedback_css}
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
      if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {{
        document.querySelectorAll('.markdown-source').forEach(function(el) {{
          var rendered = el.nextElementSibling;
          var html = marked.parse(el.value, {{ breaks: true, gfm: true }});
          rendered.innerHTML = DOMPurify.sanitize(html);
        }});
      }}
    }});
  </script>
{feedback_js}
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
    *,
    feedback_api_url: str = "",
    error_fingerprints: dict[str, str] | None = None,
    launch_id: int = 0,
    project_id: int | None = None,
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

    fp_map = error_fingerprints or {}

    body = "".join(
        _render_cluster(
            i, cluster, kb_results, llm_result, test_by_id,
            feedback_api_url=feedback_api_url,
            error_fingerprint=fp_map.get(cluster.cluster_id, ""),
            launch_id=launch_id,
            project_id=project_id,
        )
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
    *,
    feedback_api_url: str = "",
    error_fingerprint: str = "",
    launch_id: int = 0,
    project_id: int | None = None,
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
            _render_kb_entry(
                m,
                error_fingerprint=error_fingerprint,
                feedback_api_url=feedback_api_url,
                launch_id=launch_id,
                cluster_id=cluster.cluster_id,
            )
            for m in kb_matches
        )
        kb_html = (
            '<div class="block">'
            '<div class="block-title">База знаний</div>'
            f'<div class="kb-matches">{entries_html}</div>'
            "</div>"
        )

    # --- create KB entry form ---
    create_kb_html = ""
    if feedback_api_url:
        prefill_error = _e(cluster.example_message or "")
        pid = _e(str(project_id)) if project_id is not None else ""
        create_kb_html = (
            '<div class="block">'
            '<button class="create-kb-toggle" '
            'onclick="this.nextElementSibling.classList.toggle(\'hidden\')">'
            '+ Создать запись в базу знаний</button>'
            f'<form class="create-kb-form hidden" data-api-url="{_e(feedback_api_url)}">'
            '<label>Slug ID:<input name="id" required pattern="[a-z0-9_]+" placeholder="my_error_slug"></label>'
            '<label>Заголовок:<input name="title" required placeholder="Описание проблемы"></label>'
            '<label>Категория:<select name="category">'
            '<option value="test">test</option>'
            '<option value="service">service</option>'
            '<option value="env">env</option>'
            '<option value="data">data</option>'
            '</select></label>'
            f'<label>Пример ошибки:<textarea name="error_example" rows="6">{prefill_error}</textarea></label>'
            '<label>Описание:<textarea name="description" rows="3" placeholder="Подробное описание"></textarea></label>'
            '<label>Шаги по устранению (по одному на строку):'
            '<textarea name="resolution_steps" rows="3" placeholder="Шаг 1&#10;Шаг 2"></textarea></label>'
            f'<input type="hidden" name="project_id" value="{pid}">'
            '<button type="submit" class="create-kb-submit">Создать запись</button>'
            '<span class="create-kb-status"></span>'
            '</form>'
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
        f"{llm_html}{error_html}{kb_html}{create_kb_html}{tests_html}"
        "</div>"
        "</div>"
    )


def _render_kb_entry(
    m: "KBMatchResult",
    *,
    error_fingerprint: str = "",
    feedback_api_url: str = "",
    launch_id: int = 0,
    cluster_id: str = "",
) -> str:
    steps_html = ""
    if m.entry.resolution_steps:
        items = "".join(f"<li>{_e(s)}</li>" for s in m.entry.resolution_steps)
        steps_html = f'<ul class="kb-steps">{items}</ul>'

    feedback_html = ""
    if feedback_api_url and error_fingerprint:
        entry_id = _e(str(m.entry.entry_id)) if m.entry.entry_id is not None else _e(m.entry.id)
        feedback_html = (
            f'<div class="kb-feedback" '
            f'data-entry-id="{entry_id}" '
            f'data-fingerprint="{_e(error_fingerprint)}" '
            f'data-launch-id="{_e(str(launch_id))}" '
            f'data-cluster-id="{_e(cluster_id)}">'
            '<button class="fb-btn fb-like" title="Полезное совпадение">&#x2713; Полезно</button>'
            '<button class="fb-btn fb-dislike" title="Неверное совпадение">&#x2717; Не то</button>'
            '<span class="fb-status"></span>'
            "</div>"
        )

    return (
        '<div class="kb-entry">'
        '<div class="kb-entry-header">'
        f'<span class="kb-score">{(m.score * 100):.0f}%</span>'
        f'<span class="kb-title">{_e(m.entry.title)}</span>'
        f'<span class="kb-category">{_e(m.entry.category.value)}</span>'
        "</div>"
        f"{steps_html}"
        f"{feedback_html}"
        "</div>"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _e(s: object) -> str:
    """HTML-escape строку."""
    return _html.escape(str(s))


def _render_llm_text(text: str) -> str:
    """Подготовить LLM-текст для рендеринга через marked.js на клиенте с фоллбэком."""
    safe_text = _html.escape(text)
    
    # Базовый фоллбэк (серверный рендеринг) для оффлайн-режима и CSP
    fallback = safe_text
    fallback = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", fallback)
    paragraphs = [p.strip() for p in fallback.split("\n\n") if p.strip()]
    fallback_html = "\n".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
    
    return (
        f'<textarea class="markdown-source" style="display:none;">{safe_text}</textarea>'
        f'<div class="markdown-rendered">{fallback_html}</div>'
    )


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

# ---------------------------------------------------------------------------
# Feedback CSS (appended only when feedback_api_url is provided)
# ---------------------------------------------------------------------------

_FEEDBACK_CSS = """
    /* ---- KB Feedback Buttons ---- */
    .kb-feedback{display:flex;align-items:center;gap:.5rem;margin-top:.75rem;padding-top:.75rem;border-top:1px solid var(--border)}
    .fb-btn{display:inline-flex;align-items:center;gap:.25rem;padding:.375rem .75rem;border:1px solid var(--border);border-radius:6px;background:var(--surface);cursor:pointer;font-size:.8125rem;color:var(--text-muted);transition:all .2s}
    .fb-btn:hover{border-color:var(--primary);color:var(--primary)}
    .fb-btn.fb-active.fb-like{border-color:var(--success);color:var(--success);background:#f0fdf4}
    .fb-btn.fb-active.fb-dislike{border-color:var(--danger);color:var(--danger);background:#fef2f2}
    .fb-status{font-size:.75rem;color:var(--text-muted)}
    .fb-status-ok{color:var(--success)}
    .fb-status-error{color:var(--danger)}

    /* ---- Create KB Entry Form ---- */
    .create-kb-toggle{background:none;border:1px dashed var(--border);border-radius:var(--radius-sm);padding:.5rem 1rem;cursor:pointer;color:var(--text-muted);font-size:.875rem;width:100%;text-align:left;transition:all .2s}
    .create-kb-toggle:hover{border-color:var(--primary);color:var(--primary)}
    .create-kb-form{display:flex;flex-direction:column;gap:.75rem;padding:1rem;border:1px solid var(--border);border-radius:var(--radius-sm);margin-top:.5rem}
    .create-kb-form.hidden{display:none}
    .create-kb-form label{display:flex;flex-direction:column;gap:.25rem;font-size:.8125rem;font-weight:600;color:var(--text-muted)}
    .create-kb-form input,.create-kb-form textarea,.create-kb-form select{font-family:inherit;font-size:.875rem;padding:.5rem;border:1px solid var(--border);border-radius:6px;background:var(--bg)}
    .create-kb-submit{align-self:flex-start;padding:.5rem 1.5rem;background:var(--primary);color:white;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:.875rem}
    .create-kb-submit:hover{opacity:.9}
    .create-kb-submit:disabled{opacity:.5;cursor:not-allowed}
    .create-kb-ok{color:var(--success);font-size:.875rem}
    .create-kb-error{color:var(--danger);font-size:.875rem}
"""


# ---------------------------------------------------------------------------
# Feedback JavaScript builder
# ---------------------------------------------------------------------------

def _build_feedback_js(feedback_api_url: str) -> str:
    """Return a <script> block for feedback interactions.

    Only called when *feedback_api_url* is non-empty.
    """
    import json as _json
    # json.dumps produces a valid JS string literal (handles \, ", newlines, etc.)
    # _html.escape must NOT be used here: inside <script>, the browser does not
    # decode HTML entities, so & → &amp; would corrupt any URL with query params.
    js_url = _json.dumps(feedback_api_url)  # includes surrounding double-quotes
    return (
        "<script>\n"
        "(function() {\n"
        "  var FEEDBACK_API_URL = " + js_url + ";\n"
        "  if (!FEEDBACK_API_URL) return;\n"
        "\n"
        "  // --- Like / Dislike ---\n"
        "  function sendFeedback(el, isLike) {\n"
        "    var wrap = el.closest('.kb-feedback');\n"
        "    var status = wrap.querySelector('.fb-status');\n"
        "    var body = JSON.stringify({\n"
        "      kb_entry_id: parseInt(wrap.dataset.entryId, 10),\n"
        "      error_fingerprint: wrap.dataset.fingerprint,\n"
        "      launch_id: parseInt(wrap.dataset.launchId, 10) || null,\n"
        "      cluster_id: wrap.dataset.clusterId || null,\n"
        "      vote: isLike ? 'like' : 'dislike'\n"
        "    });\n"
        "    status.textContent = '...';\n"
        "    status.className = 'fb-status';\n"
        "    fetch(FEEDBACK_API_URL + '/api/v1/kb/feedback', {\n"
        "      method: 'POST',\n"
        "      headers: {'Content-Type': 'application/json'},\n"
        "      body: body\n"
        "    }).then(function(r) {\n"
        "      if (!r.ok) throw new Error(r.status);\n"
        "      return r.json();\n"
        "    }).then(function() {\n"
        "      wrap.querySelectorAll('.fb-btn').forEach(function(b) { b.classList.remove('fb-active'); });\n"
        "      el.classList.add('fb-active');\n"
        "      status.textContent = 'Saved';\n"
        "      status.className = 'fb-status fb-status-ok';\n"
        "    }).catch(function(err) {\n"
        "      status.textContent = 'Error: ' + err.message;\n"
        "      status.className = 'fb-status fb-status-error';\n"
        "    });\n"
        "  }\n"
        "\n"
        "  document.addEventListener('click', function(e) {\n"
        "    var btn = e.target.closest('.fb-like');\n"
        "    if (btn) { sendFeedback(btn, true); return; }\n"
        "    btn = e.target.closest('.fb-dislike');\n"
        "    if (btn) { sendFeedback(btn, false); return; }\n"
        "  });\n"
        "\n"
        "  // --- Create KB Entry ---\n"
        "  document.addEventListener('submit', function(e) {\n"
        "    var form = e.target.closest('.create-kb-form');\n"
        "    if (!form) return;\n"
        "    e.preventDefault();\n"
        "    var submitBtn = form.querySelector('.create-kb-submit');\n"
        "    var status = form.querySelector('.create-kb-status');\n"
        "    submitBtn.disabled = true;\n"
        "    status.textContent = '...';\n"
        "    status.className = 'create-kb-status';\n"
        "    var steps = (form.elements.resolution_steps.value || '').split('\\n').filter(function(s) { return s.trim(); });\n"
        "    var body = JSON.stringify({\n"
        "      id: form.elements.id.value,\n"
        "      title: form.elements.title.value,\n"
        "      category: form.elements.category.value,\n"
        "      error_example: form.elements.error_example.value,\n"
        "      description: form.elements.description.value,\n"
        "      resolution_steps: steps,\n"
        "      project_id: parseInt(form.elements.project_id.value, 10) || null\n"
        "    });\n"
        "    var apiUrl = form.dataset.apiUrl || FEEDBACK_API_URL;\n"
        "    fetch(apiUrl + '/api/v1/kb/entries', {\n"
        "      method: 'POST',\n"
        "      headers: {'Content-Type': 'application/json'},\n"
        "      body: body\n"
        "    }).then(function(r) {\n"
        "      if (!r.ok) throw new Error(r.status);\n"
        "      return r.json();\n"
        "    }).then(function() {\n"
        "      status.textContent = 'Создано!';\n"
        "      status.className = 'create-kb-status create-kb-ok';\n"
        "      submitBtn.disabled = true;\n"
        "    }).catch(function(err) {\n"
        "      status.textContent = 'Error: ' + err.message;\n"
        "      status.className = 'create-kb-status create-kb-error';\n"
        "      submitBtn.disabled = false;\n"
        "    });\n"
        "  });\n"
        "\n"
        "  // --- Load existing votes on page load ---\n"
        "  document.addEventListener('DOMContentLoaded', function() {\n"
        "    var fps = {};\n"
        "    document.querySelectorAll('.kb-feedback[data-fingerprint]').forEach(function(el) {\n"
        "      fps[el.dataset.fingerprint] = true;\n"
        "    });\n"
        "    Object.keys(fps).forEach(function(fp) {\n"
        "      fetch(FEEDBACK_API_URL + '/api/v1/kb/feedback/' + encodeURIComponent(fp))\n"
        "        .then(function(r) { return r.ok ? r.json() : null; })\n"
        "        .then(function(data) {\n"
        "          if (!data) return;\n"
        "          // data is {entry_id_str: 'like'|'dislike'}\n"
        "          Object.keys(data).forEach(function(entryIdStr) {\n"
        "            var vote = data[entryIdStr];\n"
        "            document.querySelectorAll('.kb-feedback[data-fingerprint=\"' + fp + '\"]').forEach(function(wrap) {\n"
        "              if (wrap.dataset.entryId === entryIdStr) {\n"
        "                var cls = vote === 'like' ? '.fb-like' : '.fb-dislike';\n"
        "                var btn = wrap.querySelector(cls);\n"
        "                if (btn) btn.classList.add('fb-active');\n"
        "              }\n"
        "            });\n"
        "          });\n"
        "        })\n"
        "        .catch(function() {});\n"
        "    });\n"
        "  });\n"
        "})();\n"
        "</script>"
    )
