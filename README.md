# alla

`alla` — AI-агент для разбора упавших автотестов из Allure TestOps. По ID
или имени запуска инструмент забирает результаты, исключает retry/hidden и
muted-падения из активного анализа, вытаскивает ошибку и логи, группирует
похожие падения, ищет совпадения в базе знаний, при наличии GigaChat делает
LLM-анализ, генерирует HTML-отчёт и может записать рекомендации обратно в
TestOps.

## Что умеет сейчас

- CLI `alla` для анализа запуска, JSON/text-вывода и локального HTML-отчёта.
- FastAPI-сервер `alla-server` с REST API, HTML-отчётами, feedback API,
  dashboard и MCP endpoint `/mcp`.
- Поиск запуска по точному имени через TestOps API.
- Очистка старых комментариев `[alla]` в TestOps.
- PostgreSQL-база знаний: глобальные записи, проектные записи и общая
  видимость между проектами через `alla.project_group`.
- Интерактивный HTML-отчёт: создание и обновление записей базы знаний,
  like/dislike по совпадениям, exact feedback memory, ручное объединение
  кластеров, перезапуск анализа без записи комментариев в TestOps.
- Хранение HTML-отчётов на диске или в PostgreSQL, с публичными ссылками
  через `ALLURE_SERVER_EXTERNAL_URL`.

## Pipeline анализа

1. `TriageService` получает launch metadata и все test results, отбрасывает
   `hidden` retry-результаты, считает статусы и исключает muted failed/broken
   из активного анализа.
2. Для failed/broken результатов извлекается ошибка: execution tree,
   `statusDetails`, затем fallback `GET /api/testresult/{id}`.
3. `LogExtractionService` скачивает processable attachments, извлекает
   `[ERROR]`-блоки из текста, HTTP 4xx/5xx контекст из JSON/XML/text и
   correlation ids.
4. `ClusteringService` строит message/trace/log/step каналы, нормализует
   волатильные значения и группирует падения через TF-IDF cosine similarity
   + agglomerative clustering.
5. Сохранённые merge rules применяются после первичной кластеризации.
6. Если задан `ALLURE_KB_POSTGRES_DSN`, выполняется поиск по базе знаний:
   Tier 1 exact substring, Tier 2 line match, Tier 3 TF-IDF fallback,
   step-aware фильтрация и exact feedback memory.
7. Если настроен GigaChat, LLM получает данные кластера и совпадения базы
   знаний, возвращает анализ по кластерам и summary запуска.
8. При `ALLURE_PUSH_TO_TESTOPS=true` в TestOps пишутся LLM-комментарии, если
   LLM успешно отработал; иначе используется fallback KB push.
9. Формируется self-contained HTML-отчёт и, если есть публичный URL,
   ссылка прикрепляется к launch links в TestOps.

## Быстрый старт

```bash
python -m pip install -e .
cp .env.example .env
```

Минимальная конфигурация:

```env
ALLURE_ENDPOINT=https://allure.example.com
ALLURE_TOKEN=...
```

`ALLURE_PROJECT_ID` не обязателен для анализа по числовому launch ID, но
полезен для поиска запуска по имени.

## CLI

```bash
alla 12345
alla --launch-name "Regression 2026-05-02" --project-id 1
alla 12345 --output-format json
alla 12345 --html-report-file alla-report.html --report-url https://ci.example/alla-report.html
alla delete 12345 --dry-run
alla delete 12345
alla backfill-report-projects --dry-run --limit 100
```

Обычный анализ через CLI всегда сохраняет HTML-файл. Если
`--html-report-file` не указан, используется `alla_report_<launch_id>.html`.

## Сервер

```bash
alla-server
```

Основные endpoints:

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/health` | health check, версия и флаг `mcp=true` |
| `GET` | `/api/v1/launch/resolve?name=...&project_id=...` | найти launch ID по точному имени |
| `POST` | `/api/v1/analyze/{launch_id}` | JSON-результат анализа |
| `POST` | `/api/v1/analyze/{launch_id}/html` | HTML-отчёт, header `X-Report-URL` при наличии публичной ссылки |
| `GET` | `/reports/{filename}` | отдать сохранённый HTML-отчёт |
| `DELETE` | `/api/v1/comments/{launch_id}?dry_run=true` | удалить комментарии `[alla]` у failed/broken тестов |
| `POST` | `/api/v1/kb/entries` | создать запись базы знаний из HTML-отчёта |
| `PUT` | `/api/v1/kb/entries/{entry_id}` | обновить запись базы знаний |
| `POST` | `/api/v1/kb/feedback` | записать like/dislike |
| `POST` | `/api/v1/kb/feedback/resolve` | получить сохранённые голоса для exact signature |
| `POST` | `/api/v1/merge-rules` | сохранить правила объединения кластеров |
| `GET` | `/api/v1/merge-rules?project_id=...` | список merge rules проекта |
| `DELETE` | `/api/v1/merge-rules/{rule_id}` | удалить merge rule |
| `GET` | `/api/v1/dashboard/stats?days=30` | агрегаты dashboard |
| `GET` | `/dashboard` | self-contained dashboard UI |
| `ANY` | `/mcp` | MCP streamable HTTP tools `analyze_launch`, `analyze_launch_html` |

Query parameter `push_to_testops=false` у analyze endpoints запрещает запись
комментариев/ссылок в TestOps для конкретного запроса.

## База знаний

Текущий backend базы знаний — PostgreSQL. YAML backend и переменные
`ALLURE_KB_ENABLED`, `ALLURE_KB_BACKEND`, `ALLURE_KB_PATH`,
`ALLURE_KB_PUSH_ENABLED` больше не используются.

Создать схему:

```bash
python sql/setup_kb.py
python sql/setup_kb.py --with-starter-pack
```

Можно применять SQL-файлы вручную:

```bash
psql "$ALLURE_KB_POSTGRES_DSN" -f sql/kb_schema.sql
psql "$ALLURE_KB_POSTGRES_DSN" -f sql/kb_feedback_schema.sql
psql "$ALLURE_KB_POSTGRES_DSN" -f sql/merge_rules_schema.sql
psql "$ALLURE_KB_POSTGRES_DSN" -f sql/kb_seed.sql
```

`alla.report` создаётся автоматически, когда включён
`ALLURE_REPORTS_POSTGRES=true`.

## Agent materials

- `skills/alla/SKILL.md` — инструкция для агентов, которые вызывают running
  `alla-server` через MCP.
- `skill/alla-analysis/SKILL.md` — read-only REST workflow через helper
  `skill/alla-analysis/scripts/run_alla_analysis.py`.
- `docs/USER_GUIDE.md` — пользовательская инструкция по HTML-отчёту и
  наполнению базы знаний.
- `CLAUDE.md` — инженерная карта проекта для будущих агентов и разработчиков.
