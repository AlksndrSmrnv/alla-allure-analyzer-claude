# JSON-схема `submit_analysis`

Точная схема входного payload'а для:

```bash
cat analysis.json | \
  python alla-skill/scripts/submit_analysis.py --run-id 42 --input -
```

## Полная форма

```json
{
  "schema_version": 1,
  "launch_summary": {
    "summary_text": "...2-4 абзаца, см. launch_summary_guide.md...",
    "key_findings": ["...", "..."],
    "priority_actions": ["...", "..."],
    "unanalyzed_tail": {
      "cluster_count": 0,
      "test_count": 0,
      "note": null
    }
  },
  "clusters": {
    "c-abc123": {
      "category": "service",
      "confidence": "high",
      "analysis_text": "ЧТО СЛОМАЛОСЬ: ...\n\nПРИЧИНА: service — ...\n\nКАК ИСПРАВИТЬ:\n1. ...\n2. ...\n3. ...",
      "kb_alignment": {
        "matched_kb_entry_ids": [17],
        "rejected_kb_entry_ids": [],
        "rejection_reason": null
      },
      "recommendations": [
        "Перезапустить service-billing pod в стейдже",
        "Проверить health-check на /api/v1/orders"
      ]
    },
    "c-def456": {
      "category": "test",
      "confidence": "medium",
      "analysis_text": "...",
      "kb_alignment": {
        "matched_kb_entry_ids": [],
        "rejected_kb_entry_ids": [42],
        "rejection_reason": "score 0.81 (Tier 3) — совпадение по общему слову 'TimeoutException', нет переклички по компоненту."
      },
      "recommendations": ["..."]
    },
    "c-xyz999": {
      "category": "unanalyzed",
      "confidence": "low",
      "analysis_text": "tail (size=4)",
      "kb_alignment": {
        "matched_kb_entry_ids": [],
        "rejected_kb_entry_ids": [],
        "rejection_reason": null
      },
      "recommendations": []
    }
  }
}
```

## Поля верхнего уровня

| Поле | Тип | Обязат. | Описание |
|---|---|---|---|
| `schema_version` | int | да | Сейчас всегда `1`. |
| `launch_summary` | object | да | См. ниже. |
| `clusters` | object | да | `cluster_id → cluster_payload`. |

## `launch_summary`

| Поле | Тип | Обязат. | Описание |
|---|---|---|---|
| `summary_text` | string | да | 2–4 абзаца. **Канонический текст итогового summary.** Всё, что должно появиться в HTML-отчёте (включая ключевые наблюдения, приоритетные действия, упоминание tail), должно быть включено сюда — серверный путь через GigaChat ничего не дописывает извне. |
| `key_findings` | array[string] | нет | Совместимость со схемой v1. **НЕ рендерится:** адаптер `agent_to_launch_summary` игнорирует это поле, чтобы skill-отчёт совпадал с серверным. Включи нужное прямо в `summary_text`. |
| `priority_actions` | array[string] | нет | То же, что `key_findings`: оставлено для совместимости, не рендерится. |
| `unanalyzed_tail` | object | нет | То же. Если в режиме >30 кластеров есть tail — упомяни его в `summary_text` отдельным предложением. |

`unanalyzed_tail`:

| Поле | Тип | Описание |
|---|---|---|
| `cluster_count` | int | Сколько кластеров не анализировались. |
| `test_count` | int | Сколько тестов в tail-кластерах. |
| `note` | string \| null | Короткое объяснение для пользователя. |

## `clusters[<cluster_id>]`

| Поле | Тип | Обязат. | Допустимые значения |
|---|---|---|---|
| `category` | string | да | `test`, `service`, `env`, `data`, `unanalyzed` |
| `confidence` | string | да | `high`, `medium`, `low` |
| `analysis_text` | string | да | 1..8000 символов. **Канонический текст**, который попадает в HTML-отчёт и в комментарий TestOps. Конкретные шаги исправления должны быть включены прямо сюда в блоке `КАК ИСПРАВИТЬ:` (формат задаёт серверный cluster-analysis промпт). Для `unanalyzed` — короткая пометка вида `tail (size=4)`. |
| `kb_alignment` | object | да | См. ниже. |
| `recommendations` | array[string] | нет | Совместимость со схемой v1. **НЕ рендерится:** адаптер `agent_to_llm_result` игнорирует это поле, чтобы skill-отчёт совпадал с серверным. Шаги исправления должны быть в `analysis_text` (блок `КАК ИСПРАВИТЬ`). |

`kb_alignment`:

| Поле | Тип | Описание |
|---|---|---|
| `matched_kb_entry_ids` | array[int] | `entry_id` записей KB, которые ты признал применимыми. |
| `rejected_kb_entry_ids` | array[int] | `entry_id` записей, которые ты явно отверг (для feedback). |
| `rejection_reason` | string \| null | Короткий текст, почему отверг. |

## Валидация

`submit_analysis.py` (через `agent_analysis_adapter.validate_agent_payload`)
проверяет:

* `schema_version == 1`.
* `launch_summary.summary_text` — непустая строка.
* Каждый `clusters[*]` — объект.
* `category` ∈ `{test, service, env, data, unanalyzed}`.
* `confidence` ∈ `{high, medium, low}`.
* `analysis_text` непустой и ≤ 8000 символов.

При нарушении — exit 2 и envelope ошибки в stderr.

`missing_cluster_ids` (известные кластеры, для которых нет анализа)
и `extra_cluster_ids` (кластеры из payload, которых нет в clustering
report) выдаются в response, но не блокируют запись.

## Хороший ответ vs плохой

### Хороший

* `analysis_text` начинается с «ЧТО СЛОМАЛОСЬ: …», содержит цитату из
  message / log / KB error_example.
* `category` соответствует фактической причине.
* `confidence: high` — есть прямое доказательство; `medium` — есть
  косвенное; `low` — данных мало.
* Конкретные шаги исправления — в блоке `КАК ИСПРАВИТЬ:` внутри
  `analysis_text` (поле `recommendations` оставлено для совместимости,
  но не рендерится).

### Плохой

* «Проверьте сервер», «уточните у команды», «возможно, инфра» — это
  диагностические советы, не исправления.
* Упомянут сервис/класс/конфиг, которого нет в данных кластера и в
  KB — это фантазия.
* `confidence: high` без явного факта/цитаты.
* `analysis_text` — markdown с заголовками `## ЧТО СЛОМАЛОСЬ` —
  ломает форматирование комментария в TestOps. Используй обычные
  prefix-метки `ЧТО СЛОМАЛОСЬ:`.
