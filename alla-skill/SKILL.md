---
name: alla
description: Анализ упавших автотестов из Allure TestOps — кластеризация, поиск в базе знаний, агентский анализ кластеров через subagents, итоговый summary прогона, HTML-отчёт, опциональный постинг рекомендаций в TestOps. Используй при запросах "проанализируй прогон/launch", "разбери падения", "почему упал launch ...", "сделай отчёт по упавшим тестам", "посмотри кластеры падений", "alla 12345" с упоминанием Allure TestOps или alla.
---

# alla — триаж упавших автотестов

## Overview

Скилл — это набор Python-скриптов в `alla-skill/scripts/`, которые работают с
Allure TestOps API и PostgreSQL. Бизнес-логика (получение тестов, извлечение
логов, кластеризация, поиск в базе знаний, генерация HTML-отчёта, постинг
комментариев) живёт в публичных сервисах внутри пакета `alla` (`src/alla/`)
и переиспользуется как server-side, так и скилл-режимом.

**Pipeline скилл-режима совпадает с серверным.** `fetch_clusters.py` идёт
теми же шагами и под теми же gate'ами, что
`alla.orchestrator.analyze_launch` (триаж → логи → кластеризация → merge
rules при `kb_active=True` → KB lookup → onboarding). `generate_report.py`
рендерит HTML через `alla.app_support.build_html_report_content`, поэтому
интерактивные элементы (KB-кнопки, like/dislike, merge rules, rerun)
появляются по тем же условиям. Это значит, что для одного launch_id
серверный CLI и скилл строят одинаковые кластеры и структурно
идентичный HTML — отличается только содержимое LLM-ответов (сервер ходит
в GigaChat, скилл — в текущего агента).

**LLM-анализ выполняешь ты, агент CLI.** Скрипты выдают тебе готовый
`system_prompt + user_prompt + контекст` для каждого кластера и для
итогового summary — те же промпты, что использует server-side GigaChat-путь.
Ты возвращаешь результат своим стандартным способом (в Claude Code —
через Task subagents, в qwen — через subagents, в codex — инлайн-циклом).

## Quick Start

```bash
cd alla-skill
cp .env.example .env       # заполни ALLURE_ENDPOINT, ALLURE_TOKEN,
                           # ALLURE_PROJECT_ID, ALLURE_KB_POSTGRES_DSN
pip install -e ..          # установит пакет alla из корня
psql "$ALLURE_KB_POSTGRES_DSN" -f sql/skill_run_schema.sql
# (один раз) применить миграции KB/feedback/merge_rules из ../sql/
```

## Workflow

### 1. Получить кластеры

```bash
python alla-skill/scripts/fetch_clusters.py --launch-id 12345
```

Stdout — JSON:

```json
{"ok": true, "run_id": 42,
 "launch": {"id": 12345, "name": "...", "project_id": 1},
 "counters": {"total_results": 320, "passed": 280, "failed": 30,
              "broken": 10, "skipped": 0, "muted_failures": 2,
              "active_failures": 38},
 "cluster_count": 5, "unclustered_count": 0,
 "clusters": [
   {"cluster_id": "c-abc", "label": "ConnectionTimeoutException",
    "size": 12, "representative_test_id": 99887,
    "example_step_path": "Login → Submit",
    "message_preview": "Connection timed out after 30s",
    "kb_match_count": 2,
    "top_kb_match": {"title": "Network flake", "score": 0.87,
                     "tier": "Tier 2"}}
 ]}
```

`run_id` — primary handle для всех последующих шагов.

### 2. Делегировать анализ кластеров

Стратегия зависит от `cluster_count` (см. `references/delegation_strategy.md`):

| `cluster_count` | Стратегия |
|---|---|
| 1–2 | Inline в основном агенте |
| 3–10 | Один subagent на кластер |
| 11–30 | Batched subagents (2–3 кластера на subagent) |
| >30 | Deep top-30 по `size × (1 − kb_score)` + tail summary |

Каждое звено анализа:

```bash
python alla-skill/scripts/get_cluster_context.py --run-id 42 --cluster-id c-abc
```

выдаёт:

```json
{"ok": true, "cluster_id": "c-abc",
 "system_prompt": "...", "user_prompt": "...",
 "context": {"label": "...", "size": 12, "representative": {...},
             "members": [...], "kb_matches": [...],
             "kb_query_provenance": {...}}}
```

Применяй `system_prompt` + `user_prompt` (это готовый промпт для анализа
одного кластера — тот же, что использует server-side GigaChat-путь),
возвращай JSON по схеме `references/cluster_analysis_guide.md`.

### 3. Собрать launch summary

```bash
python alla-skill/scripts/get_summary_context.py --run-id 42
```

выдаёт `system_prompt + user_prompt + counters/top_clusters` для итогового
отчёта по прогону. Пиши `summary_text` (2–4 абзаца) по
`references/launch_summary_guide.md`.

### 4. Записать анализ в БД

```bash
cat analysis.json | python alla-skill/scripts/submit_analysis.py --run-id 42 --input -
```

Полная схема `analysis.json` — `references/analysis_schema.md`.

### 5. Сгенерировать HTML-отчёт

```bash
python alla-skill/scripts/generate_report.py --run-id 42
```

Stdout — JSON: `report_filename`, `report_url`, `saved_to_db`,
`saved_to_disk`. Отчёт сохраняется в PostgreSQL (`alla.report`) и/или на
диск (`ALLURE_REPORTS_DIR`).

### 6. (Опц.) Постинг в Allure TestOps

**Push выключен по умолчанию (`ALLURE_PUSH_TO_TESTOPS=false`). Не вызывай
push без явного запроса от пользователя.** Дефолт — `--dry-run`.

```bash
# Посмотреть, что было бы записано
python alla-skill/scripts/push_to_testops.py --run-id 42 --dry-run

# Реальный push (только при явном разрешении)
python alla-skill/scripts/push_to_testops.py --run-id 42 --confirm
```

## Финальный ответ пользователю

* Краткий summary прогона на русском (твой `summary_text`).
* Ссылка/путь на HTML-отчёт.
* Если push выполнялся — счётчики (`comments_posted`, `comments_failed`).
  При `comments_failed > 0` явно упомяни.
* Если есть `unanalyzed_tail` — обязательно скажи: «Глубоко проанализировано
  N кластеров, ещё M кластеров (K тестов) попали в tail-summary без
  индивидуального разбора».

## Использование с qwen / codex CLI

См. `references/qwen_subagents.md` — готовый YAML для
`~/.qwen/agents/alla-cluster-analyzer.yaml` и инструкция по запуску.

В codex CLI (без Task tool) используй inline-цикл по кластерам с тем же
промптом, что выдаёт `get_cluster_context.py`.

## Дополнительные операции

```bash
# Найти launch_id по имени
python alla-skill/scripts/resolve_launch.py --name "Smoke regression" --project-id 1

# Удалить ранее запушенные [alla]-комментарии
python alla-skill/scripts/delete_comments.py --launch-id 12345 [--dry-run]

# CRUD KB-записей
python alla-skill/scripts/manage_kb.py list --project-id 1
python alla-skill/scripts/manage_kb.py create --json - < kb_entry.json
python alla-skill/scripts/manage_kb.py update --entry-id 17 --json - < patch.json
python alla-skill/scripts/manage_kb.py delete --entry-id 17

# Like/dislike feedback на kb_match
python alla-skill/scripts/record_feedback.py --run-id 42 --cluster-id c-abc \
  --kb-entry-id 17 --vote like
```

## Конфигурация

Конфиг загружается из `alla-skill/.env` (явный путь относительно файла
скрипта, не из CWD). Ключевые переменные:

| Переменная | Обязательно | Описание |
|---|---|---|
| `ALLURE_ENDPOINT` | да | URL Allure TestOps |
| `ALLURE_TOKEN` | да | API-токен |
| `ALLURE_PROJECT_ID` | да | ID проекта |
| `ALLURE_KB_POSTGRES_DSN` | да | DSN PostgreSQL |
| `ALLURE_PUSH_TO_TESTOPS` | нет | По умолчанию `false`. Не включай по своей инициативе. |
| `ALLURE_REPORTS_DIR` | нет | Директория для HTML-отчётов |

Полный список — в `.env.example`.

## Troubleshooting

См. `references/troubleshooting.md`. Часто встречающиеся ошибки:

* `ConfigurationError: ALLURE_KB_POSTGRES_DSN required` — не заполнен `.env`.
* `psycopg.OperationalError` — недоступна БД, проверь DSN/sslmode/доступы.
* `404 launch not found` — неверный `project_id` или нет прав у токена.
* `push_disabled` — попытка push без `--confirm` при `ALLURE_PUSH_TO_TESTOPS=false`.
