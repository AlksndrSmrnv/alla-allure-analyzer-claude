# Стратегия делегирования анализа кластеров

После `fetch_clusters.py` ты получаешь `cluster_count`. Стратегия:

| `cluster_count` | Стратегия |
|---|---|
| **1–2** | Inline в основном агенте |
| **3–10** | Один subagent на кластер (параллельно) |
| **11–30** | Batched subagents (2–3 кластера на subagent) |
| **>30** | Deep top-30 + tail summary |

## 1–2 кластера — inline

```text
for cluster in clusters:
  context = run("python alla-skill/scripts/get_cluster_context.py "
                "--run-id $RUN_ID --cluster-id " + cluster.cluster_id)
  apply context.system_prompt + context.user_prompt
  collect JSON-ответ
```

## 3–10 кластеров — один subagent на кластер

В Claude Code — `Task` tool с `subagent_type=general-purpose` для
каждого кластера, в qwen — `agent run alla-cluster-analyzer`. Промпт
для subagent'а — единый для обоих режимов:

```text
Прочти alla-skill/references/cluster_analysis_guide.md.
Получи контекст:
  python alla-skill/scripts/get_cluster_context.py --run-id $RUN_ID --cluster-id $CID
Применяй context.system_prompt + context.user_prompt буквально.
Верни СТРОГО JSON одного кластера по схеме (без markdown-блока):

{
  "cluster_id": "$CID",
  "category": "service",          // одно из: test|service|env|data
  "confidence": "high",           // high|medium|low
  "analysis_text": "...",
  "kb_alignment": {
    "matched_kb_entry_ids": [],
    "rejected_kb_entry_ids": [],
    "rejection_reason": null
  },
  "recommendations": ["...", "..."]
}
```

Запусти subagents параллельно. Дождись всех ответов.

## 11–30 кластеров — batched subagents

Каждому subagent'у даёшь по 2–3 `cluster_id`. Subagent последовательно
прогоняет `get_cluster_context.py` для каждого `cluster_id` и возвращает
**массив** объектов одного кластера (а не агрегированный объект):

```json
[
  {"cluster_id": "c-1", "category": "service", "confidence": "high",
   "analysis_text": "...", "kb_alignment": {...}, "recommendations": [...]},
  {"cluster_id": "c-2", ...},
  {"cluster_id": "c-3", ...}
]
```

Это упрощает финальную сборку: ты собираешь все массивы в плоский
словарь `cluster_id → analysis`.

Параллелизм: 3–4 batched subagent'а (≈ 10 кластеров на batch при 3
кластерах в каждом).

## >30 кластеров — deep top-30 + tail

При большом числе кластеров глубокий анализ всех нерационален: ты
сожжёшь контекст и время без пропорциональной пользы.

### Отбор top-30

Сортируй кластеры по убыванию `size × (1 − top_kb_score)`:

```text
priority = size * (1 - (top_kb_match.score if top_kb_match else 0.0))
```

Идея: большие кластеры важны, но если KB уже даёт точный ответ
(score ~1.0), глубокий разбор не нужен — он уже есть в KB-описании.
Вместо этого фокусируемся на больших и плохо покрытых KB.

Верхние 30 по `priority` → batched subagents (по 2–3 кластера).

### Tail (≈ 31..N)

Для оставшихся кластеров **не делай** индивидуального анализа.
В `submit_analysis` payload каждый из них включается с категорией
`"unanalyzed"`:

```json
"c-31": {
  "category": "unanalyzed",
  "confidence": "low",
  "analysis_text": "tail (size=4)",
  "kb_alignment": {"matched_kb_entry_ids": [],
                   "rejected_kb_entry_ids": [],
                   "rejection_reason": null},
  "recommendations": []
}
```

Адаптер `agent_to_llm_result` распознаёт `unanalyzed` и помечает
кластер как skipped (push в TestOps его не подхватит).

### Финальный launch_summary с tail-нотой

В `launch_summary.summary_text` обязательно упомяни:

```
Глубоко проанализированы 30 крупнейших кластеров. Ещё N кластеров
(K тестов) попали в tail-summary без индивидуального разбора —
рекомендуется отдельный проход или повторный запуск с увеличенным
ALLURE_CLUSTERING_THRESHOLD для огрубления группировки.
```

В payload — заполни поле `unanalyzed_tail`:

```json
"unanalyzed_tail": {
  "cluster_count": N,
  "test_count": K,
  "note": "Tail после deep top-30 (отбор по size × (1 − top_kb_score))."
}
```

## Когда нет Task tool (codex CLI)

Используй inline-цикл по всем кластерам с тем же промптом из
`cluster_analysis_guide.md`. Параллелизма не будет, но workflow
работает.
