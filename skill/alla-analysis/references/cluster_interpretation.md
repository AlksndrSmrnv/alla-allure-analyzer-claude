# Правила интерпретации кластеров

Используй эти правила после того, как `scripts/run_alla_analysis.py` вернул
компактный JSON. Helper вызывает Alla в read-only режиме с
`push_to_testops=false`.

## Приоритизация

- Начинай с `counters.active_failures`: он исключает muted failures и показывает
  реальный объём работы по прогону.
- Сортируй кластеры по убыванию `size`.
- Поднимай приоритет кластеров со слабым или отсутствующим совпадением в базе знаний.
- Поднимай приоритет кластеров, где LLM analysis упал, был пропущен или вернул ошибку.
- Считай muted failures контекстом, а не активной работой, если пользователь
  явно не спрашивает про muted tests.

## База знаний

- `origin=feedback_exact`: самый сильный сигнал, потому что пользователь уже
  подтверждал эту exact issue signature. Всё равно проверь, что текущий
  step/message не противоречит совпадению.
- Score `>= 0.75`: вероятная known issue, но всё равно проверь согласованность
  step path и representative message.
- Score `0.40..0.74`: частичное совпадение; подавай как гипотезу, а не как
  подтверждённую причину.
- Score `< 0.40` или отсутствие совпадения: вероятно unknown/new issue;
  рекомендуй собрать logs, correlation ids, контекст service owner и после
  подтверждения добавить запись в базу знаний.
- Dislike/negative feedback vote означает, что match не стоит трактовать как
  рекомендацию для той же signature.

## LLM-Сигналы

- Явно показывай значения `llm_error` на уровне кластера.
- Используй `llm_verdict` для гипотез о root cause и recommended actions, но не
  вставляй длинные вердикты дословно.
- Если есть launch-level summary, включи её в краткий итог и сопоставь с
  крупнейшими кластерами.
- Если LLM отсутствует, не создавай впечатление, что AI-вывод есть; опирайся на
  message, log, trace и сигналы базы знаний.

## Подозрения на merge и split

- Potential merge: похожие labels, похожие representative messages, тот же step
  path, тот же correlation hint или явно один service/error code в нескольких
  кластерах.
- Potential split: один большой кластер со смешанными step paths, разными
  exception categories или расплывчатым label для явно разных messages.
- Предлагай review merge/split как действие, а не как факт, если данные не
  выглядят очень сильными.

## Категории root cause

- `test`: assertions, waits, test data setup внутри теста, selector drift,
  failures тестового framework.
- `service`: API 4xx/5xx, backend validation, downstream timeouts, regressions в
  business logic.
- `env`: infrastructure, network, недоступные dependencies, проблемы
  deployment/configuration.
- `data`: отсутствующие или устаревшие fixtures, account state, duplicate
  entities, inconsistent reference data.

## Рекомендуемый вывод

- Держи ответ компактным и decision-oriented.
- Для каждого важного кластера укажи: label, size, strongest evidence, уверенность совпадения с базой знаний, likely category, next action.
- Предпочитай конкретные next steps пересказу raw JSON.
