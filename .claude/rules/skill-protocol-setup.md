---
paths:
  - "qwen-extension.json"
  - "skills/launch/SKILL.md"
  - "skills/launch/references/setup.md"
  - "scripts/alla_skill.py"
  - "commands/**"
  - "agents/**"
  - "tests/test_extension_manifest.py"
  - "tests/test_skill_commands.py"
  - "tests/test_skill_setup.py"
  - "scripts/alla_skill_lib/workspace.py"
---

# Протокол: установка и слэш-команды

## Точка входа и установка

- Корень репозитория является корнем нативного расширения Qwen Code CLI (`qwen-extension.json`, `name = "alla"`). Скилл лежит в `skills/launch/SKILL.md` (`name: launch` → `/alla:launch`), команды — в `commands/alla/*.md` (`/alla:*`), встроенные субагенты — в `agents/alla-*.md`.
- Bootstrap `alla_skill.py` — stdlib, Python 3.8+; основная логика — 3.11+. `project_root()` определяет проект от текущей рабочей папки (`Path.cwd().resolve()`). `venv_python()` читает актуальный `VENV_DIR` при каждом вызове, чтобы `--project-root` переключал и проверяемую `.venv`, и запускаемый Python.
- `setup` только проверяет существующую `.venv` проекта, Python и точные версии всех применимых
  записей lock-файла; печатает инструкцию инженеру и ничего не записывает. Нет окружения
  или зависимостей — `setup_required`; сломанный или старый Python — `error`; готово —
  `ready`. STATUS — первая строка. Параметров установки нет; прежние `--wheelhouse` и
  `--python` отклоняются. Pip/ensurepip, создание venv и `.env` удалены из реализации.
- Перед запуском CLI готовность проверяется по установленным версиям через stdlib
  `importlib.metadata`: вне `.venv` — одним изолированным подпроцессом `_probe_environment`
  (версия Python и зависимости разом), внутри `.venv` (`running_in_venv()`) — в том же
  процессе без подпроцессов строго по путям site-packages самого `.venv` (без `PYTHONPATH` и
  `cwd`); маркер `.venv/.alla-setup-complete` игнорируется и не меняется.
  Lock допускает точные hashed deps и применимые Python-маркеры. Проверка не создаёт
  Python-кэши в `.qwen/`. Окружение, `.qwen/alla.env` из образца и `.gitignore` готовит инженер.
- `<project>/.qwen/alla.env` (fallback — `<QWEN_HOME или ~/.qwen>/alla.env`) содержит только несекретные настройки:
  непустой `ALLURE_TOKEN` запрещён в обоих файлах и в `.env` / `.qwen/.env`. `<extension>/.env` не читается (его перезаписывает `qwen extensions update`), при обнаружении там `ALLURE_*` `check` печатает предупреждение о переносе.
  Токен хранится в системном хранилище ключей ОС через `keyring`: инженер сохраняет его
  командой `python3 <extension>/scripts/alla_skill.py token set` (скрытый ввод или `--stdin`)
  из корня проекта. Для общего токена — `--scope user`, для CI — окружение `ALLURE_TOKEN`.
  `setup` только показывает эти шаги, не устанавливает расширение и не спрашивает токен.
  `alla_skill.py` поддерживает универсальные PEP-508 маркеры `requirements.txt` (ограниченный
  разбор без `eval`: `python_full_version`, `python_version`, `sys_platform`, `os_name`,
  `platform_python_implementation`, `implementation_name`, `and`/`or`).
- Обновление и управление жизнью расширения выполняются штатно через `qwen extensions update alla` / `qwen extensions uninstall alla`; прежние `update` (`alla_release.py`) и `install-files` (`qwen_files.py`) удалены.
- CI smoke готовит окружение, расширение и конфигурацию явными командами инженера;
  проверяет отсутствие записи у `setup` и `extensions enable|disable`, затем synthetic `check`/`prepare`.

## Слэш-команды `/alla:*` (`commands/alla/`, `agents/`)

- `commands/alla/*.md` — нативные команды расширения Qwen Code (`FileCommandLoader`: подпапка `alla/` даёт имя
  `alla:<файл>`): `run`, `continue`, `apply`, `revert`, `remember`, `reject`, `review`,
  `setup`, `extensions`, `rerun`, `code`, `ticket`. Frontmatter — `description`, `argument-hint` (у команд с аргументами, тогда в теле
  `{{args}}`), `disable-model-invocation: true` (без него команду может вызвать сама модель).
- Тело — короткая просьба «вызови скилл alla:launch (инструмент skill) и действуй по разделу
  N»: шаги и правила не копируются, источник — `skills/launch/SKILL.md`. Папку разбора для `apply`,
  `revert`, `remember`, `reject`, `review`, `code`, `ticket` команда не знает — правило в разделе
  «Слэш-команды» скилла.
- Qwen загружает команды, скиллы и агентов расширения `alla` напрямую из установленного расширения.
  `extensions enable/disable` только проверяет манифест/настройки и печатает строку `enabled`
  и diff для `alla-extensions/config.toml` со статусом `ok` (без `--yes`/`--diff` и без записи).
- Старый журнал незавершённой транзакции требует ручной проверки, не выполняется и не удаляется.
- Setup и prepare не ставят файлы команд или агентов в `.qwen/`.

`extensions enable/disable` отклоняют ссылки в пути `config.toml` до обнаружения
расширений. Текущую подходящую `enabled` показывают как справку без повторной правки.
