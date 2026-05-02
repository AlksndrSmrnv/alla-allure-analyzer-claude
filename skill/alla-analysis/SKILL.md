---
name: alla-analysis
description: Используй этот skill, когда пользователь просит проанализировать launch Allure TestOps/Alla, запустить анализ Alla, посмотреть кластеры падений, найти главные кластеры, кратко разобрать отчёт Alla или расследовать запросы вроде «проанализируй прогон 12345», «разбери launch», «найди главные кластеры падений». Skill работает только через REST API запущенного alla-server.
---

# Анализ Alla

## Обзор

Используй этот skill для read-only анализа через REST API запущенного
`alla-server`. Вспомогательный скрипт вызывает сервер, нормализует большой JSON
в компактный формат для агента и всегда передаёт `push_to_testops=false`.

Этот workflow нужен для расследований и сводок. Он не должен писать комментарии
или ссылки на отчёты обратно в TestOps.

## Быстрый старт

Анализ по числовому launch ID:

```bash
python skill/alla-analysis/scripts/run_alla_analysis.py --launch-id 12345
```

Сначала найти точное имя launch, затем проанализировать:

```bash
python skill/alla-analysis/scripts/run_alla_analysis.py --launch-name "Launch name" --project-id 1
```

Создавай HTML-файл только когда пользователь явно просит отчёт/ссылку:

```bash
python skill/alla-analysis/scripts/run_alla_analysis.py --launch-id 12345 --html
```

С `--html` helper вызывает `/api/v1/analyze/{launch_id}/html`, записывает
вернувшийся HTML во временный файл и добавляет `html_report.html_path`. Если
сервер вернул `X-Report-URL`, он будет доступен как `html_report.report_url`.

## URL сервера

Адрес сервера задан внутри
`skill/alla-analysis/scripts/run_alla_analysis.py` в `ALLA_SERVER_URL`.

Скрипт намеренно не читает переменные окружения. Если значение всё ещё
содержит `TODO-ALLA-SERVER`, остановись и попроси пользователя указать URL
сервера в скрипте.

## Что делает helper

1. Вызывает `GET /health` и кладёт ответ в `server.health`.
2. При необходимости резолвит имя launch через
   `GET /api/v1/launch/resolve?name=...&project_id=...`.
3. Вызывает `POST /api/v1/analyze/{launch_id}?push_to_testops=false`.
4. Опционально вызывает `POST /api/v1/analyze/{launch_id}/html?push_to_testops=false`.
5. Возвращает компактный JSON со счётчиками, сводками кластеров, лучшими
   совпадениями из базы знаний, контекстом representative test,
   LLM verdict/error, launch summary и duration.

## Поля вывода

- `counters.active_failures`: failed + broken минус muted failures.
- `clustering.clusters`: отсортированы по убыванию `size`.
- Для каждого кластера: `label`, `size`, `step_path`, `representative_message`,
  `correlation_hint`, `trace_snippet`, `representative_test.log_snippet`.
- `kb_matches`: лучшие совпадения с title, category, score, origin и feedback vote.
- `llm.llm_launch_summary.summary_text`: launch-level summary, если есть.
- `llm_verdict` / `llm_error` по каждому кластеру: исходный материал, а не
  финальный ответ.

## Workflow анализа

- Перед финальным ответом прочитай `references/cluster_interpretation.md`.
- Начни со счётчиков и объясни, сконцентрированы ли active failures в
  нескольких кластерах или размазаны по запуску.
- Ранжируй кластеры по влиянию: сначала `size`, затем отсутствующие/слабые
  совпадения в базе знаний, затем LLM errors/skips.
- Считай `origin=feedback_exact` сильнее обычной текстовой похожести, если
  текущий step/message context ему не противоречит.
- Текст LLM от Alla используй как исходный материал. Добавляй свою
  приоритизацию, подозрения на merge/split и конкретные шаги отладки.

## Форма финального ответа

Отвечай краткой сводкой расследования на русском, если пользователь не попросил
иначе:

- краткий итог прогона;
- главные проблемные кластеры по влиянию;
- вероятные общие root causes по категориям: test, service, env, data;
- подозрительные кандидаты на split/merge;
- конкретные следующие действия для отладки или исправления;
- report URL или локальный HTML path, если отчёт был создан.

Если helper вернул payload с ошибкой, объясни сбой и укажи соответствующие HTTP
status/detail.
