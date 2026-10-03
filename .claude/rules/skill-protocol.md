---
paths:
  - "qwen-skill/alla-launch/SKILL.md"
  - "qwen-skill/alla-launch/references/**"
  - "qwen-skill/alla-launch/scripts/alla_skill.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/cli.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/workspace.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/batch_task.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/agent_rules.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/errors.py"
  - "qwen-skill/alla-launch/tests/test_skill_protocol.py"
  - "qwen-skill/alla-launch/tests/test_skill_parallel.py"
  - "qwen-skill/alla-launch/tests/test_skill_flow.py"
  - "qwen-skill/alla-launch/tests/test_skill_docs.py"
---

# Протокол скилла: команды, статусы, пакетный разбор, правила исполнителя

## Точка входа и установка

- `scripts/alla_skill.py` (Python 3.8+) перезапускает себя в `.venv` скилла.
- `setup [--python P] [-- pip-аргументы]` создаёт venv; после успешного pip install пишет
  `.venv/.alla-setup-complete` (хэш `requirements.txt`) и создаёт `.env` из образца. Без
  маркера все команды отвечают `STATUS: setup_required`.

## Команды

`prepare <id|URL> [--fresh]`, `next [run_dir | --run DIR] [--workers N | --serial]`,
`verify N [N…] --run DIR`, `skip N --run DIR`, `check`, `clean`,
`apply N --run DIR [--yes --diff ХЭШ] [--repeat]`, `revert N --run DIR`,
`remember N --run DIR [--entry ID] [--from-analysis]`, `reject N <id> --run DIR`.

- Где в синтаксисе есть `--run DIR`, он обязателен: `.last_run` после нового `prepare`
  указывает на другой прогон.
- Разбор модели сохраняется в базу знаний только с явным `--from-analysis`; он выбирает
  разбор, даже если есть `feedback/NN.md`.

## Статусы и коды возврата

- Первая строка вывода — `STATUS: analyze|analyze_batch|fix|propose|summary|done|diff|
  applied|reverted|saved|ok|ready|setup_required|error`.
- Ошибка аргументов и любое необработанное исключение тоже дают `STATUS: error`.
- Код возврата 0 у всех статусов, кроме `error`: статус с инструкцией — не авария.

## `prepare` и папка разбора

- Продолжает свежий (<24 ч) незавершённый разбор того же прогона без обращения к TestOps;
  `--fresh` — начать заново.
- Папку с пустым или оборванным `run.json` (`ws.read_run`) пропускает: берёт другую свежую
  либо начинает новую. Остальные команды на такой папке дают `error` с готовой командой
  `prepare <id> --fresh`.
- Пустой `state.json` не роняет `next`: счётчики начинаются заново.
- `write_atomic`/`write_atomic_bytes` (`workspace`) делают `fsync` перед `os.replace`; через
  них же `apply`/`revert` пишут исходник, `NN.orig` и отметку применения.
- Стадии выгрузки идут в stderr. Пустой прогон — `error`. Ошибка одного кластера деградирует
  его до «неизвестно» с предупреждением, а не роняет `prepare`.
- `pipeline` (numpy/scipy/sklearn, ~0,5 с) импортируется только внутри `cmd_prepare`:
  остальные команды вызываются десятки раз за разбор. Охраняет
  `test_cli_does_not_load_clustering_libraries`.

## Пакетный разбор больших прогонов (`cli.next_step`, `batch_task`)

- Когда кластеров без разбора ≥ `PARALLEL_MIN_PENDING` (10) и рабочих > 1, `next` отвечает
  `STATUS: analyze_batch` вместо `analyze`.
- Неразобранные кластеры (в порядке номеров, не `auto`) режутся по `BATCH_SIZE` (6) в пакеты;
  за одну волну — не больше `DEFAULT_WORKERS` (4) пакетов.
- `batches/N.md` самодостаточен (правила; кластер → задание → файл разбора; команда проверки;
  шаблон ответа — в самих `clusters/NN.md`) и пишется заново каждой волной.
- Состав волны считается по диску, но выданные кластеры запоминаются в `state.json`
  (`batched`, `wave`): пакетом кластер раздаётся не больше одного раза. Что волна не
  записала, идёт обычным `analyze` по одному.
- Если волна не записала ни одного разбора, пакетный режим выключается (`workers=1`,
  пояснение в ответе `next`); включить снова — `next --workers N` (сбрасывает `batched`).
- Основной агент раздаёт пакеты субагентам Qwen Code (`agent`, все вызовы в одном сообщении,
  `subagent_type: alla-batch`, `run_in_background: false`), ждёт всех и зовёт `next`.
- `alla-batch` — свой субагент (`agents/alla-batch.md`, узкая роль, инструменты read_file,
  write_file, run_shell_command): встроенный `general-purpose` по своей системной подсказке
  осматривал проект (ls/find, чужие пакеты), и текстом prompt это не лечилось (стенд Qwen,
  P04). `prepare` ставит его в `<project>/.qwen/agents/` (`batch_task.install_batch_agent`);
  Qwen видит агентов со старта сеанса, поэтому инструкция `next` даёт запасной путь — без
  `subagent_type`. Qwen молча пропускает невалидный файл агента: формат охраняет
  `test_batch_agent_file_is_valid_for_qwen_and_narrow`.
- Субагент `next` не вызывает. `verify N [N…] --run DIR` — только читающая проверка (те же
  parse/validate, что в `next`; `state.json`, история и файлы не трогаются, попытки не
  считаются), `STATUS: ok|fix`.
- `state.json` пишет только основной агент. Невалидный или недописанный разбор
  возвращается обычным `fix`/`analyze` по одному (лимит попыток прежний).
- Хвост меньше порога, `fix`, `propose`, `summary` идут по одному.
- `next --workers N` (1–`MAX_WORKERS`=8; `--serial` = 1) сохраняется в `state.json`; это
  запасной путь без субагентов.

## Правила исполнителя и справочники

- Агент ведёт разбор строго по сценарию: не правит файлы скилла, не пишет своих скриптов, о
  неполадках скилла сообщает блоком «Проблемы скилла» (`SKILL.md`,
  `references/problem-report.md`). Защита только текстовая.
- Запреты называют конкретные команды и файлы: абстрактное «из shell — только команды
  скилла» субагенты на быстрой модели не соблюдали и начинали с ls/find (стенд Qwen, P04).
  Поведение после правки правил проверяй сценарием P04 стенда.
- Один и тот же текст правил — `alla_skill_lib/agent_rules.py` (`EXECUTOR_RULES`): он
  подставляется в `clusters/NN.md`, `batches/N.md`, `summary_task.md` и повторён в
  `SKILL.md`.
- Форматы файлов, которые пишет агент, — в `references/`: `analysis-format.md`
  (`analyses/NN.md`), `proposal-format.md` (`proposals/NN.md`), `summary-format.md`
  (`summary.md`), `feedback.md` (`feedback/NN.md`); `protocol.md` — статусы, лимиты попыток и
  разграничение файлов.
- Шаблоны из констант кода (`EXPECTED_FORMAT`, `PROPOSAL_FORMAT`, `FEEDBACK_FORMAT`) вставлены в
  справочники дословно; примеры с пометкой `<!-- example: … -->` прогоняет через настоящие
  парсеры `tests/test_skill_docs.py`. Меняя формат или текст ошибки в коде, правьте
  справочник — тест покажет расхождение.
- Ответ `fix` на предпоследней попытке предупреждает, что третья версия разбора будет
  принята с пометкой «формат нарушен».
- `errors.py` — подсказки по сбоям выгрузки: токен, TLS, таймаут, лимит страниц, 404.
