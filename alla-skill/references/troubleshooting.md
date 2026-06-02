# Troubleshooting

Типичные ошибки и как их быстро устранить.

## `ALLURE_FEEDBACK_SERVER_URL не задан` (EXIT_CONFIG)

Весь pipeline ходит в PostgreSQL через `alla-server`. Скрипту нужен URL
сервера, а не DSN. Проверь:

1. Файл `alla-skill/.env` существует.
2. В нём задано `ALLURE_FEEDBACK_SERVER_URL=http://127.0.0.1:8090` (или
   продовый URL `alla-server`).
3. Сервер запущен: `python alla-skill/scripts/serve.py` (для local-first)
   или доступен прод.

## `ConfigurationError: .env не найден ...`

Скрипт ищет `.env` по абсолютному пути относительно директории
скрипта (а не CWD). Скопируй `.env.example` → `.env` в корне
`alla-skill/`:

```bash
cp alla-skill/.env.example alla-skill/.env
```

## `AllaApiConnectionError` / «Не удалось подключиться к alla-server»

Сервер недоступен по `ALLURE_FEEDBACK_SERVER_URL`. Проверь, что он поднят
(`python alla-skill/scripts/serve.py`) и URL правильный.

## `HTTP 501 Skill pipeline requires ALLURE_KB_POSTGRES_DSN`

Сервер отвечает, но у него самого не настроен DSN базы знаний. Это
конфигурация **сервера**, не клиента: задай `ALLURE_KB_POSTGRES_DSN` в
окружении `alla-server` и убедись, что миграции применены в его БД:

```bash
# на машине/окружении сервера
psql "$ALLURE_KB_POSTGRES_DSN" -f sql/kb_schema.sql
psql "$ALLURE_KB_POSTGRES_DSN" -f sql/kb_feedback_schema.sql
psql "$ALLURE_KB_POSTGRES_DSN" -f sql/merge_rules_schema.sql
psql "$ALLURE_KB_POSTGRES_DSN" -f alla-skill/sql/skill_run_schema.sql
```

## `404 launch not found` / пустой ответ

Проверь:

* Правильный `--launch-id` или `--launch-name --project-id`.
* Токен имеет доступ к указанному проекту.
* Через UI launch виден.

## `comments_failed > 0` в push

Скрипт логирует конкретные ошибки в stderr (для каждого `test_case_id`).
Частые причины:

* Токен без права записи комментариев — нужен другой scope.
* Истёк JWT, обновляется автоматически, но при первом запросе в новой
  сессии может быть лаг.
* Allure TestOps временно вернул 5xx — повтори push.

## `push_disabled` envelope

Push выключен по умолчанию. Чтобы запустить реальный push, передай
`--confirm` (или включи `ALLURE_PUSH_COMMENTS=true` в `.env`, что не
рекомендуется). Для предпросмотра — `--dry-run`.

## `Невалидный agent payload`

`submit_analysis.py` (через `agent_analysis_adapter`) проверяет
схему. Частые проблемы:

* Не передан `schema_version`.
* `category` не в `{test, service, env, data, unanalyzed}`.
* `analysis_text` пустой.
* `analysis_text` > 8000 символов (агент пишет слишком много).

Поправь ответ агента под `references/analysis_schema.md` и повтори.

## HTML-отчёт не открывается / пустой

* Проверь `html_size_bytes` в response `generate_report.py` — должен
  быть >50KB.
* Открой в браузере, посмотри Console: если CSS/JS не подгружается —
  возможно, какой-то ассет не закодирован в base64.
* Сравни с server-side путём (`alla 12345`) — если там тоже сломано —
  проблема в `alla.report.html_report`, а не в скилле.

## Skill_run в статусе `failed`

При любом исключении в pipeline скрипты пишут `error_json` и ставят
`status='failed'`. Чтобы посмотреть:

```sql
SELECT run_id, status, error_json, updated_at
FROM alla.skill_run
WHERE status = 'failed'
ORDER BY updated_at DESC LIMIT 10;
```

Запустить заново — просто вызови `fetch_clusters.py` ещё раз; он
создаст новый `run_id`.

## Расхождение результатов с server-side `alla 12345`

После рефакторинга оба пути используют одни и те же публичные сервисы
(`prompt_builder_service`, `comment_push_service`, `kb_lookup_service`).
Различия должны быть только в том, что:

* server-side использует GigaChat, скилл — агент CLI.
* skill-режим хранит state в `alla.skill_run`.

Если видишь регрессию (одинаковые входы → разные кластеры/KB) — это
бага рефакторинга, нужно открыть отдельный issue.
