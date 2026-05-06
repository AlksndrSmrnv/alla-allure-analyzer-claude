# Troubleshooting

Типичные ошибки и как их быстро устранить.

## `ConfigurationError: ALLURE_KB_POSTGRES_DSN обязателен ...`

PostgreSQL обязателен для skill-режима — все артефакты pipeline
(`alla.skill_run`, `alla.report`, KB, feedback) хранятся в БД.

Проверь:

1. Файл `alla-skill/.env` существует.
2. В нём задано `ALLURE_KB_POSTGRES_DSN=postgresql://user:pass@host:5432/db`.
3. БД доступна:
   ```bash
   psql "$ALLURE_KB_POSTGRES_DSN" -c "SELECT 1"
   ```

## `ConfigurationError: .env не найден ...`

Скрипт ищет `.env` по абсолютному пути относительно директории
скрипта (а не CWD). Скопируй `.env.example` → `.env` в корне
`alla-skill/`:

```bash
cp alla-skill/.env.example alla-skill/.env
```

## `psycopg.OperationalError`

Не удалось подключиться к PostgreSQL. Проверь:

* Хост/порт доступны (`telnet host 5432`).
* Учётка имеет права на схему `alla`.
* Если нужен SSL — добавь `?sslmode=require` к DSN.
* Применены миграции:
  ```bash
  psql "$DSN" -f sql/kb_schema.sql
  psql "$DSN" -f sql/kb_feedback_schema.sql
  psql "$DSN" -f sql/merge_rules_schema.sql
  psql "$DSN" -f alla-skill/sql/skill_run_schema.sql
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
