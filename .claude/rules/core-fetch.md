---
paths:
  - "qwen-skill/alla-launch/scripts/alla_core/services/triage_service.py"
  - "qwen-skill/alla-launch/scripts/alla_core/services/retry_linking.py"
  - "qwen-skill/alla-launch/scripts/alla_core/services/image_attachments.py"
  - "qwen-skill/alla-launch/scripts/alla_core/models/testops.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/pipeline.py"
  - "qwen-skill/alla-launch/tests/test_core_triage.py"
  - "qwen-skill/alla-launch/tests/test_core_fetch.py"
  - "qwen-skill/alla-launch/tests/test_core_retries.py"
  - "qwen-skill/alla-launch/scripts/alla_core/clients/testops_client.py"
  - "qwen-skill/alla-launch/scripts/alla_core/clients/mcp_client.py"
  - "qwen-skill/alla-launch/scripts/alla_core/exceptions.py"
  - "qwen-skill/alla-launch/scripts/alla_core/utils/text_normalization.py"
  - "qwen-skill/alla-launch/scripts/alla_core/services/log_extraction_service.py"
---

# Ядро: сбор данных — выгрузка и триаж

- `TriageReport.launch_created_ms` — `createdDate` прогона, а если сервер его не знает (MCP:
  инструмента прогона нет) — самый ранний `createdDate` результатов; `prepare`
  пишет его в `run.json` как `launch_created` (ISO 8601, UTC). Нужен расширениям: `sut-code`
  по нему выбирает ревизию приложения, если инженер её не назвал.

- **Полнота выгрузки** (`TriageReport.fetch`, `FetchInfo`; в `run.json` внутри `triage`,
  только числа). Результаты без повторов id (`_unique_results`: страницы идущего прогона
  сдвигаются); `results_reported` — `totalElements` последней страницы
  (`TestOpsMcpClient.last_total_elements`, у фейковых провайдеров нет — `None`);
  `execution_failed`/`detail_failed` — активные падения, чьё дерево шагов или запасной
  запрос результата по id не получены после повторов (у них может не быть сообщения и
  шага — тест уйдёт не в ту проблему). `launch_closed` — поле `closed` прогона, `None` —
  TestOps его не прислал (тогда прогон идущим не считается). `fetch_warnings` — предупреждения
  прогона (`run.json` `warnings`, вывод `prepare`, отчёт) с командой `prepare <id> --fresh`:
  прогон не закрыт, получено меньше `totalElements`, данные тестов не получены. Лимит
  страниц (`max_pages`) проверяется по первой странице: `PaginationLimitError` с
  `pages_needed`, подсказка `errors.py` называет нужное значение `ALLURE_MAX_PAGES`.
- `hidden` results исключаются из статистики и анализа как retry/non-final, но до фильтрации
  связываются с финальными (`services/retry_linking.py`, шаг 5). Ключ — свой у каждой
  попытки (`attempt_key`), первое поле, значение которого у неё есть: `historyId`, затем
  `historyKey`, иначе `testCaseId` + `parameters` + `environment`; финальный ищется по тому же
  полю (финальные проиндексированы по каждому), на следующее поле попытка не переходит —
  другой `historyId` при том же `historyKey` — другое выполнение. Раньше ключ был один на
  прогон, и попытка без `historyId` в прогоне, где он есть у других, не связывалась (dev
  `retries_more`: 11 → 13 из 15). `RetryInfo.linked_by` — использованные поля через запятую.
  Контекст — `testCaseId` + `parameters` + `environment` (только если есть `testCaseId` и оба поля —
  список или словарь, пустой тоже; нет поля или `null` — контекст неизвестен, а не пуст;
  пары «имя=значение» отсортированы, `excluded: true` пропускаются), иначе не
  связываются. Один `testCaseId` — не ключ (смешал бы параметризованные тесты); смена
  окружения или параметров — другой ключ. Финальных с одним ключом несколько — попытки не
  связываются. Порядок попыток — `createdDate`, затем id. Итог — `TriageReport.retries`
  (`RetryInfo`: поле связи и числа, в `run.json` внутри `triage`); предупреждения прогона
  (`retry_warnings`) — только если что-то не связалось или ошибки попыток известны не все.
- Попытки активного падения — `FailedTestSummary.attempts` (`AttemptSummary`: id, статус,
  первая строка ошибки до 300 символов, `same_as_final`), последние 5; более ранние —
  число в `attempts_omitted`. Ошибка — из `statusDetails` списка (message, затем trace,
  затем верхний `trace`), иначе запрос результата по id (MCP, без expand) с параллелизмом
  `ALLURE_DETAIL_CONCURRENCY` и потолком `ALLURE_RETRY_MAX_DETAIL_REQUESTS` на прогон (по
  порядку падений; не запрошенные — `errors_capped`). «Та же ошибка» —
  `text_normalization.message_key` сообщения целиком (до 20 строк; нет сообщения — трейс без
  кадров стека Java/Python, `_attempt_text`), а не показанной первой строки: у RestAssured она
  общая («1 expectation failed.»), код ответа ниже — 401 и 500 сходили за одну ошибку, и
  `retry_different` терялся (ревью Codex); код бывает и в конце длинной строки. Ключ — как
  свёртка повторов лога (`repeat_key`: без времени, UUID, длинных чисел и имён потоков, но с
  кодами ошибок); у финального результата — из того же списка результатов, что у попыток
  (нет там — из сводки). Представлений три (`_ErrorKeys`): сообщение, трейс без кадров (с
  `Caused by`), текст верхнего исключения без класса; сравнивается первое, что есть у обеих
  сторон (`_same_error`): у попытки только трейс, у финального сообщение и тот же трейс —
  одинаковые ошибки иначе выходили разными (второе ревью Codex). `None` — у попытки или
  финального результата ошибки нет. Сбой запроса — ошибка
  неизвестна. Финальный `passed` с неудачными попытками — `RetryInfo.passed_after_retry`
  (ошибка только из списка, без запросов). Попытки не участвуют в сигнатуре и кластеризации.
- muted failed/broken считаются отдельно и в активные кластеры не попадают.
- Ошибка берётся из execution tree, затем из `statusDetails`, затем fallback
  запрос результата по id — для каждого падения **без трейса**, даже если сообщение есть
  (в списке трейса обычно нет: общий ассерт о коде ответа оставался без стека с причиной и
  местом падения — ревью Codex); сообщение из него — только если своего не было, и тогда
  даже без трейса (по MCP в списке ошибки может не быть, а в полном ответе она есть).
- Логи — из processable attachments: text, JSON, XML, NDJSON и unknown text-like. Binary
  пропускаются.
- Изображения (`services/image_attachments.py`) не берутся при извлечении логов (содержимое
  приходит в ответе MCP вместе с результатом, но в задание идёт позже и не всё):
  `enrich_with_logs` записывает их метаданные в `FailedTestSummary.image_attachments`
  (`ImageAttachmentRef`, `exclude=True`, в `run.json` не пишутся). Изображение — MIME
  `image/png|jpeg|gif|webp`, без MIME — расширение; SVG — нет (текст, может нести скрипт).
  Шаг — по дереву исполнения: вложение шага с тем же id; по имени — только вложения дерева
  без id, и если имя однозначно с обеих сторон: одно среди них и одно среди вложений списка,
  не сопоставленных по id (иначе шаг неизвестен) (одноимённый `screenshot.png` после теста вне дерева
  иначе получал шаг упавшего и вытеснял его скриншот — ревью Codex); `failed_step` — шаг или любой его предок failed/broken (через сколько угодно passed-шагов). Скачивает и показывает модели
  скилл (`skill-analysis-images.md`, «Скриншоты»). Из вложений для логов изображения исключены и
  без MIME (`is_image_attachment` по расширению): иначе фильтр MIME пропускал
  `screenshot.png` без типа, и картинки всех падений скачивались как логи — даже при
  `ALLURE_IMAGES_PER_PROBLEM=0`.
