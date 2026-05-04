# Использование с qwen / codex CLI

Скилл `alla-skill` намеренно сделан универсальным: бизнес-логика — в
Python-скриптах, всё взаимодействие — через JSON в stdin/stdout. Это
работает в Claude Code, qwen CLI и codex CLI.

Параллельный анализ кластеров через subagents — фича Claude Code и
qwen. В codex CLI её нет, поэтому используется inline-цикл.

## qwen CLI

### Установка subagent

Создай файл `~/.qwen/agents/alla-cluster-analyzer.yaml`:

```yaml
name: alla-cluster-analyzer
description: Analyze a single Allure TestOps failure cluster from alla-skill.
tools: [bash]
system_prompt: |
  Ты — аналитик упавших автотестов.

  При запуске тебе передают аргументом cluster_id и run_id.
  1. Получи контекст кластера через bash:
       python alla-skill/scripts/get_cluster_context.py \
         --run-id $RUN_ID --cluster-id $CLUSTER_ID
  2. Прочти alla-skill/references/cluster_analysis_guide.md.
  3. Применяй context.system_prompt + context.user_prompt буквально
     (они уже содержат все правила и данные).
  4. Возвращай СТРОГО JSON одного кластера по схеме из
     cluster_analysis_guide.md, без markdown-обёртки и без вступления:

  {
    "cluster_id": "...",
    "category": "test|service|env|data",
    "confidence": "high|medium|low",
    "analysis_text": "ЧТО СЛОМАЛОСЬ: ...\n\nПРИЧИНА: ...\n\nКАК ИСПРАВИТЬ:\n1. ...\n2. ...\n3. ...",
    "kb_alignment": {
      "matched_kb_entry_ids": [...],
      "rejected_kb_entry_ids": [...],
      "rejection_reason": null
    },
    "recommendations": ["...", "..."]
  }

  Не выдумывай факты, которых нет в данных. Если данных мало — пиши
  "данных недостаточно" и confidence: low.
```

YAML лежит в репозитории как референс — но саму регистрацию делает
пользователь через `~/.qwen/agents/`, мы файл туда не копируем.

### Запуск

```bash
# Получаем кластеры
RESPONSE=$(python alla-skill/scripts/fetch_clusters.py --launch-id 12345)
RUN_ID=$(echo "$RESPONSE" | jq -r .run_id)
CLUSTER_IDS=$(echo "$RESPONSE" | jq -r '.clusters[].cluster_id')

# Параллельные subagents (один на кластер)
for CID in $CLUSTER_IDS; do
  qwen agent run alla-cluster-analyzer \
    --input "{\"run_id\": $RUN_ID, \"cluster_id\": \"$CID\"}" &
done
wait
```

Стратегию по числу кластеров (1–2 inline / 3–10 один на кластер /
11–30 batched / >30 deep top-30) — см. `delegation_strategy.md`.

## codex CLI

Codex не поддерживает Task tool / subagents. Используй inline-цикл:

```bash
RESPONSE=$(python alla-skill/scripts/fetch_clusters.py --launch-id 12345)
RUN_ID=$(echo "$RESPONSE" | jq -r .run_id)
COUNT=$(echo "$RESPONSE" | jq -r .cluster_count)

ANALYSIS_FILE=/tmp/alla_analysis_$RUN_ID.json
# Внутри codex-сессии: поочерёдно анализируй каждый cluster_id,
# применяя получаемый system_prompt + user_prompt из get_cluster_context.
# Накапливай результаты в analysis.json по схеме analysis_schema.md.

cat $ANALYSIS_FILE | \
  python alla-skill/scripts/submit_analysis.py --run-id $RUN_ID --input -
python alla-skill/scripts/generate_report.py --run-id $RUN_ID
```

Параллелизма не будет, но при `cluster_count <= 10` это нормально.
Для бóльших прогонов — рассмотри переход на Claude Code или qwen.

## Claude Code

Скилл лежит в корне репо. SKILL.md распознаётся автоматически при
запросах вроде «проанализируй прогон 12345». Параллельные subagents
запускаются через Task tool с `subagent_type=general-purpose` и
промптом из `cluster_analysis_guide.md`.
