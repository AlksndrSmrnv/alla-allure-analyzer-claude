---
paths:
  - "qwen-skill/alla-launch/SKILL.md"
  - "qwen-skill/alla-launch/references/**"
  - "qwen-skill/alla-launch/scripts/alla_skill.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/cli.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/agent_rules.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/errors.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/session_rules.py"
  - "qwen-skill/alla-launch/tests/test_skill_docs.py"
  - "qwen-skill/alla-launch/tests/test_skill_protocol.py"
---

# Протокол скилла: команды, статусы, пакетный разбор, правила исполнителя

Подробности — в файлах подобластей: `skill-protocol-setup.md` (установка и слэш-команды), `skill-protocol-workspace.md` (`prepare` и папка разбора), `skill-protocol-batch.md` (пакетный разбор и шаг `relate`), `skill-protocol-review.md` (проверка для пилота).

## Команды

`prepare <id|URL> [--fresh]`, `next [run_dir | --run DIR] [--workers N | --serial]`,
`verify N [N…] --run DIR`, `skip N --run DIR`, `check`, `clean`,
`review [run_dir | --run DIR] [--no-model]`,
`apply N --run DIR [--yes --diff ХЭШ] [--repeat]`, `revert N --run DIR`,
`remember N [N…] --run DIR [--entry ID] [--from-analysis | --from-extension ИМЯ]` (несколько
номеров — только с `--from-analysis`, `skill-memory-feedback.md`), `reject N <id> --run DIR`,
`extensions [list|enable|disable|run|action]` (`skill-extensions.md`; action с --run строит
контекст по папке разбора, без --context/--run и с явным --reports-dir работает после clean),
`rerun [N…] --run DIR [--yes --plan ХЭШ | --wait]` (`skill-rerun.md`),
`update [--source URL] [--ref REF]` (только `alla_skill.py`, `skill-protocol-setup.md`, «Точка входа и установка»).

- Где в синтаксисе есть `--run DIR`, он обязателен: `.last_run` после нового `prepare`
  указывает на другой прогон.
- Разбор модели сохраняется в базу знаний только с явным `--from-analysis`; он выбирает
  разбор, даже если есть `feedback/NN.md`.

- Ответы `applied`, `reverted`, `done` могут заканчиваться блоком «Расширения:» (хуки
  включённых расширений, `skill-extensions.md`); модель выполняет его по `SKILL.md`, раздел 7.

## Статусы и коды возврата

- Первая строка вывода — `STATUS: analyze|analyze_batch|fix|propose|relate|summary|done|diff|
  applied|reverted|saved|rerun|running|rerun_done|ok|ready|reviewed|setup_required|updated|error`.
- Ошибка аргументов и любое необработанное исключение тоже дают `STATUS: error`.
- Код возврата 0 у всех статусов, кроме `error`: статус с инструкцией — не авария.

## Правила исполнителя и справочники

- Агент ведёт разбор строго по сценарию: не правит файлы скилла, не пишет своих скриптов.
  Неполадки скилла основной агент записывает блоком «Проблемы скилла» в
  `<папка разбора>/skill-problems.md` (`RunPaths.skill_problems`, `SKILL.md`,
  `references/problem-report.md`) и пользователю **не выводит** — ни после отчёта, ни при
  остановке (тогда — только сообщение скрипта). Субагент пишет «Проблема скилла: …» в конце
  ответа, основной агент эти строки не переносит. Общая фраза для заданий и ответов —
  `agent_rules.RECORD_PROBLEM`. Защита только текстовая.
- Запреты называют конкретные команды и файлы: абстрактное «из shell — только команды
  скилла» субагенты на быстрой модели не соблюдали и начинали с ls/find (стенд Qwen, P04).
  Поведение после правки правил проверяй сценарием P04 стенда. Поиск файлов (glob, grep)
  запрещён тем же списком: модель искала код проекта сама (E07, E10); на стенде это ловит
  проверка `no_code_search`, а чтение кода не из «Где искать код автотеста» — `listed_code_only`.
  Копия кода приложения расширения `sut-code` (`SUT_DIRNAME` = `.alla-sut/<репо>/trees/<sha>/`)
  — исключение `listed_code_only`: её читает только субагент (`ToolCall.subagent`) и только
  в деревьях ревизий (`sut_read_problem`). Искать по ней `glob`/`grep_search` нельзя
  (`no_code_search`): поиск Qwen уважает `.gitignore` копии и молча вернул бы пусто
  (исходники Qwen Code 0.25: ripgrep без `--no-ignore-vcs`; `read_file` смотрит только
  `.qwenignore`), поэтому поиск — действие расширения `search`.
- Один и тот же текст правил — `alla_skill_lib/agent_rules.py` (`EXECUTOR_RULES`): он
  подставляется в `clusters/NN.md`, `batches/N.md`, `relate_task.md`, `summary_task.md` и
  повторён в `SKILL.md`.
- Форматы файлов, которые пишет агент, — в `references/`: `analysis-format.md`
  (`analyses/NN.md`), `proposal-format.md` (`proposals/NN.md`), `related-format.md`
  (`related.md`), `summary-format.md` (`summary.md`), `feedback.md` (`feedback/NN.md`); `protocol.md` — статусы, лимиты попыток и
  разграничение файлов.
- Шаблоны из констант кода (`EXPECTED_FORMAT`, `PROPOSAL_FORMAT`, `FEEDBACK_FORMAT`,
  `RELATED_FORMAT`) вставлены в
  справочники дословно; примеры с пометкой `<!-- example: … -->` прогоняет через настоящие
  парсеры `tests/test_skill_docs.py`. Меняя формат или текст ошибки в коде, правьте
  справочник — тест покажет расхождение.
- Ответ `fix` на предпоследней попытке предупреждает, что третья версия разбора будет
  принята с пометкой «формат нарушен».
- «Файл верен, а проверка отклоняет» — конкретное действие, а не «продолжай по сценарию»:
  разбор — исправить форму, факты не подгонять (непроверяемую цитату заменить или убрать;
  `cli.format_mismatch_note`), предложение — «не трогать» с местом правки в ПОЧЕМУ
  (`proposal_mismatch_note`), субагент — то же в выводе `verify` и строка «Проблема скилла»;
  `next` без правки файла не повторять. Неполадку записать по абсолютному пути
  (`agent_rules.record_problem`). Останавливать `remember` считает сам скрипт (модель отказы
  не сосчитает, см. `skill-memory-feedback.md`).
- `errors.py` — подсказки по сбоям выгрузки: токен, TLS, таймаут, лимит страниц, 404. При
  ошибке сертификата сначала — корневой сертификат прокси в `SSL_CERT_FILE` (httpx читает её
  из окружения, не из `.env`), `ALLURE_SSL_VERIFY=false` — только временный обход с
  последствием (токен не защищён).
