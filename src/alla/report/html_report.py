"""Генератор self-contained HTML-отчёта для Alla."""

from __future__ import annotations

import html as _html
import json as _json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypedDict

from alla.models.onboarding import OnboardingMode, OnboardingState
from alla.utils.text_normalization import canonicalize_kb_error_example

if TYPE_CHECKING:
    from alla.knowledge.models import KBMatchResult
    from alla.models.clustering import ClusteringReport, FailureCluster
    from alla.models.llm import LLMAnalysisResult, LLMLaunchSummary
    from alla.models.testops import FailedTestSummary, TriageReport
    from alla.orchestrator import AnalysisResult


class _KBPrefill(TypedDict):
    title: str
    category: str
    error_example: str
    step_path: str
    description: str
    resolution_steps: list[str]


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
    feedback_contexts = result.feedback_contexts or {}
    onboarding = result.onboarding

    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    launch_title = f"Прогон #{triage.launch_id}"
    if triage.launch_name:
        launch_title += f" — {triage.launch_name}"

    stats_html = _render_stats(triage, clustering)
    onboarding_html = _render_onboarding(
        onboarding,
        clustering,
        feedback_api_url=feedback_api_url,
    )
    summary_html = _render_launch_summary(llm_summary)
    clusters_html = _render_clusters(
        clustering,
        kb_results,
        llm_result,
        triage.failed_tests,
        onboarding=onboarding,
        feedback_api_url=feedback_api_url,
        launch_id=triage.launch_id,
        project_id=triage.project_id,
    )

    feedback_css = _FEEDBACK_CSS if feedback_api_url else ""
    feedback_js = _build_feedback_js(feedback_api_url) if feedback_api_url else ""

    # Embed exact feedback contexts as JS data for vote submission / resolve
    feedback_data_js = ""
    if feedback_api_url and feedback_contexts:
        safe_payload = {
            cluster_id: context.model_dump()
            for cluster_id, context in feedback_contexts.items()
        }
        safe_data = _json.dumps(safe_payload, ensure_ascii=False).replace(
            "</", "<\\/"
        )
        feedback_data_js = (
            f"<script>var CLUSTER_FEEDBACK_CONTEXTS = {safe_data};</script>\n"
        )

    csp_meta = ""
    if feedback_api_url:
        safe_url = _e(feedback_api_url)
        csp_meta = (
            f'  <meta http-equiv="Content-Security-Policy" content="'
            f"default-src 'self'; "
            f"script-src 'self' 'unsafe-inline'; "
            f"style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' {safe_url}; "
            f"img-src 'self' data:;"
            f'">\n'
        )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
{csp_meta}  <title>Alla — {_e(launch_title)}</title>
  <style>
{_CSS}
{feedback_css}
  </style>
</head>
<body>
  <div class="container">

    <header class="header">
      <div class="header-brand">Alla · AI Test Analysis</div>
      <div class="header-title">{_e(launch_title)}</div>
      <div class="header-meta">Сгенерировано: {generated_at} · Alla v{_e(__version__)}</div>
    </header>

    {stats_html}
    {onboarding_html}
    {summary_html}
    {clusters_html}

    <footer class="footer">
      Alla v{_e(__version__)} · AI Test Failure Triage Agent · {generated_at}
    </footer>

  </div>

{feedback_data_js}{feedback_js}
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


def _render_onboarding(
    onboarding: OnboardingState,
    clustering: "ClusteringReport | None",
    *,
    feedback_api_url: str = "",
) -> str:
    """Показать onboarding-banner или setup-callout над кластерами."""
    if onboarding.mode == OnboardingMode.NORMAL:
        return ""

    if onboarding.mode == OnboardingMode.KB_NOT_CONFIGURED:
        return (
            '<div class="section">'
            '<div class="onboarding-banner setup" data-onboarding-banner>'
            '<div class="onboarding-kicker">Проектная память отключена</div>'
            '<div class="onboarding-title">Alla ещё не может учиться на ваших кластерах</div>'
            '<div class="onboarding-copy">'
            'Кластеризация и AI-анализ уже работают, но база знаний проекта и '
            'обратная связь недоступны, пока не задан <code>ALLURE_KB_POSTGRES_DSN</code>.'
            '</div>'
            "</div>"
            "</div>"
        )

    interactive_note = (
        '<div class="onboarding-note">'
        'Интерактивное обучение из HTML-отчёта доступно через alla-server: '
        'задайте <code>ALLURE_FEEDBACK_SERVER_URL</code>.'
        "</div>"
        if not feedback_api_url
        else ""
    )
    starter_pack_note = (
        '<div class="onboarding-note">'
        'Глобальные seeded-совпадения спрятаны в optional starter pack и не влияют '
        'на главный first-run UX.'
        "</div>"
        if onboarding.starter_pack_available
        else ""
    )
    return (
        '<div class="section">'
        '<div class="onboarding-banner guided" data-onboarding-banner>'
        '<div class="onboarding-title">Alla ещё не знает этот проект</div>'
        '<div class="onboarding-copy">'
        'Сначала инструмент показывает реальные кластеры ошибок, которые найдены '
        'по результатам Allure отчета.'
        '</div>'
        '<div class="onboarding-next-step">'
        '<div class="onboarding-next-step-label">Следующий шаг - ваш</div>'
        '<div class="onboarding-next-step-text">'
        'Опишите каждый кластер и добавьте для него решение, чтобы на следующем '
        'анализе рекомендации стали точнее.'
        '</div>'
        '</div>'
        f"{starter_pack_note}"
        f"{interactive_note}"
        "</div>"
        "</div>"
    )


def _render_clusters(
    clustering: "ClusteringReport | None",
    kb_results: dict[str, list["KBMatchResult"]],
    llm_result: "LLMAnalysisResult | None",
    failed_tests: "list[FailedTestSummary]",
    *,
    onboarding: OnboardingState | None = None,
    feedback_api_url: str = "",
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

    body = "".join(
        _render_cluster(
            i, cluster, kb_results, llm_result, test_by_id,
            onboarding=onboarding,
            feedback_api_url=feedback_api_url,
            cluster_id=cluster.cluster_id,
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
    onboarding: OnboardingState | None = None,
    feedback_api_url: str = "",
    cluster_id: str = "",
    launch_id: int = 0,
    project_id: int | None = None,
) -> str:
    kb_matches = kb_results.get(cluster.cluster_id, [])
    guided_mode = onboarding is not None and onboarding.mode == OnboardingMode.GUIDED

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

    # --- error example + log snippet ---
    rep_test = (
        test_by_id.get(cluster.representative_test_id)
        if cluster.representative_test_id is not None
        else None
    )
    rep_log_snippet = rep_test.log_snippet if rep_test and rep_test.log_snippet else None

    step_path_html = ""
    if cluster.example_step_path:
        step_path_html = (
            '<div class="block">'
            '<div class="block-title">Шаг теста</div>'
            f'<div class="step-path">{_e(cluster.example_step_path)}</div>'
            "</div>"
        )

    error_html = ""
    if cluster.example_message or rep_log_snippet:
        error_parts: list[str] = []
        error_parts.append('<div class="block">')
        error_parts.append('<div class="block-title">Пример ошибки</div>')

        if cluster.example_message:
            snippet = cluster.example_message[:2000]
            if len(cluster.example_message) > 2000:
                snippet += "\n…"
            error_parts.append(
                '<div class="error-block">'
                f"<pre>{_e(snippet)}</pre>"
                "</div>"
            )

        if rep_log_snippet:
            from alla.utils.log_utils import parse_log_sections

            sections = parse_log_sections(rep_log_snippet)
            per_file_limit = 1500
            inner_parts: list[str] = []
            for _log_filename, _log_body in sections:
                _snippet = _log_body[:per_file_limit]
                if len(_log_body) > per_file_limit:
                    _snippet += "\n…"
                if _log_filename:
                    inner_parts.append(
                        f'<div class="log-file-name">{_e(_log_filename)}</div>'
                    )
                inner_parts.append(f"<pre>{_e(_snippet)}</pre>")
            error_parts.append(
                '<div class="block-title" style="margin-top: 0.75rem;">Лог приложения</div>'
                '<div class="log-block">'
                + "".join(inner_parts)
                + "</div>"
            )

        error_parts.append("</div>")
        error_html = "".join(error_parts)

    project_matches, starter_pack_matches = _split_kb_matches(kb_matches)

    # Pre-compute cluster error example for edit-KB forms
    prefill: _KBPrefill | None = (
        _build_kb_prefill(cluster, llm_text, rep_log_snippet)
        if feedback_api_url
        else None
    )
    cluster_error_example = prefill["error_example"] if prefill is not None else ""

    # --- matches from knowledge base ---
    kb_html = ""
    if not guided_mode and kb_matches:
        entries_html = "".join(
            _render_kb_entry(
                m,
                feedback_api_url=feedback_api_url,
                launch_id=launch_id,
                cluster_id=cluster_id,
                cluster_step_path=cluster.example_step_path or "",
                cluster_error_example=cluster_error_example,
            )
            for m in kb_matches
        )
        kb_html = (
            '<div class="block">'
            '<div class="block-title">База знаний</div>'
            f'<div class="kb-matches">{entries_html}</div>'
            "</div>"
        )

    project_kb_html = ""
    if guided_mode:
        if project_matches:
            entries_html = "".join(
                _render_kb_entry(
                    m,
                    feedback_api_url=feedback_api_url,
                    launch_id=launch_id,
                    cluster_id=cluster_id,
                    cluster_step_path=cluster.example_step_path or "",
                    cluster_error_example=cluster_error_example,
                )
                for m in project_matches
            )
            project_kb_html = (
                '<div class="block">'
                '<div class="block-title">Знания проекта</div>'
                f'<div class="kb-matches">{entries_html}</div>'
                "</div>"
            )
        else:
            project_kb_html = (
                '<div class="block">'
                '<div class="block-title">Знания проекта</div>'
                '<div class="project-knowledge-empty">'
                'Для этого кластера ещё нет project-scoped решения. '
                'Сохраните своё описание ниже, чтобы следующий прогон уже '
                'давал проектный совет вместо общей подсказки.'
                "</div>"
                "</div>"
            )

    starter_pack_html = ""
    if starter_pack_matches and guided_mode:
        entries_html = "".join(
            _render_kb_entry(
                m,
                feedback_api_url=feedback_api_url,
                launch_id=launch_id,
                cluster_id=cluster_id,
                cluster_step_path=cluster.example_step_path or "",
                copy_payload=_build_starter_pack_payload(m, project_id),
                copy_api_url=feedback_api_url,
                cluster_error_example=cluster_error_example,
            )
            for m in starter_pack_matches
        )
        starter_pack_html = (
            '<div class="block starter-pack-block">'
            '<button class="starter-pack-toggle" '
            "onclick=\"this.nextElementSibling.classList.toggle('hidden')\">"
            'Показать starter pack'
            "</button>"
            f'<div class="starter-pack-panel hidden"><div class="kb-matches">{entries_html}</div></div>'
            "</div>"
        )

    # --- create knowledge-base entry form ---
    create_kb_html = ""
    if feedback_api_url:
        pid = _e(str(project_id)) if project_id is not None else ""
        cta_label = (
            "Создать решение для кластера"
            if guided_mode
            else "Добавить знание проекта"
        )
        toggle_cls = (
            "create-kb-toggle create-kb-toggle-primary"
            if guided_mode
            else "create-kb-toggle"
        )
        form_cls = (
            "create-kb-form create-kb-form-primary hidden"
            if guided_mode
            else "create-kb-form hidden"
        )
        resolution_control = (
            '<textarea name="resolution_steps" rows="4" '
            'placeholder="Шаг 1&#10;Шаг 2&#10;Шаг 3" autofocus></textarea>'
        )
        title_control = (
            f'<input name="title" placeholder="Описание проблемы" '
            f'value="{_e(prefill["title"] if prefill is not None else "")}">'
        )
        category_control = (
            f'<select name="category">{_render_category_options(prefill["category"] if prefill is not None else "service")}</select>'
        )
        error_example_control = (
            f'<textarea name="error_example" rows="4">{_e(prefill["error_example"] if prefill is not None else "")}</textarea>'
        )
        step_path_value = prefill["step_path"] if prefill is not None else ""
        step_path_checkbox = ""
        if step_path_value:
            step_path_checkbox = (
                '<label class="step-path-toggle">'
                f'<input type="checkbox" name="include_step_path" '
                f'data-step-path="{_e(step_path_value)}">'
                f' Добавить шаг теста: <span class="step-path-preview">{_e(step_path_value)}</span>'
                '</label>'
            )
        description_control = (
            f'<textarea name="description" rows="3" placeholder="Подробное описание">'
            f'{_e(prefill["description"] if prefill is not None else "")}</textarea>'
        )
        create_kb_html = (
            '<div class="block create-kb-action">'
            f'<button class="{toggle_cls}" '
            'onclick="this.nextElementSibling.classList.toggle(\'hidden\')">'
            f'{_e(cta_label)}</button>'
            f'<form class="{form_cls}" data-api-url="{_e(feedback_api_url)}">'
            f'{_render_form_field("Шаги по устранению", "основное поле", resolution_control, required=True)}'
            f'{_render_form_field("Заголовок", "необязательно", title_control)}'
            f'{_render_form_field("Категория", "", category_control)}'
            f'{_render_form_field("Пример ошибки", "необязательно", error_example_control)}'
            f'{step_path_checkbox}'
            f'{_render_form_field("Описание", "необязательно", description_control)}'
            f'<input type="hidden" name="project_id" value="{pid}">'
            '<button type="submit" class="create-kb-submit">Сохранить в проект</button>'
            '<span class="create-kb-status"></span>'
            '</form>'
            "</div>"
        )

    # --- test links ---
    _MAX_IDS = 60
    shown_ids = cluster.member_test_ids[:_MAX_IDS]
    links: list[str] = []
    for tid in shown_ids:
        test = test_by_id.get(tid)
        display = _e(test.name) if test and test.name else str(tid)
        if test and test.link:
            href = _e(test.link.replace("/testresult/", "/errors/"))
            links.append(f'<a href="{href}" target="_blank" class="test-id">{display}</a>')
        else:
            links.append(f'<span class="test-id no-link">{display}</span>')

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

    body_parts: list[str] = []
    if guided_mode:
        body_parts.extend(
            [step_path_html, error_html, llm_html, create_kb_html, project_kb_html, starter_pack_html, tests_html]
        )
    else:
        body_parts.extend([llm_html, step_path_html, error_html, kb_html, create_kb_html, tests_html])

    cluster_cls = "cluster guided-cluster" if guided_mode else "cluster"
    return (
        f'<div class="{cluster_cls}">'
        '<div class="cluster-header">'
        f'<span class="cluster-num">#{idx}</span>'
        f'<span class="cluster-label">{_e(cluster.label)}</span>'
        f'<span class="cluster-count">{cluster.member_count} тест(ов)</span>'
        "</div>"
        '<div class="cluster-body">'
        f'{"".join(part for part in body_parts if part)}'
        "</div>"
        "</div>"
    )


def _render_kb_entry(
    m: "KBMatchResult",
    *,
    feedback_api_url: str = "",
    launch_id: int = 0,
    cluster_id: str = "",
    cluster_step_path: str = "",
    copy_payload: dict[str, object] | None = None,
    copy_api_url: str = "",
    cluster_error_example: str = "",
) -> str:
    steps_html = ""
    if m.entry.resolution_steps:
        items = "".join(f"<li>{_linkify(_e(s))}</li>" for s in m.entry.resolution_steps)
        steps_html = f'<ul class="kb-steps">{items}</ul>'

    error_example_html = ""
    if m.entry.error_example:
        error_example_html = (
            '<button class="kb-example-toggle" '
            "onclick=\"this.nextElementSibling.classList.toggle('hidden')\">"
            "Посмотреть пример ошибки</button>"
            f'<pre class="kb-example hidden">{_e(m.entry.error_example)}</pre>'
        )

    step_path_html = ""
    if m.entry.step_path:
        step_path_html = (
            f'<div class="step-path kb-entry-step-path">{_e(m.entry.step_path)}</div>'
        )

    copy_html = ""
    if copy_payload is not None and copy_api_url:
        payload = _e(_json.dumps(copy_payload, ensure_ascii=False))
        copy_html = (
            '<div class="starter-pack-actions">'
            f'<button class="starter-pack-copy" data-api-url="{_e(copy_api_url)}" '
            f'data-payload="{payload}">Скопировать в проект</button>'
            '<span class="copy-kb-status"></span>'
            "</div>"
        )

    feedback_html = ""
    if feedback_api_url and cluster_id:
        entry_id = _e(str(m.entry.entry_id)) if m.entry.entry_id is not None else _e(m.entry.id)
        # Pre-compute CSS classes from feedback_vote.
        like_cls = "fb-btn fb-like"
        dislike_cls = "fb-btn fb-dislike"
        if m.feedback_vote == "like":
            like_cls += " fb-active"
        elif m.feedback_vote == "dislike":
            dislike_cls += " fb-active"
        feedback_html = (
            f'<div class="kb-feedback" '
            f'data-entry-id="{entry_id}" '
            f'data-launch-id="{_e(str(launch_id))}" '
            f'data-cluster-id="{_e(cluster_id)}" '
            f'data-step-aware="{"1" if m.entry.step_path else "0"}">'
            f'<button class="{like_cls}" title="Полезное совпадение">👍</button>'
            f'<button class="{dislike_cls}" title="Неверное совпадение">👎</button>'
            '<span class="fb-status"></span>'
            f'<span class="fb-id">{"fb#" + str(m.feedback_id) if m.feedback_id else ""}</span>'
            "</div>"
        )

    edit_html = ""
    if feedback_api_url and m.entry.entry_id is not None:
        eid = str(m.entry.entry_id)
        steps_text = "\n".join(m.entry.resolution_steps) if m.entry.resolution_steps else ""
        title_ctrl = f'<input name="title" value="{_e(m.entry.title)}">'
        desc_ctrl = f'<textarea name="description" rows="3">{_e(m.entry.description)}</textarea>'
        cat_ctrl = f'<select name="category">{_render_category_options(str(m.entry.category))}</select>'
        example_ctrl = f'<textarea name="error_example" rows="4">{_e(m.entry.error_example)}</textarea>'
        effective_step_path = m.entry.step_path or cluster_step_path
        step_toggle_html = ""
        if effective_step_path:
            checked_attr = ' checked="checked"' if m.entry.step_path else ""
            step_toggle_html = (
                '<label class="step-path-toggle">'
                f'<input type="checkbox" name="include_step_path" '
                f'data-step-path="{_e(effective_step_path)}"{checked_attr}>'
                f' Добавить шаг теста: <span class="step-path-preview">{_e(effective_step_path)}</span>'
                '</label>'
            )
        refresh_btn = ""
        if cluster_error_example:
            refresh_btn = (
                '<button type="button" class="edit-kb-refresh-example" '
                f'data-cluster-error="{_e(cluster_error_example)}" '
                'title="Подставить актуальный пример ошибки из текущего кластера">'
                '\u21bb Обновить из кластера</button>'
            )
        example_ctrl_with_btn = example_ctrl + refresh_btn
        steps_ctrl = f'<textarea name="resolution_steps" rows="4">{_e(steps_text)}</textarea>'
        edit_html = (
            '<div class="edit-kb-action">'
            '<button class="edit-kb-toggle" '
            "onclick=\"this.nextElementSibling.classList.toggle('hidden')\">"
            "Обновить запись в базе знаний</button>"
            f'<form class="edit-kb-form hidden" data-entry-id="{_e(eid)}">'
            f'{_render_form_field("Заголовок", "", title_ctrl)}'
            f'{_render_form_field("Описание", "необязательно", desc_ctrl)}'
            f'{_render_form_field("Категория", "", cat_ctrl)}'
            f'{_render_form_field("Пример ошибки", "необязательно", example_ctrl_with_btn)}'
            f'{step_toggle_html}'
            f'{_render_form_field("Шаги по устранению", "необязательно", steps_ctrl)}'
            '<div class="edit-kb-actions">'
            '<button type="submit" class="edit-kb-save">Сохранить</button>'
            '<button type="button" class="edit-kb-cancel">Отменить</button>'
            '<span class="edit-kb-status"></span>'
            "</div>"
            "</form>"
            "</div>"
        )

    origin_badge = (
        '<span class="kb-origin starter-pack">starter pack</span>'
        if m.entry.project_id is None
        else '<span class="kb-origin project">project</span>'
    )

    entry_id_badge = ""
    if m.entry.entry_id is not None:
        entry_id_badge = f'<span class="kb-id">#{m.entry.entry_id}</span>'

    score_html = (
        '<span class="kb-memory">Ранее подтверждено</span>'
        if m.match_origin == "feedback_exact"
        else f'<span class="kb-score">{(m.score * 100):.0f}%</span>'
    )

    return (
        '<div class="kb-entry">'
        '<div class="kb-entry-header">'
        f"{score_html}"
        f'<span class="kb-title">{_e(m.entry.title)}</span>'
        f"{entry_id_badge}"
        f"{origin_badge}"
        f'<span class="kb-category">{_e(str(m.entry.category))}</span>'
        "</div>"
        f"{steps_html}"
        f"{step_path_html}"
        f"{error_example_html}"
        f"{copy_html}"
        f"{feedback_html}"
        f"{edit_html}"
        "</div>"
    )


def _split_kb_matches(
    kb_matches: list["KBMatchResult"],
) -> tuple[list["KBMatchResult"], list["KBMatchResult"]]:
    """Разделить совпадения с базой знаний на project knowledge и global starter pack."""
    project_matches: list["KBMatchResult"] = []
    starter_pack_matches: list["KBMatchResult"] = []
    for match in kb_matches:
        if match.entry.project_id is None:
            starter_pack_matches.append(match)
        else:
            project_matches.append(match)
    return project_matches, starter_pack_matches


def _render_category_options(selected: str) -> str:
    """Собрать select options с выбранной категорией."""
    categories = ["service", "test", "env", "data"]
    parts: list[str] = []
    for category in categories:
        selected_attr = ' selected="selected"' if category == selected else ""
        parts.append(
            f'<option value="{category}"{selected_attr}>{category}</option>'
        )
    return "".join(parts)


def _render_form_field(
    label: str,
    meta: str,
    control_html: str,
    *,
    required: bool = False,
) -> str:
    """Отрендерить поле формы базы знаний как отдельный визуальный блок."""
    meta_html = ""
    if meta:
        badge_cls = "field-required" if required else "field-optional"
        meta_html = f'<span class="{badge_cls}">{_e(meta)}</span>'

    return (
        '<div class="create-kb-field">'
        '<div class="create-kb-field-head">'
        f'<span class="create-kb-field-label">{_e(label)}:</span>'
        f"{meta_html}"
        "</div>"
        f"{control_html}"
        "</div>"
    )


def _build_kb_prefill(
    cluster: "FailureCluster",
    llm_text: str | None,
    rep_log_snippet: str | None,
) -> _KBPrefill:
    """Подготовить prefill для формы project knowledge."""
    parsed_llm = _extract_llm_prefill(llm_text or "")
    prefill_parts: list[str] = []
    if cluster.example_message:
        prefill_parts.append(cluster.example_message)
    if rep_log_snippet:
        prefill_parts.append(rep_log_snippet.strip())

    error_example = ""
    if prefill_parts:
        error_example = canonicalize_kb_error_example("\n".join(prefill_parts))

    return {
        "title": cluster.label or parsed_llm["title"],
        "category": parsed_llm["category"] or "service",
        "error_example": error_example,
        "step_path": cluster.example_step_path or "",
        "description": parsed_llm["description"],
        "resolution_steps": parsed_llm["resolution_steps"],
    }


def _extract_llm_prefill(analysis_text: str) -> _KBPrefill:
    """Вытащить title/description/category/steps из LLM-анализа."""
    what_broke = ""
    category = ""
    steps: list[str] = []
    in_steps = False

    for raw_line in analysis_text.splitlines():
        line = raw_line.strip()
        if not line:
            if in_steps:
                continue
            continue

        upper = line.upper()
        if upper.startswith("ЧТО СЛОМАЛОСЬ:"):
            what_broke = line.split(":", 1)[1].strip()
            in_steps = False
            continue
        if upper.startswith("ПРИЧИНА:"):
            category = _map_prefill_category(line.split(":", 1)[1].strip())
            in_steps = False
            continue
        if upper.startswith("КАК ИСПРАВИТЬ:"):
            in_steps = True
            remainder = line.split(":", 1)[1].strip()
            if remainder:
                steps.append(remainder)
            continue
        if in_steps:
            match = re.match(r"^\d+[.)]\s*(.*)$", line)
            if match:
                steps.append(match.group(1).strip())
            elif steps:
                steps[-1] = f"{steps[-1]} {line}".strip()

    title = what_broke.split(".")[0].strip() if what_broke else ""
    return {
        "title": title,
        "error_example": "",
        "step_path": "",
        "description": what_broke,
        "category": category,
        "resolution_steps": steps[:3],
    }


def _map_prefill_category(raw_value: str) -> str:
    """Преобразовать русскую категорию из LLM-ответа в enum value."""
    normalized = raw_value.lower().split("—", 1)[0].split("-", 1)[0].strip()
    mapping = {
        "тест": "test",
        "приложение": "service",
        "окружение": "env",
        "данные": "data",
    }
    return mapping.get(normalized, "")


def _build_starter_pack_payload(
    match: "KBMatchResult",
    project_id: int | None,
) -> dict[str, object] | None:
    """Собрать payload для копирования starter pack записи в проект."""
    if project_id is None or match.entry.project_id is not None:
        return None
    return {
        "id": match.entry.id,
        "title": match.entry.title,
        "description": match.entry.description,
        "error_example": match.entry.error_example,
        "step_path": match.entry.step_path,
        "category": str(match.entry.category),
        "resolution_steps": list(match.entry.resolution_steps),
        "project_id": project_id,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _e(s: object) -> str:
    """HTML-escape строку."""
    return _html.escape(str(s))


def _linkify(text: str) -> str:
    """Turn plain-text URLs into clickable <a> links (text must be HTML-escaped already)."""

    def _replace(m: re.Match[str]) -> str:
        url = m.group(1)
        # Strip trailing sentence punctuation that's unlikely to be part of the URL
        stripped = url.rstrip(".,)!?:;")
        # Restore ';' if stripping broke an HTML entity (e.g., &amp; → &amp)
        if re.search(r"&\w+$", stripped) and url[len(stripped):].startswith(";"):
            stripped += ";"
        trail = url[len(stripped):]
        return f'<a href="{stripped}" target="_blank" rel="noopener">{stripped}</a>{trail}'

    return re.sub(r"(https?://(?:[^\s<>&\"']|&amp;)+)", _replace, text)


def _inline_md(text: str) -> str:
    """HTML-escape text then apply inline Markdown (bold, italic, code, links)."""
    text = _html.escape(text)
    # Bold **text**
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", text)
    # Italic *text* (not preceded/followed by another *)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    # Inline code `code`
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    # Auto-linkify URLs
    text = _linkify(text)
    return text


def _markdown_to_html(text: str) -> str:
    """Convert a Markdown subset to HTML (pure Python, no external dependencies).

    Handles: ATX headings, fenced code blocks, unordered/ordered lists,
    bold, italic, inline code, and paragraphs.
    """
    lines = text.splitlines()
    parts: list[str] = []
    para: list[str] = []
    in_code = False
    code_buf: list[str] = []
    code_open_tag = ""
    in_ul = False
    in_ol = False

    def flush_para() -> None:
        if not para:
            return
        inner = _inline_md(" ".join(para))
        parts.append(f"<p>{inner}</p>")
        para.clear()

    def close_list() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            parts.append("</ul>")
            in_ul = False
        elif in_ol:
            parts.append("</ol>")
            in_ol = False

    for line in lines:
        # Fenced code block toggle
        if line.startswith("```"):
            if in_code:
                parts.append(code_open_tag + _html.escape("\n".join(code_buf)) + "</code></pre>")
                code_buf.clear()
                code_open_tag = ""
                in_code = False
            else:
                flush_para()
                close_list()
                lang = _html.escape(line[3:].strip())
                attr = f' class="language-{lang}"' if lang else ""
                code_open_tag = f"<pre><code{attr}>"
                in_code = True
            continue

        if in_code:
            code_buf.append(line)
            continue

        # ATX headings
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            parts.append(f"<h{level}>{_inline_md(m.group(2))}</h{level}>")
            continue

        # Unordered list item
        m = re.match(r"^[-*+]\s+(.*)", line)
        if m:
            flush_para()
            if in_ol:
                close_list()
            if not in_ul:
                parts.append("<ul>")
                in_ul = True
            parts.append(f"<li>{_inline_md(m.group(1))}</li>")
            continue

        # Ordered list item
        m = re.match(r"^\d+[.)]\s+(.*)", line)
        if m:
            flush_para()
            if in_ul:
                close_list()
            if not in_ol:
                parts.append("<ol>")
                in_ol = True
            parts.append(f"<li>{_inline_md(m.group(1))}</li>")
            continue

        # Blank line — end paragraph and list
        if not line.strip():
            flush_para()
            close_list()
            continue

        # Regular text — close any open list and accumulate into paragraph
        close_list()
        para.append(line)

    flush_para()
    close_list()
    if in_code:
        parts.append(code_open_tag + _html.escape("\n".join(code_buf)) + "</code></pre>")

    return "\n".join(parts)


def _render_llm_text(text: str) -> str:
    """Render LLM Markdown to HTML server-side (no external dependencies)."""
    return f'<div class="markdown-rendered">{_markdown_to_html(text)}</div>'


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

    /* ---- Onboarding ---- */
    .onboarding-banner {
      background: linear-gradient(135deg, #fff3d6 0%, #ffd7b8 42%, #fff8ef 100%);
      border: 2px solid #f59e0b;
      border-radius: var(--radius);
      padding: 1.75rem 2rem;
      box-shadow: 0 14px 32px rgba(245, 158, 11, 0.18);
      display: flex;
      flex-direction: column;
      gap: 0.85rem;
    }
    .onboarding-banner.hidden { display: none; }
    .onboarding-banner.setup {
      background: linear-gradient(135deg, #fff7ed 0%, #ffffff 100%);
      border-color: #fdba74;
      box-shadow: 0 8px 24px rgba(249, 115, 22, 0.12);
    }
    .onboarding-kicker {
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--warning);
    }
    .onboarding-title {
      font-size: 1.55rem;
      font-weight: 700;
      line-height: 1.2;
      color: #7c2d12;
    }
    .onboarding-copy {
      font-size: 1rem;
      color: #9a3412;
      max-width: 76rem;
      line-height: 1.65;
    }
    .onboarding-next-step {
      background: rgba(255, 251, 235, 0.92);
      border: 1px solid rgba(217, 119, 6, 0.35);
      border-left: 6px solid #d97706;
      border-radius: var(--radius-sm);
      padding: 0.9rem 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      max-width: 64rem;
    }
    .onboarding-next-step-label {
      font-size: 0.9rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: #9a3412;
    }
    .onboarding-next-step-text {
      font-size: 0.98rem;
      line-height: 1.55;
      color: #7c2d12;
    }
    .onboarding-note {
      font-size: 0.825rem;
      color: var(--text-muted);
      background: rgba(255,255,255,0.8);
      border: 1px dashed var(--border);
      border-radius: var(--radius-sm);
      padding: 0.65rem 0.9rem;
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
    .guided-cluster {
      border-color: #fdba74;
      box-shadow: 0 10px 24px rgba(249, 115, 22, 0.08);
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

    /* ---- Step Path ---- */
    .step-path {
      font-family: var(--font-mono, monospace);
      font-size: 0.85rem;
      color: var(--text-muted);
      padding: 0.4rem 0.6rem;
      background: var(--surface);
      border-radius: 6px;
    }
    .step-path-toggle {
      display: flex;
      align-items: baseline;
      gap: 0.3rem;
      font-size: 0.8rem;
      color: var(--text-muted);
      cursor: pointer;
      margin: 0.3rem 0 0.5rem;
    }
    .step-path-toggle input[type="checkbox"] { margin: 0; }
    .step-path-preview {
      font-family: var(--font-mono, monospace);
      font-size: 0.78rem;
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

    /* ---- Log Block ---- */
    .log-block {
      background: #f5f3ff;
      border: 1px solid #c4b5fd;
      border-radius: var(--radius-sm);
      padding: 1rem;
    }
    .log-block pre {
      margin: 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.8125rem;
      color: #5b21b6;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 400px;
      overflow-y: auto;
    }
    .log-file-name {
      font-size: 0.7rem;
      font-weight: 600;
      color: #7c3aed;
      margin-top: 0.75rem;
      margin-bottom: 0.15rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    .log-file-name:first-child { margin-top: 0; }

    /* ---- LLM Analysis ---- */
    .llm-analysis {
      background: var(--info-light);
      border: 1px solid #bae6fd;
      border-radius: var(--radius-sm);
      padding: 1.25rem 1.5rem;
    }

    /* ---- Knowledge Base Matches ---- */
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
    .kb-memory {
      background: #eff6ff;
      color: #1d4ed8;
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
    .kb-id {
      font-size: .7rem;
      color: var(--text-muted);
      font-weight: 400;
    }
    .kb-origin {
      font-size: 0.7rem;
      font-weight: 700;
      text-transform: uppercase;
      border-radius: 9999px;
      padding: 0.125rem 0.5rem;
    }
    .kb-origin.project {
      background: var(--primary-light);
      color: var(--primary);
    }
    .kb-origin.starter-pack {
      background: #fff7ed;
      color: var(--warning);
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
    .kb-example-toggle{background:none;border:1px dashed var(--border);border-radius:var(--radius-sm);padding:.4rem .75rem;cursor:pointer;color:var(--text-muted);font-size:.8rem;margin-top:.75rem;transition:all .2s}
    .kb-example-toggle:hover{border-color:var(--primary);color:var(--primary)}
    .kb-example{margin:.5rem 0 0;padding:.75rem 1rem;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:.75rem;line-height:1.5;white-space:pre-wrap;word-break:break-word;max-height:20rem;overflow-y:auto}
    .kb-example.hidden{display:none}
    .project-knowledge-empty {
      background: #f8fafc;
      border: 1px dashed var(--border);
      border-radius: var(--radius-sm);
      padding: 1rem 1.25rem;
      color: var(--text-muted);
    }
    .starter-pack-block {
      border-top: 1px solid var(--border);
      padding-top: 0.25rem;
    }
    .starter-pack-toggle {
      width: 100%;
      text-align: left;
      background: none;
      border: 1px dashed var(--border);
      border-radius: var(--radius-sm);
      padding: 0.75rem 1rem;
      cursor: pointer;
      color: var(--text-muted);
      font-weight: 600;
      transition: all 0.2s;
    }
    .starter-pack-toggle:hover {
      border-color: var(--warning);
      color: var(--warning);
    }
    .starter-pack-panel {
      margin-top: 0.75rem;
    }
    .starter-pack-panel.hidden {
      display: none;
    }

    /* ---- Test IDs ---- */
    .test-list {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .test-id {
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text);
      font-size: 0.8125rem;
      padding: 0.3rem 0.5rem;
      border-radius: 6px;
      text-decoration: none;
      transition: all 0.2s;
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
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
      .onboarding-banner { padding: 1.35rem 1.15rem; }
      .onboarding-title { font-size: 1.35rem; }
      .create-kb-toggle-primary { padding: .95rem 1rem; font-size: .95rem; }
      .cluster-header { flex-direction: column; align-items: flex-start; gap: 0.5rem; }
      .cluster-label { min-width: 100%; }
    }
"""

# ---------------------------------------------------------------------------
# Feedback CSS (appended only when feedback_api_url is provided)
# ---------------------------------------------------------------------------

_FEEDBACK_CSS = """
    /* ---- Knowledge Base Feedback Buttons ---- */
    .kb-feedback{display:flex;align-items:center;gap:.5rem;margin-top:.75rem;padding-top:.75rem;border-top:1px solid var(--border)}
    .fb-btn{display:inline-flex;align-items:center;gap:.25rem;padding:.375rem .75rem;border:1px solid var(--border);border-radius:6px;background:var(--surface);cursor:pointer;font-size:.8125rem;color:var(--text-muted);transition:all .2s}
    .fb-btn:hover{border-color:var(--primary);color:var(--primary)}
    .fb-btn.fb-active.fb-like{border-color:var(--success);color:var(--success);background:#f0fdf4}
    .fb-btn.fb-active.fb-dislike{border-color:var(--danger);color:var(--danger);background:#fef2f2}
    .fb-status{font-size:.75rem;color:var(--text-muted)}
    .fb-status-ok{color:var(--success)}
    .fb-status-error{color:var(--danger)}
    .fb-id{font-size:.65rem;color:var(--text-muted);font-variant-numeric:tabular-nums}

    /* ---- Create Knowledge Base Entry Form ---- */
    .create-kb-action{gap:.8rem}
    .create-kb-toggle{background:none;border:1px dashed var(--border);border-radius:var(--radius-sm);padding:.5rem 1rem;cursor:pointer;color:var(--text-muted);font-size:.875rem;width:100%;text-align:left;transition:all .2s}
    .create-kb-toggle:hover{border-color:var(--primary);color:var(--primary)}
    .create-kb-toggle-primary{background:linear-gradient(135deg,#f97316 0%,#ea580c 100%);border:none;color:#fff;font-size:1rem;font-weight:700;padding:1rem 1.25rem;border-radius:14px;text-align:center;box-shadow:0 14px 30px rgba(234,88,12,.28)}
    .create-kb-toggle-primary:hover{color:#fff;opacity:.96;transform:translateY(-1px)}
    .create-kb-form{display:flex;flex-direction:column;gap:.9rem;padding:1rem;border:1px solid var(--border);border-radius:var(--radius-sm);margin-top:.25rem;background:#fff}
    .create-kb-form.hidden{display:none}
    .create-kb-form-primary{border:1px solid #fdba74;background:linear-gradient(180deg,#fffaf5 0%,#ffffff 100%);box-shadow:0 10px 24px rgba(249,115,22,.08)}
    .create-kb-field{display:flex;flex-direction:column;gap:.55rem;padding:.9rem 1rem;border:1px solid #e5e7eb;border-radius:12px;background:#f8fafc}
    .create-kb-field-head{display:flex;align-items:center;gap:.65rem;flex-wrap:wrap}
    .create-kb-field-label{font-size:.875rem;font-weight:700;color:#0f172a}
    .create-kb-field input,.create-kb-field textarea,.create-kb-field select{width:100%;font-family:inherit;font-size:.9rem;padding:.75rem .8rem;border:1px solid #cbd5e1;border-radius:10px;background:#fff;color:var(--text)}
    .create-kb-field textarea{resize:vertical;min-height:3.25rem}
    .create-kb-submit{align-self:flex-start;padding:.7rem 1.5rem;background:var(--primary);color:white;border:none;border-radius:10px;cursor:pointer;font-weight:700;font-size:.92rem}
    .create-kb-submit:hover{opacity:.9}
    .create-kb-submit:disabled{opacity:.5;cursor:not-allowed}
    .create-kb-ok{color:var(--success);font-size:.875rem}
    .create-kb-error{color:var(--danger);font-size:.875rem}
    .field-required,.field-optional{display:inline-flex;align-items:center;height:1.45rem;padding:0 .55rem;border-radius:9999px;font-size:.72rem;line-height:1;font-weight:700;white-space:nowrap}
    .field-required{background:#dbeafe;color:var(--primary)}
    .field-optional{background:#e2e8f0;color:var(--text-muted)}
    .starter-pack-actions{display:flex;align-items:center;gap:.75rem;margin-top:.75rem}
    .starter-pack-copy{padding:.45rem .95rem;background:var(--warning-light);color:var(--warning);border:1px solid #fed7aa;border-radius:6px;cursor:pointer;font-weight:600;font-size:.8125rem}
    .starter-pack-copy:hover{border-color:#fb923c}
    .starter-pack-copy:disabled{opacity:.55;cursor:not-allowed}
    .copy-kb-status{font-size:.75rem;color:var(--text-muted)}
    .copy-kb-ok{color:var(--success)}
    .copy-kb-error{color:var(--danger)}

    /* ---- Edit Knowledge Base Entry ---- */
    .edit-kb-action{margin-top:.75rem}
    .edit-kb-toggle{background:none;border:1px dashed var(--border);border-radius:var(--radius-sm);padding:.5rem 1rem;cursor:pointer;color:var(--text-muted);font-size:.8125rem;width:100%;text-align:left;transition:all .2s}
    .edit-kb-toggle:hover{border-color:var(--primary);color:var(--primary)}
    .edit-kb-form{display:flex;flex-direction:column;gap:.9rem;padding:1rem;border:1px solid var(--border);border-radius:var(--radius-sm);margin-top:.25rem;background:#fff}
    .edit-kb-form.hidden{display:none}
    .edit-kb-actions{display:flex;align-items:center;gap:.75rem}
    .edit-kb-save{padding:.7rem 1.5rem;background:var(--primary);color:white;border:none;border-radius:10px;cursor:pointer;font-weight:700;font-size:.92rem}
    .edit-kb-save:hover{opacity:.9}
    .edit-kb-save:disabled{opacity:.5;cursor:not-allowed}
    .edit-kb-cancel{padding:.7rem 1.5rem;background:var(--surface);color:var(--text-muted);border:1px solid var(--border);border-radius:10px;cursor:pointer;font-weight:600;font-size:.92rem}
    .edit-kb-cancel:hover{border-color:var(--text-muted);color:var(--text)}
    .edit-kb-status{font-size:.75rem;color:var(--text-muted)}
    .edit-kb-ok{color:var(--success)}
    .edit-kb-error{color:var(--danger)}
    .edit-kb-refresh-example{background:none;border:1px dashed var(--border);border-radius:var(--radius-sm);padding:.35rem .75rem;cursor:pointer;color:var(--primary);font-size:.75rem;margin-top:.25rem;transition:all .2s;display:inline-block}
    .edit-kb-refresh-example:hover{border-color:var(--primary);background:var(--surface)}
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
        "  function markOnboardingComplete() {\n"
        "    document.querySelectorAll('[data-onboarding-banner]').forEach(function(el) {\n"
        "      el.classList.add('hidden');\n"
        "    });\n"
        "  }\n"
        "\n"
        "  function postKbEntry(apiUrl, payload) {\n"
        "    return fetch(apiUrl + '/api/v1/kb/entries', {\n"
        "      method: 'POST',\n"
        "      headers: {'Content-Type': 'application/json'},\n"
        "      body: JSON.stringify(payload)\n"
        "    }).then(function(r) {\n"
        "      if (!r.ok) throw new Error(r.status);\n"
        "      return r.json();\n"
        "    });\n"
        "  }\n"
        "\n"
        "  function getFeedbackSignature(context, wrap) {\n"
        "    if (!context) return null;\n"
        "    var stepAware = wrap && wrap.dataset.stepAware === '1';\n"
        "    if (stepAware && context.step_issue_signature) return context.step_issue_signature;\n"
        "    return context.base_issue_signature || context.issue_signature || null;\n"
        "  }\n"
        "\n"
        "  function syncKbEntrySteps(entry, steps) {\n"
        "    if (!entry) return;\n"
        "    var stepsEl = entry.querySelector('.kb-steps');\n"
        "    if (!steps || !steps.length) {\n"
        "      if (stepsEl) stepsEl.remove();\n"
        "      return;\n"
        "    }\n"
        "    if (!stepsEl) {\n"
        "      stepsEl = document.createElement('ul');\n"
        "      stepsEl.className = 'kb-steps';\n"
        "      var stepsAnchor = entry.querySelector('.kb-entry-step-path, .kb-example-toggle, .starter-pack-actions, .kb-feedback, .edit-kb-action');\n"
        "      if (stepsAnchor) entry.insertBefore(stepsEl, stepsAnchor);\n"
        "      else entry.appendChild(stepsEl);\n"
        "    }\n"
        "    stepsEl.textContent = '';\n"
        "    steps.forEach(function(step) {\n"
        "      var li = document.createElement('li');\n"
        "      li.textContent = step;\n"
        "      stepsEl.appendChild(li);\n"
        "    });\n"
        "  }\n"
        "\n"
        "  function syncKbEntryStepState(entry, stepPath) {\n"
        "    if (!entry) return;\n"
        "    var feedbackWrap = entry.querySelector('.kb-feedback');\n"
        "    if (feedbackWrap) feedbackWrap.dataset.stepAware = stepPath ? '1' : '0';\n"
        "    var stepPathEl = entry.querySelector('.kb-entry-step-path');\n"
        "    if (!stepPath) {\n"
        "      if (stepPathEl) stepPathEl.remove();\n"
        "      return;\n"
        "    }\n"
        "    if (!stepPathEl) {\n"
        "      stepPathEl = document.createElement('div');\n"
        "      stepPathEl.className = 'step-path kb-entry-step-path';\n"
        "      var stepAnchor = entry.querySelector('.kb-example-toggle, .starter-pack-actions, .kb-feedback, .edit-kb-action');\n"
        "      if (stepAnchor) entry.insertBefore(stepPathEl, stepAnchor);\n"
        "      else entry.appendChild(stepPathEl);\n"
        "    }\n"
        "    stepPathEl.textContent = stepPath;\n"
        "  }\n"
        "\n"
        "  // --- Like / Dislike ---\n"
        "  function sendFeedback(el, isLike) {\n"
        "    var wrap = el.closest('.kb-feedback');\n"
        "    var status = wrap.querySelector('.fb-status');\n"
        "    var clusterId = wrap.dataset.clusterId;\n"
        "    var context = (typeof CLUSTER_FEEDBACK_CONTEXTS !== 'undefined')\n"
        "      ? CLUSTER_FEEDBACK_CONTEXTS[clusterId] : null;\n"
        "    var signature = getFeedbackSignature(context, wrap);\n"
        "    if (!context || !context.audit_text || !signature) {\n"
        "      status.textContent = 'No feedback context';\n"
        "      status.className = 'fb-status fb-status-error';\n"
        "      return;\n"
        "    }\n"
        "    var body = JSON.stringify({\n"
        "      kb_entry_id: parseInt(wrap.dataset.entryId, 10),\n"
        "      audit_text: context.audit_text,\n"
        "      issue_signature_hash: signature.signature_hash,\n"
        "      issue_signature_version: signature.version,\n"
        "      issue_signature_payload: signature,\n"
        "      launch_id: parseInt(wrap.dataset.launchId, 10) || null,\n"
        "      cluster_id: clusterId || null,\n"
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
        "    }).then(function(data) {\n"
        "      wrap.querySelectorAll('.fb-btn').forEach(function(b) { b.classList.remove('fb-active'); });\n"
        "      el.classList.add('fb-active');\n"
        "      status.textContent = 'Saved';\n"
        "      status.className = 'fb-status fb-status-ok';\n"
        "      var fbIdEl = wrap.querySelector('.fb-id');\n"
        "      if (fbIdEl && data.feedback_id) fbIdEl.textContent = 'fb#' + data.feedback_id;\n"
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
        "    btn = e.target.closest('.starter-pack-copy');\n"
        "    if (btn) {\n"
        "      var status = btn.parentElement.querySelector('.copy-kb-status');\n"
        "      var apiUrl = btn.dataset.apiUrl || FEEDBACK_API_URL;\n"
        "      var payload = JSON.parse(btn.dataset.payload || '{}');\n"
        "      btn.disabled = true;\n"
        "      status.textContent = '...';\n"
        "      status.className = 'copy-kb-status';\n"
        "      postKbEntry(apiUrl, payload).then(function() {\n"
        "        status.textContent = 'Скопировано в проект';\n"
        "        status.className = 'copy-kb-status copy-kb-ok';\n"
        "        markOnboardingComplete();\n"
        "      }).catch(function(err) {\n"
        "        status.textContent = 'Error: ' + err.message;\n"
        "        status.className = 'copy-kb-status copy-kb-error';\n"
        "        btn.disabled = false;\n"
        "      });\n"
        "      return;\n"
        "    }\n"
        "  });\n"
        "\n"
        "  // --- Create Knowledge Base Entry ---\n"
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
        "    var titleVal = form.elements.title.value.trim() || null;\n"
        "    var errorExample = form.elements.error_example.value;\n"
        "    var stepPathCb = form.querySelector('input[name=\"include_step_path\"]');\n"
        "    var stepPath = (stepPathCb && stepPathCb.checked && stepPathCb.dataset.stepPath) ? stepPathCb.dataset.stepPath : null;\n"
        "    var payload = {\n"
        "      title: titleVal,\n"
        "      category: form.elements.category.value,\n"
        "      error_example: errorExample,\n"
        "      step_path: stepPath,\n"
        "      description: form.elements.description.value,\n"
        "      resolution_steps: steps,\n"
        "      project_id: parseInt(form.elements.project_id.value, 10) || null\n"
        "    };\n"
        "    var apiUrl = form.dataset.apiUrl || FEEDBACK_API_URL;\n"
        "    postKbEntry(apiUrl, payload).then(function() {\n"
        "      status.textContent = 'Создано!';\n"
        "      status.className = 'create-kb-status create-kb-ok';\n"
        "      submitBtn.disabled = true;\n"
        "      markOnboardingComplete();\n"
        "    }).catch(function(err) {\n"
        "      status.textContent = 'Error: ' + err.message;\n"
        "      status.className = 'create-kb-status create-kb-error';\n"
        "      submitBtn.disabled = false;\n"
        "    });\n"
        "  });\n"
        "\n"
        "  // --- Edit Knowledge Base Entry ---\n"
        "  document.addEventListener('submit', function(e) {\n"
        "    var form = e.target.closest('.edit-kb-form');\n"
        "    if (!form) return;\n"
        "    e.preventDefault();\n"
        "    var saveBtn = form.querySelector('.edit-kb-save');\n"
        "    var status = form.querySelector('.edit-kb-status');\n"
        "    var entryId = form.dataset.entryId;\n"
        "    saveBtn.disabled = true;\n"
        "    status.textContent = '...';\n"
        "    status.className = 'edit-kb-status';\n"
        "    var steps = (form.elements.resolution_steps.value || '').split('\\n').filter(function(s) { return s.trim(); });\n"
        "    var stepPathCb = form.querySelector('input[name=\"include_step_path\"]');\n"
        "    var stepPath = (stepPathCb && stepPathCb.checked && stepPathCb.dataset.stepPath) ? stepPathCb.dataset.stepPath : null;\n"
        "    var payload = {\n"
        "      title: form.elements.title.value,\n"
        "      description: form.elements.description.value,\n"
        "      category: form.elements.category.value,\n"
        "      error_example: form.elements.error_example.value,\n"
        "      step_path: stepPath,\n"
        "      resolution_steps: steps\n"
        "    };\n"
        "    fetch(FEEDBACK_API_URL + '/api/v1/kb/entries/' + entryId, {\n"
        "      method: 'PUT',\n"
        "      headers: {'Content-Type': 'application/json'},\n"
        "      body: JSON.stringify(payload)\n"
        "    }).then(function(r) {\n"
        "      if (!r.ok) throw new Error(r.status);\n"
        "      return r.json();\n"
        "    }).then(function() {\n"
        "      status.textContent = 'Сохранено!';\n"
        "      status.className = 'edit-kb-status edit-kb-ok';\n"
        "      saveBtn.disabled = false;\n"
        "      // Update the displayed KB entry content\n"
        "      var entry = form.closest('.kb-entry');\n"
        "      if (entry) {\n"
        "        var titleEl = entry.querySelector('.kb-title');\n"
        "        if (titleEl) titleEl.textContent = payload.title;\n"
        "        var catEl = entry.querySelector('.kb-category');\n"
        "        if (catEl) catEl.textContent = payload.category;\n"
        "        syncKbEntrySteps(entry, steps);\n"
        "        syncKbEntryStepState(entry, payload.step_path);\n"
        "        if (stepPathCb) stepPathCb.checked = !!payload.step_path;\n"
        "      }\n"
        "    }).catch(function(err) {\n"
        "      status.textContent = 'Ошибка: ' + err.message;\n"
        "      status.className = 'edit-kb-status edit-kb-error';\n"
        "      saveBtn.disabled = false;\n"
        "    });\n"
        "  });\n"
        "\n"
        "  // --- Refresh error_example from cluster ---\n"
        "  document.addEventListener('click', function(e) {\n"
        "    var btn = e.target.closest('.edit-kb-refresh-example');\n"
        "    if (!btn) return;\n"
        "    var form = btn.closest('.edit-kb-form');\n"
        "    if (!form) return;\n"
        "    var textarea = form.querySelector('textarea[name=\"error_example\"]');\n"
        "    if (textarea) {\n"
        "      textarea.value = btn.dataset.clusterError;\n"
        "    }\n"
        "  });\n"
        "\n"
        "  // --- Cancel Edit Knowledge Base Entry ---\n"
        "  document.addEventListener('click', function(e) {\n"
        "    var cancelBtn = e.target.closest('.edit-kb-cancel');\n"
        "    if (!cancelBtn) return;\n"
        "    var form = cancelBtn.closest('.edit-kb-form');\n"
        "    if (form) {\n"
        "      form.reset();\n"
        "      form.classList.add('hidden');\n"
        "      var status = form.querySelector('.edit-kb-status');\n"
        "      if (status) { status.textContent = ''; status.className = 'edit-kb-status'; }\n"
        "    }\n"
        "  });\n"
        "\n"
        "  // --- Load existing votes on page load (exact resolve) ---\n"
        "  document.addEventListener('DOMContentLoaded', function() {\n"
        "    if (typeof CLUSTER_FEEDBACK_CONTEXTS === 'undefined') return;\n"
        "    var items = [];\n"
        "    var seen = {};\n"
        "    document.querySelectorAll('.kb-feedback').forEach(function(el) {\n"
        "      var entryId = parseInt(el.dataset.entryId, 10);\n"
        "      var clusterId = el.dataset.clusterId;\n"
        "      var context = CLUSTER_FEEDBACK_CONTEXTS[clusterId];\n"
        "      var signature = getFeedbackSignature(context, el);\n"
        "      if (!signature) return;\n"
        "      var key = entryId + ':' + clusterId;\n"
        "      if (seen[key]) return;\n"
        "      seen[key] = true;\n"
        "      items.push({\n"
        "        kb_entry_id: entryId,\n"
        "        issue_signature_hash: signature.signature_hash,\n"
        "        issue_signature_version: signature.version,\n"
        "        cluster_id: clusterId || ''\n"
        "      });\n"
        "    });\n"
        "    if (items.length === 0) return;\n"
        "    fetch(FEEDBACK_API_URL + '/api/v1/kb/feedback/resolve', {\n"
        "      method: 'POST',\n"
        "      headers: {'Content-Type': 'application/json'},\n"
        "      body: JSON.stringify({items: items})\n"
        "    })\n"
        "    .then(function(r) { return r.ok ? r.json() : null; })\n"
        "    .then(function(data) {\n"
        "      if (!data || !data.votes) return;\n"
        "      document.querySelectorAll('.kb-feedback').forEach(function(wrap) {\n"
        "        var eid = wrap.dataset.entryId;\n"
        "        var cid = wrap.dataset.clusterId || '';\n"
        "        var key = eid + ':' + cid;\n"
        "        var info = data.votes[key];\n"
        "        if (!info) return;\n"
        "        wrap.querySelectorAll('.fb-btn').forEach(function(b) { b.classList.remove('fb-active'); });\n"
        "        var cls = info.vote === 'like' ? '.fb-like' : '.fb-dislike';\n"
        "        var btn = wrap.querySelector(cls);\n"
        "        if (btn) btn.classList.add('fb-active');\n"
        "        var fbIdEl = wrap.querySelector('.fb-id');\n"
        "        if (fbIdEl && info.feedback_id) fbIdEl.textContent = 'fb#' + info.feedback_id;\n"
        "      });\n"
        "    })\n"
        "    .catch(function() {});\n"
        "  });\n"
        "})();\n"
        "</script>"
    )
