---
paths:
  - "qwen-skill/alla-launch/extensions/git-publish/**"
  - "qwen-skill/alla-launch/tests/test_ext_git_publish.py"
---

# Расширения: git-publish

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
