# Установка и настройка скилла alla-launch

## Что нужно

- Qwen Code, запущенный в корне проекта автотестов.
- Python 3.11+ для окружения скилла. Сам скрипт запуска работает на любом
  `python3` ≥ 3.8 и перезапускает себя в `.venv` скилла.
- API-токен Allure TestOps и доступ к индексу пакетов pip (PyPI или
  корпоративное зеркало).

## Установка

1. Скопируйте папку `qwen-skill/alla-launch` из репозитория alla в проект
   автотестов: `<project>/.qwen/skills/alla-launch/`.
2. Один раз создайте окружение:

       python3 .qwen/skills/alla-launch/scripts/alla_skill.py setup

   Нужен конкретный интерпретатор — `... setup --python /usr/bin/python3.11`.
3. Скопируйте `.env.example` в `.env` рядом с `SKILL.md` и задайте
   `ALLURE_ENDPOINT` и `ALLURE_TOKEN`. Вместо файла можно задать переменные
   окружения с теми же именами — они важнее файла. Чужие переменные в `.env`
   игнорируются.
4. `.venv/` и `.env` скилла уже исключены его собственным `.gitignore`.
   Папка `alla-reports/` создаётся при первом разборе со своим `.gitignore`
   (`*`), поэтому отчёты не попадают в git.

Скилл вызывается командой `/alla-launch 12345` или фразой «разбери прогон
12345». Чтобы Qwen Code не спрашивал подтверждение на каждый вызов скрипта,
разрешите команду `python3` при первом запросе («Always allow»).

Проверить доступ к TestOps без модели:

    python3 .qwen/skills/alla-launch/scripts/alla_skill.py prepare 12345

## Что получается

```
alla-reports/<launch_id>-<YYYYmmdd-HHMMSS>/
  run.json          данные прогона (служебный файл)
  clusters/NN.md    задание на разбор кластера NN
  analyses/NN.md    разбор кластера, написанный моделью
  summary_task.md   задание на общий анализ
  summary.md        общий анализ прогона
  report.md         итоговый отчёт: краткий текст + детали по кластерам
```

Каждый запуск `prepare` создаёт новую папку. `next` без аргументов работает с
последней (`alla-reports/.last_run`).

## Настройки

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ALLURE_ENDPOINT` | — | URL Allure TestOps (обязательно) |
| `ALLURE_TOKEN` | — | API-токен (обязательно) |
| `ALLURE_SSL_VERIFY` | `true` | Проверка TLS; `false` для корпоративного прокси |
| `ALLURE_REQUEST_TIMEOUT` | `30` | Таймаут HTTP-запроса, сек |
| `ALLURE_PAGE_SIZE` / `ALLURE_MAX_PAGES` | `100` / `50` | Пагинация результатов |
| `ALLURE_DETAIL_CONCURRENCY` | `10` | Параллельные запросы деталей тестов |
| `ALLURE_LOGS_CONCURRENCY` | `5` | Параллельные загрузки вложений |
| `ALLURE_LOGS_MAX_ATTACHMENT_BYTES` | `10485760` | Максимум байт из одного вложения |
| `ALLURE_LOGS_MAX_SNIPPET_CHARS` | `65536` | Максимум символов лога на тест |
| `ALLURE_CLUSTERING_THRESHOLD` | `0.60` | Порог похожести; ниже — крупнее кластеры |
| `ALLURE_LOGS_CLUSTERING_WEIGHT` | `0.15` | Вес лога в кластеризации |
| `ALLURE_CLUSTERING_STEP_STRICT_THRESHOLD` | `0.95` | Жёсткое разделение по шагу теста |
| `ALLURE_LLM_PROMPT_MESSAGE_MAX_CHARS` | `2000` | Лимит сообщения об ошибке в задании |
| `ALLURE_LLM_PROMPT_TRACE_MAX_CHARS` | `400` | Лимит трейса в задании |
| `ALLURE_LLM_PROMPT_LOG_MAX_CHARS` | `8000` | Лимит лога в задании |

Значения по умолчанию совпадают с сервером alla.

## Ограничения первой версии

- Нет базы знаний, merge rules, HTML-отчёта и записи комментариев или ссылок
  в TestOps. Скилл только читает TestOps.
- Прогон задаётся только числовым ID.
- Ошибки и логи прогона передаются модели, с которой работает Qwen Code.
- Код проекта может отличаться от ревизии, на которой шёл прогон.

## Обновление

Папка `scripts/alla_core/` сгенерирована из репозитория alla
(`tools/sync_qwen_skill.py`) — не правьте её вручную. Для обновления
скопируйте папку скилла заново, сохранив `.env` и `.venv`, и выполните
`setup`, чтобы подтянуть зависимости.
