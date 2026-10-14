---
paths:
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/extensions.py"
  - "qwen-skill/alla-launch/extensions/**"
  - "qwen-skill/alla-launch/commands/alla/extensions.md"
  - "qwen-skill/alla-launch/tests/test_skill_extensions.py"
  - "qwen-skill/alla-launch/tests/test_ext_git_publish.py"
---

# Расширения скилла (`extensions.py`, `extensions/`)

Зачем: команды добавляют свои функции (git, задачи, чат) без форка скилла; включение —
одной командой. Руководство автора — `extensions/README.md` (контракт целиком), для
пользователя — `references/setup.md`, «Расширения»; для модели — `SKILL.md`, «7. Расширения».

## Устройство

- Расширение — папка с `extension.toml`: встроенные `<скилл>/extensions/<имя>/` (выключены),
  свои `<проект>/alla-extensions/<имя>/`; своё с тем же именем заменяет встроенное
  (`overrides_bundled`). Включение — `enabled = [...]` в `<проект>/alla-extensions/config.toml`
  (общее для команды, в git); `enable`/`disable` переписывают только эту строку (многострочный
  список — отказ), настройки `[<имя>]` переопределяют `[settings]` манифеста того же типа,
  неизвестный ключ — ошибка.
- Контракт `api = 1`. Скилл не импортирует код расширения: скрипты — отдельный процесс
  `sys.executable`, cwd — корень проекта, данные — JSON-контекст (`ALLA_CONTEXT`). Поля
  контекста и переменные шаблона задания — в `extensions/README.md`; их меняют только вместе
  с ним, несовместимо — только с новым `api`.
- События: `after_apply` (`_apply`, только `result.changed`), `after_revert` (`_revert`,
  отметка читается до отката), `after_report` (`next_step` при `done`, один раз на хэш
  `report.md` для каждого расширения: `state.json["extension_reports"]`). Повтор —
  `extensions run <имя> [N]`: `after_apply` только для правки в состоянии `applied`.
- Хук `task` — задание субагенту `alla-<имя>` (файл агента расширения, проверяется как
  `alla-batch`): контекст `<папка разбора>/extensions/<имя>/<stem>.json`, задание `<stem>.md`,
  в ответ команды — блок «Расширения:» (`ext.block`) с вызовом `agent` и запасным путём без
  `subagent_type`; `confirm = true` — сначала вопрос пользователю. Хук `script` — скилл
  запускает сам, вывод (≤ `OUTPUT_LIMIT`) — пользователю.
- Действие — `extensions action <имя> <действие> --context FILE [-- аргументы]`: только
  включённое расширение, контекст только из `<папка отчётов>/<разбор>/extensions/<имя>/*.json`,
  первая строка вывода скрипта `STATUS: ok|error`, иначе `error`.
- Ошибка расширения (битый манифест, упавший или зависший скрипт, неверные настройки) —
  строка блока «Расширения:», статус команды скилла не меняется (`fire` ловит всё).
- Агенты включённых расширений ставит `enable` и каждый `prepare`
  (`qwen_files.install_extension_agent`); `disable` убирает файл, только если он совпадает с
  нашим.
- Модель работу расширения не делает: субагент запускает только команду скилла из задания
  (правило «из shell только команды скилла» не ослаблено). `session_rules.WRITABLE_RE`
  разрешает субагенту только `extensions/<имя>/*.txt` (сообщение коммита).

## git-publish

- `scripts/publish.py` не меняет рабочую копию, индекс и HEAD: временный индекс
  (`GIT_INDEX_FILE`) от `base`; если версия файла в базе == `sha_before` — блоб = текущий файл
  байт в байт (`hash-object --stdin`, без фильтров), иначе `git apply --cached` показанного
  патча (`--directory`, если проект — подпапка репозитория), не ложится — отказ.
- Файл ≠ `sha_after` — отказ (ушли бы чужие правки). Ветка `<branch_prefix><прогон>-problem-<N>`,
  занята локально или на remote — `-2`…; `git branch` без checkout; `push -u` с
  `GIT_TERMINAL_PROMPT=0`; ссылка на MR — из строк `remote:`. Результат — `<stem>.result.json`:
  повтор после push — «уже выгружена», после неудачного push — та же локальная ветка.
- `after_revert.py` только напоминает о ветке на сервере, ничего не удаляет.
- Тесты — на настоящем git (`test_ext_git_publish.py`, изолированный `GIT_CONFIG_GLOBAL`);
  поведение модели — сценарий стенда `X01` (фикстура `test_bug`, bare-remote внутри проекта).

## Новое событие или поле

Вызов `ext.fire` в команде, поля — в `extensions/README.md`, событие — в `EVENTS`, тест в
`test_skill_extensions.py`; если модель должна что-то сделать иначе — `SKILL.md` раздел 7 и
`references/protocol.md`.
