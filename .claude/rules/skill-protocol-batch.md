---
paths:
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/cli.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/batch_task.py"
  - "qwen-skill/alla-launch/tests/test_skill_parallel.py"
---

# Протокол: пакетный разбор и шаг `relate`

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
  P04). `prepare` ставит его в `<project>/.qwen/agents/` (`qwen_files.install_batch_agent`);
  Qwen видит агентов со старта сеанса, поэтому инструкция `next` даёт запасной путь — без
  `subagent_type`. Qwen молча пропускает невалидный файл агента: формат охраняет
  `test_batch_agent_file_is_valid_for_qwen_and_narrow`.
- Субагент `next` не вызывает. `verify N [N…] --run DIR` — только читающая проверка (те же
  parse/validate, что в `next`; `state.json`, история и файлы не трогаются, попытки не
  считаются), `STATUS: ok|fix`.
- `state.json` пишет только основной агент. Невалидный или недописанный разбор
  возвращается обычным `fix`/`analyze` по одному (лимит попыток прежний).
- Хвост меньше порога, `fix`, `propose`, `relate`, `summary` идут по одному.
- `next --workers N` (1–`MAX_WORKERS`=8; `--serial` = 1) сохраняется в `state.json`; это
  запасной путь без субагентов.

## Шаг `relate` (`cli._related_step`, `related.py`)

- После предложений правок и известных проблем, до сводки. Нет кандидатов
  (`related.candidate_groups`, см. `skill-analysis-related.md`) — шага нет: `related.md`, отметки в
  `state.json` удаляются, `next` сразу просит сводку.
- Данные групп (`relate_data`) хэшируются в `state.related_data_hash`; другой хэш — `related.md`
  удаляется, попытки `attempts["related"]` сбрасываются, `next` отвечает `STATUS: relate` с
  заданием `relate_task.md`. Тот же хэш — решение действует (после `skip`/`reject`/правки
  разбора, не задевших группы, повторно не спрашиваем).
- Неверный файл — `fix` с теми же `MAX_FIX_ATTEMPTS`/`_register_invalid`, что у разборов;
  запасной путь «верный файл отклонён» — `related_mismatch_note`: записать проблему скилла и
  переписать одной строкой `РАЗНЫЕ` со всеми номерами. После лимита решение отбрасывается:
  групп нет, в «Замечаниях» — `RELATED_DROPPED_NOTE`.
- `state.related` — только числа (`candidate_stats` и `confirmed_*`): их берёт `review` в
  `pilot-summary.md`.
