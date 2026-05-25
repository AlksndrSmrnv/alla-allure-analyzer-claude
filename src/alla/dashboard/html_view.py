"""Самодостаточная HTML-страница дашборда использования.

Серверный шаблон отдаёт пустую оболочку. Данные подгружаются на клиенте
через ``fetch('/api/v1/dashboard/stats?days=N')`` или
``?date=YYYY-MM-DD`` и рендерятся ванильным JS. Ни внешних CSS,
ни сторонних скриптов — только inline.
"""

from __future__ import annotations

from functools import lru_cache

from alla.report.html_report import _logo_data_uri

_DASHBOARD_CSS = """
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
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  font-size: 14px;
  -webkit-font-smoothing: antialiased;
}
.container { max-width: 1200px; margin: 0 auto; padding: 2rem 1.5rem; }
.header {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.25rem 1.5rem;
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1.5rem;
  flex-wrap: wrap;
}
.header-logo { height: 40px; width: auto; flex-shrink: 0; }
.header-title { font-size: 1.25rem; font-weight: 600; flex: 1; }
.header-controls { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.header-controls label { color: var(--text-muted); font-size: 0.85rem; }
.header-controls select,
.header-controls input[type="date"],
.header-controls button {
  padding: 0.4rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text);
  font-size: 0.9rem;
  cursor: pointer;
  font-family: inherit;
}
.header-controls select:disabled,
.header-controls input:disabled { opacity: 0.5; cursor: not-allowed; }
.header-controls button.ghost { color: var(--text-muted); }
.header-controls button.ghost:hover { color: var(--text); border-color: var(--text-muted); }
.window-label {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.6rem 1rem;
  margin-bottom: 1rem;
  color: var(--text-muted);
  font-size: 0.85rem;
}
.section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.5rem;
  margin-bottom: 1.25rem;
}
.section h2 {
  font-size: 1rem;
  font-weight: 600;
  margin-bottom: 1rem;
  color: var(--text);
}
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem;
}
.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.25rem;
}
.kpi-card .label { color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em; }
.kpi-card .value { font-size: 1.85rem; font-weight: 700; margin-top: 0.5rem; color: var(--text); }
.kpi-card .sub   { font-size: 0.8rem; color: var(--text-muted); margin-top: 0.25rem; }
.kpi-card.reports .value { color: var(--primary); }
.kpi-card.tokens  .value { color: var(--info); }
.kpi-card.duration .value { color: var(--warning); }
.kpis-compact {
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.6rem;
}
.kpis-compact .kpi-card { padding: 0.75rem 0.9rem; }
.kpis-compact .kpi-card .label { font-size: 0.7rem; }
.kpis-compact .kpi-card .value { font-size: 1.15rem; margin-top: 0.25rem; }
.kpis-compact .kpi-card .sub   { font-size: 0.72rem; }
.section.tokens-section h2 { font-size: 0.85rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }
.bars {
  display: flex;
  gap: 2px;
  align-items: flex-end;
  height: 180px;
  padding: 0.5rem 0;
  overflow-x: auto;
}
.bars .bar {
  flex: 1;
  min-width: 4px;
  background: var(--primary-light);
  border-top: 2px solid var(--primary);
  border-radius: 2px 2px 0 0;
  position: relative;
}
.bars .bar:hover { background: var(--primary); }
.bars .bar[data-n="0"] { background: var(--border); border-top: 0; min-height: 1px; }
.bars-axis {
  display: flex;
  justify-content: space-between;
  color: var(--text-muted);
  font-size: 0.75rem;
  margin-top: 0.25rem;
}
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
thead th {
  background: var(--bg);
  text-align: left;
  padding: 0.65rem 0.8rem;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  font-weight: 600;
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  cursor: pointer;
  user-select: none;
}
thead th.sorted-asc::after  { content: " \\2191"; color: var(--primary); }
thead th.sorted-desc::after { content: " \\2193"; color: var(--primary); }
tbody td {
  padding: 0.7rem 0.8rem;
  border-bottom: 1px solid var(--border);
}
tbody tr.project-row { cursor: pointer; }
tbody tr.project-row:hover { background: var(--bg); }
tbody tr.unattributed { color: var(--text-muted); font-style: italic; }
tbody tr.expand-row td { background: var(--bg); padding: 0; }
tbody tr.expand-row .expand-inner { padding: 0.75rem 1rem; }
.expand-inner table { font-size: 0.85rem; }
.expand-inner thead th { background: transparent; cursor: default; }
.expand-inner .empty { color: var(--text-muted); padding: 0.5rem 0; }
.disclosure { color: var(--text-muted); margin-right: 0.4rem; display: inline-block; transition: transform 0.15s; }
tr.project-row.expanded .disclosure { transform: rotate(90deg); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.muted { color: var(--text-muted); }
.banner {
  background: var(--danger-light);
  border: 1px solid var(--danger);
  color: var(--danger);
  border-radius: var(--radius-sm);
  padding: 1rem 1.25rem;
  margin-bottom: 1rem;
}
.banner.warn { background: var(--warning-light); border-color: var(--warning); color: var(--warning); }
.footer {
  text-align: center;
  color: var(--text-muted);
  font-size: 0.8rem;
  margin-top: 1.5rem;
}
.spinner {
  display: inline-block;
  width: 14px; height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  vertical-align: middle;
  margin-right: 0.5rem;
}
@keyframes spin { to { transform: rotate(360deg); } }
"""


_DASHBOARD_JS = """
(function () {
  const STATS_URL = '/api/v1/dashboard/stats';
  const PROJECT_REPORTS_URL = '/api/v1/dashboard/projects';

  function fmt(n) {
    if (n == null) return '—';
    return new Intl.NumberFormat('ru-RU').format(n);
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('ru-RU'); } catch (e) { return iso; }
  }
  function fmtDuration(ms) {
    if (ms == null) return '—';
    const totalSec = Math.round(ms / 1000);
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    if (m === 0) return s + ' сек';
    return m + ' мин ' + String(s).padStart(2, '0') + ' сек';
  }
  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === 'class') e.className = attrs[k];
      else if (k === 'text') e.textContent = attrs[k];
      else if (k.startsWith('data-')) e.setAttribute(k, attrs[k]);
      else e[k] = attrs[k];
    }
    if (children) for (const c of children) if (c) e.appendChild(c);
    return e;
  }

  let CURRENT_WINDOW = { kind: 'days', value: 30 };
  let WINDOW_DAYS = 30;
  let PROJECT_REPORTS_CACHE = {};

  function windowQS() {
    if (CURRENT_WINDOW.kind === 'day') {
      return 'date=' + encodeURIComponent(CURRENT_WINDOW.value);
    }
    return 'days=' + encodeURIComponent(CURRENT_WINDOW.value);
  }

  function renderWindowLabel(window, kpis) {
    const node = document.getElementById('windowLabel');
    if (!node) return;
    if (window.kind === 'day') {
      node.textContent = 'За день: ' + window.value;
    } else {
      node.textContent = 'За последние ' + window.value + ' дней';
    }
    if (kpis && kpis.peak_day) {
      node.textContent += ' · пиковый день: ' + kpis.peak_day + ' (' + kpis.peak_day_count + ')';
    }
  }

  function avgPerDay(kpis) {
    if (CURRENT_WINDOW.kind === 'day') return kpis.total_reports;
    const days = Math.max(1, parseInt(CURRENT_WINDOW.value, 10) || 1);
    return Math.round((kpis.total_reports || 0) / days * 10) / 10;
  }

  function fmtSinceDate(iso) {
    // Префикс «данные с» подчёркивает, что это абсолютная дата начала сбора
    // статистики по таблице, а не нижняя граница текущего окна — счётчики
    // выше уже отфильтрованы по окну, дата же остаётся глобальной.
    if (!iso) return null;
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return null;
      return 'данные с ' + d.toLocaleDateString('ru-RU');
    } catch (e) { return null; }
  }

  function appendCard(grid, cls, label, value, subIso) {
    const children = [
      el('div', { class: 'label', text: label }),
      el('div', { class: 'value', text: value }),
    ];
    const sub = fmtSinceDate(subIso);
    if (sub) children.push(el('div', { class: 'sub', text: sub }));
    grid.appendChild(el('div', { class: 'kpi-card' + (cls ? ' ' + cls : '') }, children));
  }

  function renderKpis(kpis) {
    const grid = document.getElementById('kpis');
    const tokensGrid = document.getElementById('kpisTokens');
    grid.innerHTML = '';
    tokensGrid.innerHTML = '';
    const mainCards = [
      ['reports',  'Отчёты',                  fmt(kpis.total_reports),                        kpis.earliest_report_at],
      ['reports',  'Просмотры отчётов',       fmt(kpis.report_views),                         kpis.earliest_report_view_at],
      ['',         'Уникальных запусков',     fmt(kpis.unique_launches),                      kpis.earliest_report_at],
      ['duration', 'Среднее время анализа',   fmtDuration(kpis.avg_analysis_duration_ms),     kpis.earliest_report_at],
      ['',         'Записи в базе знаний',    fmt(kpis.total_kb_entries),                     kpis.earliest_kb_entry_at],
      ['',         'Merge rules',             fmt(kpis.total_merge_rules),                    kpis.earliest_merge_rule_at],
      ['',         'Активных проектов',       fmt(kpis.active_projects),                      kpis.earliest_report_at],
      ['',         'Среднее отчётов / день',  fmt(avgPerDay(kpis)),                           kpis.earliest_report_at],
    ];
    const tokenCards = [
      ['tokens',   'Токены за период',        fmt(kpis.llm_total_tokens),                     kpis.earliest_report_at],
      ['tokens',   'Входные за период',       fmt(kpis.llm_prompt_tokens),                    kpis.earliest_report_at],
      ['tokens',   'Выходные за период',      fmt(kpis.llm_completion_tokens),                kpis.earliest_report_at],
      ['tokens',   'Токены / прогон',         fmt(kpis.llm_avg_tokens_per_run),               kpis.earliest_report_at],
      ['tokens',   'Входные / прогон',        fmt(kpis.llm_avg_prompt_tokens_per_run),        kpis.earliest_report_at],
      ['tokens',   'Выходные / прогон',       fmt(kpis.llm_avg_completion_tokens_per_run),    kpis.earliest_report_at],
    ];
    for (const [cls, label, value, subIso] of mainCards)  appendCard(grid,       cls, label, value, subIso);
    for (const [cls, label, value, subIso] of tokenCards) appendCard(tokensGrid, cls, label, value, subIso);
  }

  function renderSeries(series) {
    const wrap = document.getElementById('bars');
    wrap.innerHTML = '';
    let max = 0;
    for (const p of series) if (p.n > max) max = p.n;
    for (const p of series) {
      const h = max === 0 ? 0 : (p.n / max) * 100;
      const bar = el('div', {
        class: 'bar',
        title: p.day + ': ' + p.n,
        style: 'height:' + (p.n === 0 ? 1 : h) + '%',
        'data-n': String(p.n),
      });
      wrap.appendChild(bar);
    }
    const axis = document.getElementById('barsAxis');
    axis.innerHTML = '';
    if (series.length > 0) {
      axis.appendChild(el('span', { text: series[0].day }));
      axis.appendChild(el('span', { text: 'max ' + fmt(max) + '/день' }));
      axis.appendChild(el('span', { text: series[series.length - 1].day }));
    }
  }

  let TABLE_ROWS = [];
  let SORT_COL = 'reports';
  let SORT_DIR = -1;

  function projectKey(row) {
    return row.project_id === null ? '_null' : String(row.project_id);
  }

  function renderTable() {
    const tbody = document.querySelector('#projectsTable tbody');
    tbody.innerHTML = '';

    const named = TABLE_ROWS.filter(r => r.project_id !== null);
    const unattributed = TABLE_ROWS.filter(r => r.project_id === null);
    named.sort((a, b) => {
      const va = a[SORT_COL]; const vb = b[SORT_COL];
      if (typeof va === 'string' || typeof vb === 'string') {
        return String(va || '').localeCompare(String(vb || '')) * SORT_DIR;
      }
      return ((va || 0) - (vb || 0)) * SORT_DIR;
    });

    const ordered = named.concat(unattributed);
    if (ordered.length === 0) {
      tbody.appendChild(el('tr', {}, [
        el('td', { colSpan: 11, class: 'muted', text: 'Нет данных за период' }),
      ]));
      return;
    }
    for (const r of ordered) {
      const key = projectKey(r);
      const tr = el('tr', {
        class: 'project-row' + (r.project_id === null ? ' unattributed' : ''),
        'data-key': key,
      }, [
        el('td', {}, [
          el('span', { class: 'disclosure', text: '▸' }),
          document.createTextNode(r.project_name),
        ]),
        el('td', { class: 'num', text: fmt(r.reports) }),
        el('td', { class: 'num', text: fmt(r.report_views) }),
        el('td', { class: 'num', text: fmt(r.kb_entries) }),
        el('td', { class: 'num', text: fmt(r.merge_rules) }),
        el('td', { class: 'num', text: fmt(r.llm_total_tokens) }),
        el('td', { class: 'num', text: fmt(r.llm_prompt_tokens) }),
        el('td', { class: 'num', text: fmt(r.llm_completion_tokens) }),
        el('td', { class: 'num', text: fmt(r.llm_avg_tokens_per_run) }),
        el('td', { class: 'num', text: fmtDuration(r.avg_analysis_duration_ms) }),
        el('td', { class: 'muted', text: fmtDate(r.last_activity) }),
      ]);
      tr.addEventListener('click', () => toggleExpand(tr, r));
      tbody.appendChild(tr);
    }

    const ths = document.querySelectorAll('#projectsTable thead th');
    ths.forEach(th => {
      th.classList.remove('sorted-asc', 'sorted-desc');
      if (th.dataset.col === SORT_COL) {
        th.classList.add(SORT_DIR === 1 ? 'sorted-asc' : 'sorted-desc');
      }
    });
  }

  async function toggleExpand(tr, row) {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('expand-row') && next.dataset.key === tr.dataset.key) {
      next.remove();
      tr.classList.remove('expanded');
      return;
    }
    if (next && next.classList.contains('expand-row')) next.remove();

    tr.classList.add('expanded');
    const expandTr = el('tr', { class: 'expand-row', 'data-key': tr.dataset.key });
    const td = el('td', { colSpan: 11 });
    const inner = el('div', { class: 'expand-inner' });
    inner.innerHTML = '<span class="spinner"></span>Загрузка отчётов…';
    td.appendChild(inner);
    expandTr.appendChild(td);
    tr.parentNode.insertBefore(expandTr, tr.nextSibling);

    const cacheKey = tr.dataset.key + '|' + windowQS();
    let reports;
    if (PROJECT_REPORTS_CACHE[cacheKey]) {
      reports = PROJECT_REPORTS_CACHE[cacheKey];
    } else {
      const projectIdParam = row.project_id === null ? 0 : row.project_id;
      try {
        const resp = await fetch(PROJECT_REPORTS_URL + '/' + projectIdParam + '/reports?' + windowQS());
        if (!resp.ok) {
          inner.innerHTML = '';
          inner.appendChild(el('div', { class: 'empty', text: 'Ошибка загрузки: ' + resp.status }));
          return;
        }
        const data = await resp.json();
        reports = data.reports || [];
        PROJECT_REPORTS_CACHE[cacheKey] = reports;
      } catch (err) {
        inner.innerHTML = '';
        inner.appendChild(el('div', { class: 'empty', text: 'Не удалось загрузить отчёты' }));
        return;
      }
    }

    inner.innerHTML = '';
    if (reports.length === 0) {
      inner.appendChild(el('div', { class: 'empty', text: 'Нет отчётов в выбранном окне' }));
      return;
    }
    const tbl = el('table');
    const thead = el('thead', {}, [
      el('tr', {}, [
        el('th', { text: 'Создан' }),
        el('th', { text: 'Launch' }),
        el('th', { class: 'num', text: 'Просмотры' }),
        el('th', { class: 'num', text: 'Входные' }),
        el('th', { class: 'num', text: 'Выходные' }),
        el('th', { class: 'num', text: 'Токены' }),
        el('th', { class: 'num', text: 'Длительность' }),
        el('th', { text: 'Отчёт' }),
      ]),
    ]);
    const tb = el('tbody');
    for (const rep of reports) {
      const link = el('a', {
        href: '/reports/' + encodeURIComponent(rep.filename),
        target: '_blank',
        rel: 'noopener',
        text: 'открыть',
      });
      const launchText = rep.launch_id == null ? '—' : '#' + rep.launch_id;
      tb.appendChild(el('tr', {}, [
        el('td', { text: fmtDate(rep.created_at) }),
        el('td', { text: launchText }),
        el('td', { class: 'num', text: fmt(rep.view_count) }),
        el('td', { class: 'num', text: fmt(rep.llm_prompt_tokens) }),
        el('td', { class: 'num', text: fmt(rep.llm_completion_tokens) }),
        el('td', { class: 'num', text: fmt(rep.llm_total_tokens) }),
        el('td', { class: 'num', text: fmtDuration(rep.analysis_duration_ms) }),
        el('td', {}, [link]),
      ]));
    }
    tbl.appendChild(thead);
    tbl.appendChild(tb);
    inner.appendChild(tbl);
  }

  function bindSort() {
    document.querySelectorAll('#projectsTable thead th').forEach(th => {
      th.addEventListener('click', () => {
        const col = th.dataset.col;
        if (!col) return;
        if (SORT_COL === col) SORT_DIR = -SORT_DIR;
        else { SORT_COL = col; SORT_DIR = -1; }
        renderTable();
      });
    });
  }

  function showError(message) {
    const root = document.getElementById('content');
    root.innerHTML = '';
    const banner = el('div', { class: 'banner', text: message });
    root.appendChild(banner);
  }

  function setBusy(busy) {
    const status = document.getElementById('status');
    status.innerHTML = busy ? '<span class="spinner"></span>Загрузка…' : '';
  }

  async function load() {
    setBusy(true);
    PROJECT_REPORTS_CACHE = {};
    try {
      const resp = await fetch(STATS_URL + '?' + windowQS());
      if (!resp.ok) {
        let detail = resp.statusText;
        try { const j = await resp.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
        if (resp.status === 503) {
          showError('Дашборд недоступен: настройте ALLURE_KB_POSTGRES_DSN. (' + detail + ')');
          return;
        }
        showError('Ошибка загрузки: ' + resp.status + ' ' + detail);
        return;
      }
      const data = await resp.json();
      renderWindowLabel(data.window || CURRENT_WINDOW, data.kpis);
      renderKpis(data.kpis);
      renderSeries(data.series);
      TABLE_ROWS = data.per_project;
      renderTable();
      const ts = document.getElementById('generatedAt');
      ts.textContent = 'Обновлено: ' + fmtDate(data.generated_at);
    } catch (err) {
      showError('Не удалось получить данные: ' + (err && err.message ? err.message : err));
    } finally {
      setBusy(false);
    }
  }

  function applyDay(dayValue) {
    const select = document.getElementById('daysSelect');
    const reset = document.getElementById('resetDay');
    if (dayValue) {
      CURRENT_WINDOW = { kind: 'day', value: dayValue };
      select.disabled = true;
      reset.disabled = false;
    } else {
      CURRENT_WINDOW = { kind: 'days', value: parseInt(select.value, 10) || 30 };
      WINDOW_DAYS = CURRENT_WINDOW.value;
      select.disabled = false;
      reset.disabled = true;
    }
    load();
  }

  window.addEventListener('DOMContentLoaded', () => {
    bindSort();
    const select = document.getElementById('daysSelect');
    const dateInput = document.getElementById('daySelect');
    const reset = document.getElementById('resetDay');

    CURRENT_WINDOW = { kind: 'days', value: parseInt(select.value, 10) || 30 };
    WINDOW_DAYS = CURRENT_WINDOW.value;
    reset.disabled = true;

    select.addEventListener('change', () => {
      if (dateInput.value) return;
      CURRENT_WINDOW = { kind: 'days', value: parseInt(select.value, 10) || 30 };
      WINDOW_DAYS = CURRENT_WINDOW.value;
      load();
    });
    dateInput.addEventListener('change', () => applyDay(dateInput.value));
    reset.addEventListener('click', () => {
      dateInput.value = '';
      applyDay(null);
    });

    load();
  });
})();
"""


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>alla — дашборд использования</title>
  <style>{css}</style>
</head>
<body>
  <div class="container">
    <header class="header">
      {logo_html}
      <div class="header-title">Дашборд использования alla</div>
      <div class="header-controls">
        <span id="status" class="muted"></span>
        <label for="daysSelect">Период:</label>
        <select id="daysSelect">
          <option value="30" selected>30 дней</option>
          <option value="60">60 дней</option>
          <option value="90">90 дней</option>
        </select>
        <label for="daySelect">Конкретный день:</label>
        <input id="daySelect" type="date">
        <button id="resetDay" class="ghost" type="button">Сбросить день</button>
      </div>
    </header>

    <div id="windowLabel" class="window-label"></div>

    <div id="content">
      <section class="section">
        <h2>Ключевые показатели</h2>
        <div id="kpis" class="kpis"></div>
      </section>

      <section class="section tokens-section">
        <h2>Расход токенов LLM</h2>
        <div id="kpisTokens" class="kpis kpis-compact"></div>
      </section>

      <section class="section">
        <h2>Отчёты по дням</h2>
        <div id="bars" class="bars"></div>
        <div id="barsAxis" class="bars-axis"></div>
      </section>

      <section class="section">
        <h2>Все проекты</h2>
        <p class="muted" style="font-size:0.85rem;margin-bottom:0.5rem">
          Нажмите на проект, чтобы развернуть список отчётов с прямыми ссылками.
        </p>
        <table id="projectsTable">
          <thead>
            <tr>
              <th data-col="project_name">Проект</th>
              <th data-col="reports"     class="num">Отчёты</th>
              <th data-col="report_views" class="num">Просмотры</th>
              <th data-col="kb_entries"  class="num">База знаний</th>
              <th data-col="merge_rules" class="num">Merge rules</th>
              <th data-col="llm_total_tokens" class="num">Токены</th>
              <th data-col="llm_prompt_tokens" class="num">Входные</th>
              <th data-col="llm_completion_tokens" class="num">Выходные</th>
              <th data-col="llm_avg_tokens_per_run" class="num">Токены/прогон</th>
              <th data-col="avg_analysis_duration_ms" class="num">Время анализа</th>
              <th data-col="last_activity">Последняя активность</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </section>
    </div>

    <div class="footer" id="generatedAt"></div>
  </div>
  <script>{js}</script>
</body>
</html>
"""


@lru_cache(maxsize=1)
def render_dashboard_html_shell() -> str:
    """Вернуть self-contained HTML-страницу дашборда (без данных).

    Кэшируется: статика, без зависимости от запроса.
    """
    logo_uri = _logo_data_uri()
    if logo_uri:
        logo_html = (
            f'<img class="header-logo" src="{logo_uri}" alt="alla logo">'
        )
    else:
        logo_html = ""
    return _HTML_TEMPLATE.format(
        css=_DASHBOARD_CSS,
        js=_DASHBOARD_JS,
        logo_html=logo_html,
    )
