# alla-skill

Полноценный скилл для агентных CLI (Claude Code, qwen CLI, codex CLI):
получение упавших тестов из Allure TestOps, кластеризация, поиск в базе
знаний, агентский анализ, HTML-отчёт и опциональный постинг рекомендаций
обратно в TestOps.

LLM-вызовов в скрипте нет — анализ выполняет сам агент CLI на основе
готовых промптов, которые выдают скрипты. Бизнес-логика переиспользует
публичные сервисы пакета `alla` (`src/alla/services/*`), один источник
истины с server-side путём.

## Установка

```bash
cd alla-skill
cp .env.example .env
# заполни ALLURE_ENDPOINT, ALLURE_TOKEN, ALLURE_PROJECT_ID,
#         ALLURE_KB_POSTGRES_DSN

pip install -e ..        # установит пакет alla из корня
psql "$ALLURE_KB_POSTGRES_DSN" -f sql/skill_run_schema.sql
# (один раз) применить миграции из ../sql/:
#   kb_schema.sql, kb_feedback_schema.sql, merge_rules_schema.sql
```

## Подключение из агента

### Claude Code

Скилл лежит в корне репо. SKILL.md автоматически распознаётся как
context-файл при запросах вроде «проанализируй прогон 12345».

### qwen CLI

Зарегистрируй YAML из `references/qwen_subagents.md` в
`~/.qwen/agents/alla-cluster-analyzer.yaml` и используй
`agent run alla-cluster-analyzer ...` для делегирования анализа кластеров.

### codex CLI

Используй inline-цикл — readme в `references/qwen_subagents.md`
содержит fallback-инструкцию.

## Точки входа

* `scripts/resolve_launch.py` — резолв name → id.
* `scripts/fetch_clusters.py` — pipeline → строка в `alla.skill_run`.
* `scripts/get_cluster_context.py` — промпт + контекст для одного кластера.
* `scripts/get_summary_context.py` — промпт + контекст для launch summary.
* `scripts/submit_analysis.py` — записать агентский анализ в БД.
* `scripts/generate_report.py` — HTML-отчёт.
* `scripts/push_to_testops.py` — постинг (требует `--confirm`).
* `scripts/delete_comments.py` — очистка `[alla]`-комментариев.
* `scripts/manage_kb.py` — CRUD KB-записей.
* `scripts/record_feedback.py` — like/dislike feedback.

## Структура

```
alla-skill/
├── SKILL.md                 # Описание, workflow, frontmatter
├── README.md                # Этот файл
├── .env.example
├── pyproject.toml           # Метаданные (deps приходят из ../pyproject.toml)
├── sql/skill_run_schema.sql
├── scripts/
│   ├── _common.py
│   ├── resolve_launch.py
│   ├── fetch_clusters.py
│   ├── get_cluster_context.py
│   ├── get_summary_context.py
│   ├── submit_analysis.py
│   ├── generate_report.py
│   ├── push_to_testops.py
│   ├── delete_comments.py
│   ├── manage_kb.py
│   └── record_feedback.py
├── references/
│   ├── workflow.md
│   ├── delegation_strategy.md
│   ├── cluster_analysis_guide.md
│   ├── launch_summary_guide.md
│   ├── analysis_schema.md
│   ├── qwen_subagents.md
│   └── troubleshooting.md
└── prompts/                 # Read-only копии промптов
    ├── cluster_analysis.md
    └── launch_summary.md
```

## Архитектурные принципы

1. **Скрипты — тонкие orchestration-entrypoints.** Бизнес-логика
   импортируется из `alla.services.*` и `alla.knowledge.*`. Никакого
   дублирования.
2. **Один источник истины для промптов.** GigaChat-путь и скилл-путь
   используют `alla.services.prompt_builder_service`.
3. **Состояние — в `alla.skill_run`.** `run_id` — primary handle. Никаких
   временных файлов между шагами.
4. **Push выключен по умолчанию.** Чтобы избежать случайной записи в
   TestOps, нужно явно передать `--confirm`.
5. **`.env` грузится по абсолютному пути относительно директории
   скрипта**, а не CWD.
