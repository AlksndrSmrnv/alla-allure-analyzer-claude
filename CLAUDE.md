# CLAUDE.md — alla

## Что это

`alla` — скилл Qwen Code для триажа упавших автотестов из Allure TestOps. По launch ID или
ссылке на запуск он получает результаты, извлекает ошибки и логи, кластеризует активные
failed/broken падения; анализ пишет модель Qwen Code по заданиям скилла, Python готовит
данные. Результат — краткий разбор в терминале, `report.md` и предложения правок
автотестов. Скилл только читает TestOps. База знаний — проектная (`alla-kb/` в репозитории
автотестов), наполняется обратной связью пользователя.

## Состояние репозитория

- В `main` только скилл: `qwen-skill/alla-launch/`.
- Серверный инструмент (CLI `alla`, FastAPI `alla-server`, MCP, PostgreSQL, GigaChat,
  HTML-отчёт, dashboard, Docker/Jenkins) удалён и есть только в истории git: последний
  коммит с ним — `e100909` (`git show e100909:src/alla/<файл>`). Серверный код и пакет
  `alla` не возвращать без явного решения пользователя.
- Первая версия скилла — ветка `codex/experiment-qwen-skill-v1`.

## Где что лежит

| Путь (от `qwen-skill/alla-launch/`) | Что там |
|---|---|
| `SKILL.md`, `references/` | сценарий и правила для модели, форматы её файлов, протокол, установка |
| `agents/alla-batch.md` | свой субагент Qwen для пакетов кластеров; `prepare` ставит его в `.qwen/agents/` проекта |
| `scripts/alla_skill.py` | точка входа (Python 3.8+), перезапускает себя в `.venv` скилла |
| `scripts/alla_skill_lib/` | логика скилла: `cli`, `workspace`, `pipeline`, `batch_task`, `agent_rules`, `cluster_task`, `sources`, `analysis_format`, `code_hints`, `report`, `known_issues`, `kb`, `modules`, `feedback`, `history`, `proposals`, `errors` |
| `scripts/alla_core/` | ядро: клиент TestOps, триаж, логи, кластеризация, блок «Данные», сигнатура |
| `tests/` | `test_skill_*` — скилл на фейковом TestOps, `test_core_*` — ядро; `qwen_stand.py` + `fake_testops_server.py` — стенд настоящего Qwen |
| `tests/eval/` | эталон точности: кассеты прогонов (`record.py` пишет у команды), размеченный корпус dev/holdout, метрики склеек, дробления, скрытых групп и потери доказательств (`run_eval.py`), см. `README.md` там же |

## Команды

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r qwen-skill/alla-launch/requirements-dev.txt
.venv/bin/python -m pytest                      # из корня, несколько секунд
.venv/bin/ruff check qwen-skill/alla-launch
.venv/bin/mypy qwen-skill/alla-launch/scripts   # strict; пока не чистый — новых ошибок не добавлять
.venv/bin/python qwen-skill/alla-launch/tests/qwen_stand.py run --case A01 --output /tmp/alla-stand-001
.venv/bin/python qwen-skill/alla-launch/tests/eval/run_eval.py --details   # эталон, офлайн, ~2 с
```

## Проверка на настоящем Qwen Code

Скилл можно и нужно проверять в целевом runtime: Qwen Code CLI установлен (`qwen`, модель
`qwen/qwen3.8-flash` через OpenRouter, настройки в `~/.qwen/settings.json`), а стенд
`tests/qwen_stand.py` гоняет его на синтетическом TestOps (последняя команда выше). Pytest
проверяет протокол; выполняет ли модель инструкции — видно только на стенде. Запускай его
после правки `SKILL.md`, справочников, текстов заданий и правил исполнителя.

- Сценарии — `qwen_stand.py list`; короткие (A03, A04, P01, E03, E07) — до минуты, A01/E06/E08/E09 — 2–3
  минуты, P04 (40 кластеров, субагенты) — 8–10 минут и 3–4 млн токенов: только когда
  меняются пакеты или правила субагентов.
- Диагностика — один запуск с `--api-log` (что модель на самом деле получила), а не серия
  повторов на догадках. Повторы (`--repeat`) — чтобы оценить разброс уже исправленного.
- `inconclusive` с пунктами «оценить» — автоматические проверки прошли, оси вроде Evidence
  оцени сам по `alla-reports/` и trace. Проверки стенда без модели — `recheck`.
- Подробности, границы песочницы и найденные особенности Qwen — `docs/skill-quality.md`
  («Стенд Qwen») и `docs/harness-context.md`.

Корневой `pyproject.toml` содержит только конфиг ruff/mypy/pyright/pytest, пакета нет.
pyright настроен для навигации (`typeCheckingMode = "off"`), типы проверяет mypy.

## Общие правила

- Python ядра и скилла — `>=3.11` (системный 3.9 падает на аннотациях типов); только
  `alla_skill.py` совместим с 3.8+.
- Текст для пользователя — по-русски и простым языком; «база знаний», не «KB»; в отчёте для
  человека — «проблема», не «кластер».
- Код, справочники и правила меняются в одном коммите:
  - формат файла модели или текст ошибки валидатора → `references/*.md`;
    `tests/test_skill_docs.py` прогоняет примеры справочников через настоящие парсеры;
  - наличие контрактных директив в заданиях проверяет `tests/test_skill_prompts.py`,
    размер краткого разбора — `tests/test_skill_report.py`; поведение модели оценивается
    отдельно через `.agents/skills/skill-evaluation/`;
  - изменённое поведение → файл правил своей области в `.claude/rules/` (см. ниже).
  - изменение извлечения логов, данных заданий или кластеризации → базовая линия эталона
    (`tests/eval/run_eval.py --write-baseline`), изменение цифр — в сообщении коммита;
    базовая линия holdout — отдельным коммитом. Старые папки разборов должны открываться —
    `tests/test_legacy_run.py` на фикстуре `tests/fixtures/legacy_run_v1/`.
- Тесты — только `qwen-skill/alla-launch/tests/` (единственный `testpaths`). Фикстуры — в
  `skill_fixtures.py`, фабрики моделей ядра — в `skill_factories.py`, фейковый TestOps на
  `httpx.MockTransport` — в `skill_fake_testops.py`; `conftest.py` нет, чтобы папка скилла
  оставалась самодостаточной. `skill_fixtures` импортировать раньше `alla_core`: он
  добавляет `scripts/` в `sys.path`. libmagic в окружении нет — автоматически включается
  фикстура `without_libmagic`. Зависимости тестов — `requirements-dev.txt`.
- Сообщения коммитов — по-английски, в стиле `fix(scope): …`.

## Подробные правила — `.claude/rules/`

Контракты модулей вынесены в файлы правил. В Claude Code файл подгружается сам, когда
открываешь файл его области; в Codex прочитай нужные правила явно (см. `AGENTS.md`).
Если задача задевает область, файлы которой ещё не открыты (например, правка
`cli.py` ради `apply`), прочитай нужный файл правил сам. «Обновить `CLAUDE.md`» в планах и
задачах значит обновить файл правил этой области, а этот файл — если меняется общее.

| Файл | О чём | Подгружается при работе с |
|---|---|---|
| `skill-protocol.md` | команды, статусы, `prepare`, пакетный разбор, правила исполнителя, справочники | `SKILL.md`, `references/`, `alla_skill.py`, `cli.py`, `workspace.py`, `batch_task.py`, `agent_rules.py`, `errors.py` |
| `skill-analysis.md` | задание кластера, источники, разбор модели, отчёт, краткий разбор, сводка, известные проблемы | `cluster_task.py`, `sources.py`, `analysis_format.py`, `code_hints.py`, `report.py`, `known_issues.py`, `prompt_builder_service.py`, `log_focus.py` |
| `skill-memory.md` | база знаний, сигнатура, модули, обратная связь, история | `kb.py`, `modules.py`, `feedback.py`, `history.py`, `alla_core/knowledge/` |
| `skill-proposals.md` | предложения правок, `apply`/`revert`, состояние применения | `proposals.py` |
| `core.md` | ядро: сбор данных, отбор логов, кластеризация, клиент TestOps, настройки `ALLURE_*` | `alla_core/`, `pipeline.py` |

## Инструменты Claude Code

- Навигация по Python — LSP (pyright): определения и ссылки до переименования или смены
  сигнатуры.
- Документация httpx, pydantic, pytest, Qwen Code — Context7, затем локальная `--help`.
- Инструкции скилла (`SKILL.md`, `references/`, тексты заданий): формулировки — скилл
  `skill-creator`; после существенной правки — агент `qwen-executor-review` (читает скилл
  с чистым контекстом, без этого файла); поведение модели — скилл `skill-evaluation`.
- Хуки в `.claude/settings.local.json`: ruff после правки `.py`; тесты справочников и
  заданий после правки `SKILL.md`, `references/`, `alla_skill_lib/`; перед `git commit` —
  ruff, pytest и mypy не больше `.claude/hooks/mypy_baseline` ошибок (уменьшать при чистке).
