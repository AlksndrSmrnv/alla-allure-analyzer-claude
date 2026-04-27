"""Self-contained HTML-страница дашборда использования.

Серверный шаблон отдаёт пустую оболочку. Данные подгружаются на клиенте
через ``fetch('/api/v1/dashboard/stats?days=N')`` и рендерятся ванильным JS.
Ни внешних CSS, ни сторонних скриптов — только inline.
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
}
.header-logo { height: 40px; width: auto; flex-shrink: 0; }
.header-title { font-size: 1.25rem; font-weight: 600; flex: 1; }
.header-controls { display: flex; align-items: center; gap: 0.5rem; }
.header-controls label { color: var(--text-muted); font-size: 0.85rem; }
.header-controls select {
  padding: 0.4rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text);
  font-size: 0.9rem;
  cursor: pointer;
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
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
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
.kpi-card.likes  .value { color: var(--success); }
.kpi-card.dislikes .value { color: var(--danger); }
.kpi-card.reports .value { color: var(--primary); }
.ratio-bar {
  display: flex;
  height: 14px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--border);
  margin-top: 0.5rem;
}
.ratio-bar .like { background: var(--success); }
.ratio-bar .dislike { background: var(--danger); }
.ratio-legend { font-size: 0.85rem; color: var(--text-muted); margin-top: 0.5rem; display: flex; gap: 1rem; }
.ratio-legend .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: middle; margin-right: 4px; }
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
tbody tr:hover { background: var(--bg); }
tbody tr.unattributed { color: var(--text-muted); font-style: italic; }
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
.top5 ol { padding-left: 1.5rem; }
.top5 li { margin: 0.4rem 0; }
.top5 li b { color: var(--primary); }
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
  const API_URL = '/api/v1/dashboard/stats';

  function fmt(n) {
    if (n == null) return '—';
    return new Intl.NumberFormat('ru-RU').format(n);
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('ru-RU'); } catch (e) { return iso; }
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

  function renderKpis(kpis) {
    const grid = document.getElementById('kpis');
    grid.innerHTML = '';
    const cards = [
      ['reports',  'Отчёты',          kpis.total_reports],
      ['',         'Записи в базе знаний', kpis.total_kb_entries],
      ['likes',    'Лайки',           kpis.total_likes],
      ['dislikes', 'Дизлайки',        kpis.total_dislikes],
      ['',         'Merge rules',     kpis.total_merge_rules],
      ['',         'Активных проектов', kpis.active_projects],
    ];
    for (const [cls, label, value] of cards) {
      const card = el('div', { class: 'kpi-card' + (cls ? ' ' + cls : '') }, [
        el('div', { class: 'label', text: label }),
        el('div', { class: 'value', text: fmt(value) }),
      ]);
      grid.appendChild(card);
    }

    const total = (kpis.total_likes || 0) + (kpis.total_dislikes || 0);
    const ratioBar = document.getElementById('ratioBar');
    const legend   = document.getElementById('ratioLegend');
    if (total === 0) {
      ratioBar.innerHTML = '<div style="flex:1;background:var(--border)"></div>';
      legend.textContent = 'Пока нет голосов';
    } else {
      const likePct = Math.round(100 * kpis.total_likes / total);
      ratioBar.innerHTML = '';
      ratioBar.appendChild(el('div', { class: 'like',    style: 'width:' + likePct + '%' }));
      ratioBar.appendChild(el('div', { class: 'dislike', style: 'width:' + (100 - likePct) + '%' }));
      legend.innerHTML = '';
      legend.appendChild(el('span', { text: '' }, [
        el('span', { class: 'swatch', style: 'background:var(--success)' }),
        document.createTextNode(' Лайки: ' + fmt(kpis.total_likes) + ' (' + likePct + '%)'),
      ]));
      legend.appendChild(el('span', { text: '' }, [
        el('span', { class: 'swatch', style: 'background:var(--danger)' }),
        document.createTextNode(' Дизлайки: ' + fmt(kpis.total_dislikes) + ' (' + (100 - likePct) + '%)'),
      ]));
    }
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

  function renderTop5(rows) {
    const ol = document.getElementById('top5List');
    ol.innerHTML = '';
    const named = rows.filter(r => r.project_id !== null).slice(0, 5);
    if (named.length === 0) {
      ol.appendChild(el('li', { class: 'muted', text: 'Нет данных за период' }));
      return;
    }
    for (const r of named) {
      const li = el('li', {}, [
        el('b', { text: r.project_name }),
        document.createTextNode(' — отчётов: ' + fmt(r.reports) +
          ', KB-записей: ' + fmt(r.kb_entries) +
          ', лайки/дизлайки: ' + fmt(r.likes) + '/' + fmt(r.dislikes)),
      ]);
      ol.appendChild(li);
    }
  }

  let TABLE_ROWS = [];
  let SORT_COL = 'reports';
  let SORT_DIR = -1;

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
        el('td', { colSpan: 7, class: 'muted', text: 'Нет данных за период' }),
      ]));
      return;
    }
    for (const r of ordered) {
      const tr = el('tr', { class: r.project_id === null ? 'unattributed' : '' }, [
        el('td', { text: r.project_name }),
        el('td', { class: 'num', text: fmt(r.reports) }),
        el('td', { class: 'num', text: fmt(r.kb_entries) }),
        el('td', { class: 'num', text: fmt(r.likes) }),
        el('td', { class: 'num', text: fmt(r.dislikes) }),
        el('td', { class: 'num', text: fmt(r.merge_rules) }),
        el('td', { class: 'muted', text: fmtDate(r.last_activity) }),
      ]);
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

  async function load(days) {
    setBusy(true);
    try {
      const resp = await fetch(API_URL + '?days=' + encodeURIComponent(days));
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
      renderKpis(data.kpis);
      renderSeries(data.series);
      renderTop5(data.per_project);
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

  window.addEventListener('DOMContentLoaded', () => {
    bindSort();
    const select = document.getElementById('daysSelect');
    select.addEventListener('change', () => load(select.value));
    load(select.value);
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
      </div>
    </header>

    <div id="content">
      <section class="section">
        <h2>Ключевые показатели</h2>
        <div id="kpis" class="kpis"></div>
        <div style="margin-top:1.25rem">
          <div class="muted" style="font-size:0.8rem;text-transform:uppercase;letter-spacing:0.04em">Лайки vs дизлайки</div>
          <div id="ratioBar" class="ratio-bar"></div>
          <div id="ratioLegend" class="ratio-legend"></div>
        </div>
      </section>

      <section class="section">
        <h2>Отчёты по дням</h2>
        <div id="bars" class="bars"></div>
        <div id="barsAxis" class="bars-axis"></div>
      </section>

      <section class="section top5">
        <h2>Топ-5 проектов</h2>
        <ol id="top5List"></ol>
      </section>

      <section class="section">
        <h2>Все проекты</h2>
        <table id="projectsTable">
          <thead>
            <tr>
              <th data-col="project_name">Проект</th>
              <th data-col="reports"     class="num">Отчёты</th>
              <th data-col="kb_entries"  class="num">База знаний</th>
              <th data-col="likes"       class="num">Лайки</th>
              <th data-col="dislikes"    class="num">Дизлайки</th>
              <th data-col="merge_rules" class="num">Merge rules</th>
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
