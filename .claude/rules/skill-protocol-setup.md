---
paths:
  - "qwen-skill/alla-launch/scripts/alla_skill.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/qwen_files.py"
  - "qwen-skill/alla-launch/commands/**"
  - "qwen-skill/alla-launch/tests/test_skill_update.py"
  - "qwen-skill/alla-launch/tests/test_skill_commands.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/workspace.py"
---

# Протокол: установка и слэш-команды

## Точка входа и установка

- `scripts/alla_skill.py` (Python 3.8+) перезапускает себя в `.venv` в корне проекта
  автотестов (`project_root` — копия `workspace.detect_project_root`: `alla_skill_lib` на 3.8
  не импортируется).
- `setup [--python P] [-- pip-аргументы]` создаёт `.venv` проекта или берёт существующий;
  существующий на Python < 3.11 или неработающий — `STATUS: error`, чужой venv не
  пересоздаём; нет pip (`--without-pip`) — `ensurepip`, не вышло — `error` с командой. Корневой
  `.gitignore` проекта `setup` не меняет (замечание ревью: чужой файл без просьбы): если git
  не игнорирует `.venv` (`venv_needs_ignore`), только напоминает добавить `/.venv` — без слэша
  на конце, иначе шаблон не прикрывает `.venv`-симлинк (так его делает стенд). `check-ignore` —
  по пути `.venv`, если он есть (у симлинка путь со слэшем — ошибка 128), иначе `.venv/`. После
  успешного `pip install --require-hashes -r requirements.txt` (lock с точными версиями и хешами; диапазоны — в
  `requirements.in`, см. `CLAUDE.md`) пишет `.venv/.alla-setup-complete` (хэш `requirements.txt`) и создаёт
  `.env` скилла из образца. Без маркера все команды отвечают `STATUS: setup_required`.
- Установку с нуля на Linux проверяет `.github/scripts/skill_setup_smoke.sh` (job CI в
  `python:3.11`): `setup_required` до установки, `setup` дважды (второй берёт готовый `.venv`,
  `.gitignore` проекта не тронут, `.qwen/commands` нет), `install-files` без `--yes` (ничего не
  пишет) и с `--yes` (команды и `alla-batch`), права `.env` 600, `check` с пустым `.env` — `error` и код
  2, с фейковым TestOps — `ready`, `prepare 777` — `analyze`. Меняя вывод или коды `setup` и
  `check`, правь и скрипт. Фейковый сервер работает в `.venv` проекта, поэтому
  `skill_fake_testops.py` не импортирует pytest во время выполнения.
- `update [--source URL] [--ref REF]` — тоже в `alla_skill.py` до проверки `setup_complete()`
  (работает без `.venv`; в `cli.py` файлы скилла пришлось бы менять под работающим кодом).
  Адрес — таблица `[update]` (`repo`, `ref`, `path`) в `<проект>/alla-extensions/config.toml`,
  читается своим разбором `ключ = "значение"` (на 3.8 нет `tomllib`; остальные таблицы
  `extensions.load_config` отдаёт расширениям, `update` ему не мешает). Только для скилла в
  `…/.qwen/skills/` (рабочую копию разработчика не заменяет); пароль в адресе (у http(s) — любой userinfo, в том числе токен) — отказ; адрес `транспорт::…` (remote-helper) — тоже; `::` внутри IPv6 допустимо.
  `git clone --depth 1` с окружением как у `sut-code` (без вопросов, ssh `BatchMode`, `core.sshCommand` из настроек проекта передаётся в `GIT_SSH_COMMAND`,
  таймаут 90 с — Qwen обрывает shell на 120 с), версия — sha и папка скилла в репозитории (`path`) в `.alla-version` (совпали —
  `STATUS: ok`). Папка заменяется целиком через `.alla-update-*` рядом (откат при сбое),
  маркер версии пишется в копию до переименования (одновременные `update` не разводят файлы и маркер); сохраняются `.env` и `.venv`; команды `/alla:*` не трогаются (`install-files`). `setup` внутри не запускается —
  печатается его команда, если `setup_complete()` ложно. Стадии — в stderr: первая строка
  stdout `STATUS: updated|ok|error`. Проверяет `test_skill_update.py` на настоящем git.
- `/alla-launch установи` (`SKILL.md`, «1. Старт») — модель выполняет `setup`, затем `check`, и
  без номера прогона ничего не разбирает; пустой `.env` — `check` отвечает `error`, модель
  просит пользователя вписать адрес и токен. Отдельной команды скрипта для этого нет.

## Слэш-команды `/alla:*` (`commands/alla/`, `qwen_files.py`)

- `commands/alla/*.md` — команды Qwen Code (`FileCommandLoader`: подпапка `alla/` даёт имя
  `alla:<файл>`): `run`, `continue`, `apply`, `revert`, `remember`, `reject`, `review`,
  `setup`, `update`, `extensions`, `rerun`. Frontmatter — `description`, `argument-hint` (у команд с аргументами, тогда в теле
  `{{args}}`), `disable-model-invocation: true` (без него команду может вызвать сама модель).
- Тело — короткая просьба «вызови скилл alla-launch (инструмент skill) и действуй по разделу
  N»: шаги и правила не копируются, источник — `SKILL.md`. Папку разбора для `apply`,
  `revert`, `remember`, `reject`, `review` команда не знает — правило в разделе `SKILL.md`
  «Слэш-команды» (папка из сеанса, иначе `next` без аргументов; краткий разбор повторно не
  выводить).
- Qwen читает команды из `<project>/.qwen/commands/` при старте сеанса. Ставит их (и
  субагента `alla-batch`, и файлы включённых расширений) только `install-files`
  (`cli.cmd_install_files`, `qwen_files.planned_files`/`install_planned`) — замечание ревью:
  `setup`, `update` и `prepare` раньше писали в каталоги конфигурации агента без просьбы. Без
  `--yes` команда ничего не пишет: `STATUS: install_files`, список «создать/обновить: путь —
  что это» и просьба спросить пользователя; с `--yes` — запись только новых и изменённых
  файлов (чужие в `.qwen/commands/alla/` не удаляются), `STATUS: ok`. `setup` и `update`
  печатают подсказку с командой (`FILES_NOTE`), `check` — строку «Команды /alla:* и
  субагенты… установлены | не установлены или устарели»; сам `SKILL.md` («Файлы в `.qwen/`
  проекта») велит спросить «да». Одна реализация (Python 3.11): копии в `alla_skill.py` больше
  нет. `extensions enable/disable` остаются явной просьбой: ставят и убирают файлы своего
  расширения. `test_skill_commands.py` проверяет и формат, набор и разделы `SKILL.md`.
- Стенд кладёт команды в проект заранее (как со второго сеанса); сценарии `C01`, `C02`.
