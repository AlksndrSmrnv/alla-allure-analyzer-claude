# alla-skill workflow

Подробный пошаговый workflow для агента CLI. Используй, когда нужны
больше деталей, чем в Quick Start `SKILL.md`.

## Шаги

### 1. Получить кластеры

```bash
python alla-skill/scripts/fetch_clusters.py --launch-id 12345
```

или по имени:

```bash
python alla-skill/scripts/fetch_clusters.py \
  --launch-name "Smoke regression" --project-id 1
```

Опции: `--no-merge-rules`, `--no-kb`.

Что происходит:

1. Triage (`/api/launch`, `/api/testresult`, `/api/testresult/{id}/execution`).
2. Log enrichment (`/api/testresult/attachment`).
3. Кластеризация TF-IDF (message + log fallback).
4. Применение merge rules (`alla.merge_rules`).
5. KB lookup (`alla.kb_entry`) + exact-feedback rerank (`alla.kb_feedback`).
6. INSERT новой строки в `alla.skill_run` со status `clustered`.

Stdout — компактный JSON с `run_id`, счётчиками, top-уровневой
информацией о каждом кластере (без длинных сообщений и трейсов —
их можно дотянуть через `get_cluster_context.py`).

**Что делать при `cluster_count == 0`:** скажи пользователю, что в
прогоне нет активных падений, не вызывая остальных скриптов.

**Что делать при ошибке БД (`psycopg.OperationalError`):**
проверь `ALLURE_KB_POSTGRES_DSN`, `sslmode`, доступы. См.
`troubleshooting.md`.

### 2. Делегировать анализ кластеров

См. `delegation_strategy.md`. Каждое звено анализа:

```bash
python alla-skill/scripts/get_cluster_context.py \
  --run-id 42 --cluster-id c-abc123
```

Применяй `system_prompt` + `user_prompt` (это тот же промпт, что
строит server-side GigaChat-путь). Subagent возвращает JSON одного
кластера по схеме из `cluster_analysis_guide.md`.

### 3. Собрать launch summary

```bash
python alla-skill/scripts/get_summary_context.py --run-id 42
```

Скрипт вытащит из `alla.skill_run` уже сохранённый агентский анализ
(если ты уже сделал submit) или подмешает в промпт сырые данные
кластеров, и выдаст готовый промпт для итогового summary.

Альтернативно — передай stdin промежуточный JSON cluster_analyses,
если ты ещё не сделал submit:

```bash
echo '{"clusters": {"c-abc": {"analysis_text": "..."}}}' \
  | python alla-skill/scripts/get_summary_context.py \
      --run-id 42 --analyses-input -
```

### 4. Submit anlysis

```bash
cat analysis.json | \
  python alla-skill/scripts/submit_analysis.py --run-id 42 --input -
```

Полная схема `analysis.json` — `analysis_schema.md`. Скрипт валидирует
`schema_version`, категории, confidence levels, длину `analysis_text`
и UPDATE'ит `alla.skill_run` (status `analyzed`).

`missing_cluster_ids` в ответе — кластеры, для которых ты не прислал
анализ. Не блокирует запись (warning), но HTML-отчёт для них покажет
"Кластер не проанализирован агентом". Если ты в режиме `>30 кластеров`
и хочешь явно отметить tail — пришли для них `category: "unanalyzed"`.

### 5. Сгенерировать HTML-отчёт

```bash
python alla-skill/scripts/generate_report.py --run-id 42
```

Опции:

* `--out path/to/file.html` — явный путь сохранения.
* `--no-save-to-db` — не сохранять в `alla.report`.

Без `--out` отчёт пишется в `ALLURE_REPORTS_DIR` (если задан).

В stdout: `report_filename`, `report_url` (составляется из
`ALLURE_SERVER_EXTERNAL_URL` + filename, или из `ALLURE_REPORT_URL`),
`saved_to_db`, `saved_to_disk`, `html_size_bytes`.

### 6. (Опц.) Push в Allure TestOps

**Push выключен по умолчанию.** Не вызывай без явного запроса
пользователя. Сначала dry-run:

```bash
python alla-skill/scripts/push_to_testops.py --run-id 42 --dry-run
```

Реальный push (только если пользователь явно попросил):

```bash
python alla-skill/scripts/push_to_testops.py --run-id 42 --confirm
```

Опции:

* `--attach-report-url URL` — передать ссылку явно.
* `--report-url-from-db` — взять `report_url`, сохранённый
  `generate_report` в `alla.skill_run`.

Если пользователь решил откатить — `delete_comments.py --launch-id 12345`
удалит все `[alla]`-комментарии для упавших тестов launch'а.

## Финальный ответ пользователю

После шагов 1–5 (минимум) или 1–6 (с push):

* Краткий summary прогона (твой `summary_text`).
* Ссылка/путь на HTML-отчёт.
* Если push выполнялся — счётчики (`comments_posted`, `comments_failed`,
  `report_link_attached`). При `comments_failed > 0` явно упомяни.
* Если был tail (`>30 кластеров`) — обязательно скажи: «Глубоко
  проанализировано N кластеров, ещё M (K тестов) попали в tail-summary
  без индивидуального разбора».
