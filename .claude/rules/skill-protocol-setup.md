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

- Bootstrap `alla_skill.py`, `alla_release.py` — stdlib, Python 3.8+; основная логика — 3.11+.
- `setup` только проверяет существующую `.venv`, Python и точные версии всех применимых
  записей lock-файла; печатает инструкцию инженеру и ничего не записывает. Нет окружения
  или зависимостей — `setup_required`; сломанный или старый Python — `error`; готово —
  `ready`. STATUS — первая строка. Параметров установки нет; прежние `--wheelhouse` и
  `--python` отклоняются. Pip/ensurepip, создание venv и `.env` удалены из реализации.
- Перед запуском CLI готовность проверяется по установленным версиям через stdlib
  `importlib.metadata`; маркер `.venv/.alla-setup-complete` игнорируется и не меняется.
  Lock допускает точные hashed deps и применимые Python-маркеры. Проверка не создаёт
  Python-кэши в `.qwen/`. Окружение, `.env` из образца и `.gitignore` готовит инженер.
- `update [--source PATH]` работает без venv. `[update] source` — локальный путь; прежние
  repo/ref/path и URL запрещены. Git не вызывается. Каталог/ZIP/TAR/TAR.GZ проходят проверку
  путей, links/special files, collisions, лимиты100MiB/10000entries/256MiB распакованного потока.
  `.env` и backups, `.venv`, `.git`, кэши не входят в payload/diff; `.env.example` сохраняется.
  Источник закрепляется fd: O_NOFOLLOW/fstat, обход каталогов через dir_fd без повторного
  разрешения пути. Исключённые .env не открываются. Пользовательские symlink parents
  запрещены; root-owned системные /tmp,/var,/etc aliases допускаются только по точной карте.
  Архив читается из одного fd в ограниченный private snapshot, затем парсится только снимок.
- Релиз с sha256 по путям и contenthashes атомарно публикуется в
  `<project>/.alla-releases/<digest>/alla-launch` с manifest/complete/diff. Повтор проверяет
  все байты; повреждённый каталог не перезаписывается. `STATUS: update_ready` показывает пути.
  Активация вручную, старая папка сохранена; никакого swap активной папки или установки deps.
- CI smoke готовит окружение и конфигурацию явными командами инженера, отдельно от скилла;
  проверяет отсутствие записи у setup/install-files/toggle, затем synthetic check/prepare.

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
- Qwen читает файлы при старте сеанса. `install-files` и `extensions enable/disable`
  только печатают исходные/целевые пути, готовое содержимое и diff; toggle — также
  корневую строку `enabled`. Файлы и конфигурацию размещает инженер. У команд нет
  `--yes`/`--diff` и пути применения. Проверки манифестов, настроек, конфликтов и путей
  сохранены; чужие изменённые файлы при отключении лишь отмечаются.
- Нет транзакционной записи, блокировок или восстановления файлов. Старый журнал
  незавершённой транзакции требует ручной проверки, не выполняется и не удаляется.
- Инструкция скилла заканчивается показом результата, без вопроса «установить?» и
  исполнения по обычному «да». Отдельное поручение Qwen выполнить ручные действия —
  вне сценария скилла. Setup/update/prepare не ставят файлы в `.qwen/`.

Просмотр файлов и enable/disable отклоняют ссылки в пути config.toml до обнаружения
расширений. Текущую подходящую enabled показывают как справку без повторной правки;
чужой файл при disable остаётся отдельной инструкцией со статусом install_files.
