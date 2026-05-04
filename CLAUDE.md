# CLAUDE.md — alla

## Что это

`alla` — AI-агент триажа упавших автотестов из Allure TestOps. По launch ID
или точному имени запуска он получает результаты, извлекает ошибки и логи,
кластеризует активные failed/broken падения, ищет совпадения в PostgreSQL базе
знаний, запускает GigaChat-анализ при наличии конфигурации, генерирует
self-contained HTML-отчёт и может записать рекомендации/ссылку обратно в
TestOps.

## Текущий pipeline

`TriageService`
→ `LogExtractionService`
→ `ClusteringService`
→ merge rules
→ база знаний + exact feedback memory
→ `LLMService`
→ LLM push или fallback KB push
→ HTML/report link

Важные детали:

- `hidden` results исключаются из статистики и анализа как retry/non-final.
- muted failed/broken считаются отдельно и не попадают в активные кластеры.
- Ошибка извлекается из execution tree, затем из `statusDetails`, затем через
  fallback `GET /api/testresult/{id}`.
- Логи берутся из processable attachments: text, JSON, XML, NDJSON и unknown
  text-like. Binary attachments пропускаются.
- KB push выполняется только как fallback, если LLM не дал успешного результата.

## Слои

| Пакет | Назначение |
|---|---|
| `alla/cli.py` | CLI: анализ, удаление комментариев, backfill report.project_id |
| `alla/server.py` | FastAPI REST API, HTML reports, feedback API, merge rules, dashboard, MCP mount |
| `alla/mcp_app.py` | MCP streamable HTTP tools `analyze_launch`, `analyze_launch_html` |
| `alla/app_support.py` | Общие helpers CLI/HTTP: settings, JSON response, HTML, report persistence/link |
| `alla/orchestrator.py` | Общий pipeline CLI/сервер/MCP |
| `alla/services/` | Триаж, логи, кластеризация, LLM, push, delete comments, merge |
| `alla/clients/` | Allure TestOps, auth, GigaChat, Protocol interfaces |
| `alla/knowledge/` | PostgreSQL база знаний, matcher, feedback memory, merge rules models/store |
| `alla/report/` | HTML-отчёт и PostgreSQL report store |
| `alla/dashboard/` | Dashboard HTML, stats store, backfill |
| `alla/models/` | Pydantic/domain models |
| `alla/config.py` | `Settings(BaseSettings)`, env prefix `ALLURE_` |

## CLI

```bash
alla 12345
alla --launch-name "Regression 2026-05-02" --project-id 1
alla 12345 --output-format json
alla 12345 --log-level DEBUG --page-size 200
alla 12345 --html-report-file alla-report.html --report-url https://ci/alla-report.html
alla delete 12345 --dry-run
alla delete 12345
alla backfill-report-projects --dry-run --limit 100 --concurrency 5
```

Обычный `alla <launch_id>` всегда сохраняет HTML. Если `--html-report-file`
не указан, файл называется `alla_report_<launch_id>.html`.

## HTTP API

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/health` | `{"status":"ok","version":...,"mcp":true}` |
| `GET` | `/api/v1/launch/resolve?name=&project_id=` | Точное имя запуска → `launch_id` |
| `POST` | `/api/v1/analyze/{launch_id}` | Pipeline анализа → JSON |
| `POST` | `/api/v1/analyze/{launch_id}/html` | Pipeline анализа → HTML; header `X-Report-URL` если ссылка есть |
| `GET` | `/reports/{filename}` | Отдать сохранённый HTML из PostgreSQL или `ALLURE_REPORTS_DIR` |
| `DELETE` | `/api/v1/comments/{launch_id}?dry_run=true` | Удалить комментарии с префиксом `[alla]` |
| `POST` | `/api/v1/kb/entries` | Создать project/global запись базы знаний |
| `PUT` | `/api/v1/kb/entries/{entry_id}` | Обновить запись базы знаний |
| `POST` | `/api/v1/kb/feedback` | UPSERT like/dislike exact feedback memory |
| `POST` | `/api/v1/kb/feedback/resolve` | Вернуть сохранённые голоса для HTML-отчёта |
| `POST` | `/api/v1/merge-rules` | Сохранить пары сигнатур для объединения кластеров |
| `GET` | `/api/v1/merge-rules?project_id=` | Список merge rules проекта |
| `DELETE` | `/api/v1/merge-rules/{rule_id}` | Удалить merge rule |
| `GET` | `/api/v1/dashboard/stats?days=30` | KPI, per-project rollup, series |
| `GET` | `/dashboard` | Self-contained dashboard UI |
| `ANY` | `/mcp` | MCP streamable HTTP transport |
| `GET` | `/docs` | Swagger UI |

`push_to_testops=false` у `/api/v1/analyze/...` переопределяет
`ALLURE_PUSH_TO_TESTOPS` для одного запроса.

## MCP

MCP сервер монтируется в `alla-server` на `/mcp`.

| Tool | Описание |
|---|---|
| `analyze_launch(launch_id, push_to_testops?)` | Компактный JSON: launch, total_failed, clusters, top совпадения базы знаний, LLM verdicts |
| `analyze_launch_html(launch_id, push_to_testops?)` | То же + сохранение HTML в `ALLURE_REPORTS_DIR` и/или PostgreSQL, `report_url`/`hint` |

MCP tools не принимают launch name. Для имени используйте REST
`/api/v1/launch/resolve` или helper `skill/alla-analysis/scripts/run_alla_analysis.py`.

## Конфигурация

Все переменные имеют префикс `ALLURE_`, читаются из env или `.env`.

| Переменная | Обязательная | По умолчанию | Описание |
|---|:---:|---|---|
| `ALLURE_ENDPOINT` | да | — | URL Allure TestOps |
| `ALLURE_TOKEN` | да* | `""` | API token; может прийти из Vault |
| `ALLURE_PROJECT_ID` | нет | `None` | Default project scope для поиска запуска по имени |
| `ALLURE_VAULT_URL` | нет | `""` | Vault Proxy; загружает token, PostgreSQL DSN, GigaChat cert/key |
| `ALLURE_REQUEST_TIMEOUT` | нет | `30` | HTTP timeout к TestOps |
| `ALLURE_PAGE_SIZE` | нет | `100` | Размер страницы TestOps API |
| `ALLURE_MAX_PAGES` | нет | `50` | Защита пагинации |
| `ALLURE_DETAIL_CONCURRENCY` | нет | `10` | Параллелизм details/comments |
| `ALLURE_LOG_LEVEL` | нет | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `ALLURE_SSL_VERIFY` | нет | `true` | Проверка TLS |
| `ALLURE_CLUSTERING_THRESHOLD` | нет | `0.60` | Порог similarity; ниже = агрессивнее объединение |
| `ALLURE_LOGS_CONCURRENCY` | нет | `5` | Параллелизм скачивания attachments |
| `ALLURE_LOGS_CLUSTERING_WEIGHT` | нет | `0.15` | Вес log channel в кластеризации |
| `ALLURE_KB_POSTGRES_DSN` | нет | `""` | Включает PostgreSQL базу знаний, feedback, merge rules, dashboard |
| `ALLURE_KB_MIN_SCORE` | нет | `0.15` | Минимальный score совпадения базы знаний |
| `ALLURE_KB_MAX_RESULTS` | нет | `5` | Максимум совпадений на кластер |
| `ALLURE_FEEDBACK_SERVER_URL` | нет | `""` | Base URL `alla-server` для интерактивных кнопок HTML-отчёта |
| `ALLURE_GIGACHAT_BASE_URL` | нет | `""` | GigaChat API URL |
| `ALLURE_GIGACHAT_CERT_B64` | нет | `""` | mTLS cert PEM в base64 |
| `ALLURE_GIGACHAT_KEY_B64` | нет | `""` | mTLS key PEM в base64 |
| `ALLURE_GIGACHAT_MODEL` | нет | `GigaChat-2-Max` | LLM model |
| `ALLURE_LLM_TIMEOUT` | нет | `120` | Timeout одного LLM-запроса |
| `ALLURE_LLM_CONCURRENCY` | нет | `3` | Параллелизм LLM cluster requests |
| `ALLURE_LLM_REQUEST_DELAY` | нет | `0.5` | Минимальная пауза между LLM-запросами |
| `ALLURE_LLM_MAX_RETRIES` | нет | `3` | Retry для 429/503/network |
| `ALLURE_LLM_RETRY_BASE_DELAY` | нет | `1.0` | Exponential backoff base |
| `ALLURE_LLM_PROMPT_MESSAGE_MAX_CHARS` | нет | `2000` | Лимит message в prompt |
| `ALLURE_LLM_PROMPT_TRACE_MAX_CHARS` | нет | `400` | Лимит trace в prompt |
| `ALLURE_LLM_PROMPT_LOG_MAX_CHARS` | нет | `8000` | Лимит log в prompt |
| `ALLURE_PUSH_TO_TESTOPS` | нет | `true` | Писать комментарии/ссылки в TestOps |
| `ALLURE_REPORT_URL` | нет | `""` | Статический URL HTML-отчёта, обычно Jenkins artifact |
| `ALLURE_REPORT_LINK_NAME` | нет | `[Alla] HTML-отчёт запуска автотестов` | Название launch link |
| `ALLURE_REPORTS_DIR` | нет | `""` | Сохранять HTML на диск и отдавать через `/reports` |
| `ALLURE_REPORTS_POSTGRES` | нет | `false` | Сохранять HTML в `alla.report`, требует DSN |
| `ALLURE_SERVER_EXTERNAL_URL` | нет | `""` | Публичный URL сервера для report links и rerun button |
| `ALLURE_SERVER_HOST` | нет | `0.0.0.0` | Host uvicorn |
| `ALLURE_SERVER_PORT` | нет | `8090` | Port uvicorn |

`ALLURE_TOKEN` обязателен после `resolve_secrets()`: можно задать напрямую или
через `ALLURE_VAULT_URL`.

Удалённые/неиспользуемые настройки: `ALLURE_KB_ENABLED`,
`ALLURE_KB_BACKEND`, `ALLURE_KB_PATH`, `ALLURE_KB_PUSH_ENABLED`,
`ALLURE_CLUSTERING_ENABLED`.

## База знаний и feedback

Текущий backend базы знаний — только PostgreSQL.

Таблицы:

- `alla.kb_entry`: записи базы знаний; `project_id IS NULL` = global/starter pack.
- `alla.project_group`: проекты с общим `group_id` видят записи друг друга.
- `alla.kb_feedback`: exact feedback memory like/dislike по stable issue signature.
- `alla.merge_rules`: ручные правила объединения кластеров.
- `alla.report`: HTML-отчёты и token usage, создаётся report store автоматически.

Matcher:

- Tier 1: normalized exact substring, score 1.0.
- Tier 2: line match, score 0.7-0.95.
- Tier 3: TF-IDF fallback, capped score 0.5.
- `step_path` у записи фильтрует mismatch и даёт небольшой bonus при exact/compatible path.
- `feedback_exact` like поднимает запись с score 1.0; dislike скрывает запись для той же signature.

## HTML-отчёт

HTML self-contained: inline CSS/JS, без внешних ассетов кроме embedded logo data URI.

Интерактивные элементы появляются только когда есть `ALLURE_KB_POSTGRES_DSN` и
`ALLURE_FEEDBACK_SERVER_URL`:

- создать запись базы знаний для кластера;
- обновить существующую запись;
- like/dislike по совпадению;
- resolve сохранённых голосов при загрузке;
- сохранить merge rules;
- показать starter pack в guided onboarding.

Кнопка `Перезапустить анализ` появляется при `ALLURE_SERVER_EXTERNAL_URL` и
делает `POST /api/v1/analyze/{launch_id}/html?push_to_testops=false`, затем
заменяет текущую страницу новым HTML.

## Design notes

1. API-модели TestOps принимают неизвестные поля (`extra="allow"`) и оба стиля
   имён (`populate_by_name=True`), потому что TestOps payloads плавают.
2. Кластеризация message-first: низкая message similarity обычно блокирует
   merge, но log override может объединить падения с одинаковой явной ошибкой
   в логе.
3. KB query строится из message + log; trace используется только когда лога нет.
   В текущем pipeline trace выключен для KB query (`include_trace=False`), чтобы
   лучше совпадать с примерами, созданными из HTML-формы.
4. LLM prompt строго требует проверять применимость неточных совпадений базы
   знаний; высокий score без Tier 1/feedback_exact не считается доказательством.
5. `PostgresKnowledgeBase` создаётся заново на каждый анализ, чтобы подхватывать
   записи, добавленные из HTML-отчёта, без рестарта сервера.

## Gotchas

- В user-facing тексте писать «база знаний», не «KB».
- `feedback_server_url` отвечает за интерактивность HTML, а
  `server_external_url` — за публичные ссылки `/reports/...` и rerun button.
- `analyze_launch_html` REST прикрепляет report link к TestOps только если есть
  effective report URL и `push_to_testops=true`.
- MCP `analyze_launch_html` сохраняет отчёт и возвращает URL/hint, но не патчит
  launch links напрямую.
- `_request()` в TestOps client должен корректно обрабатывать пустые тела
  PATCH/DELETE; `_request_raw()` нужен для бинарных attachments.
- Для запуска проекта нужен Python `>=3.11`; системный Python 3.9 падает ещё на
  аннотациях типов.
