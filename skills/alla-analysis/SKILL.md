---
name: alla-analysis
description: Use this skill when the user asks to analyze an Allure TestOps/Alla launch, run Alla analysis, inspect launch failure clusters, get the main failing clusters, summarize an Alla run report, or investigate prompts such as “проанализируй прогон 12345”, “разбери launch”, “найди главные кластеры падений”. This skill uses the running alla-server REST API only.
---

# Alla Analysis

## Overview

Use this skill to run a read-only Alla analysis for an Allure TestOps launch through the running `alla-server` REST API, normalize the result, and produce an agent-level interpretation of the failure clusters.

## Quick Start

1. Run the helper script first:

```bash
python /Users/exc333ption/.codex/skills/alla-analysis/scripts/run_alla_analysis.py --launch-id 12345
```

2. If the user gives a launch name instead of an id, resolve it through the server:

```bash
python /Users/exc333ption/.codex/skills/alla-analysis/scripts/run_alla_analysis.py --launch-name "Launch name" --project-id 1
```

3. Generate HTML only when the user explicitly asks for a report file or link:

```bash
python /Users/exc333ption/.codex/skills/alla-analysis/scripts/run_alla_analysis.py --launch-id 12345 --html
```

The helper always sends `push_to_testops=false` unless the script is edited. This keeps the agent workflow read-only by default.

## Server URL

The server address lives in `scripts/run_alla_analysis.py` as `ALLA_SERVER_URL`. It is intentionally a placeholder and intentionally not read from environment variables. If the placeholder has not been replaced, stop and tell the user to set the server URL inside the script.

## Analysis Workflow

After running the helper, analyze the compact JSON it prints:

- Start with launch counters: total results, active failures, muted failures, failed/broken/skipped counts.
- Rank clusters by impact: larger clusters first, then clusters with no strong KB match, then clusters with LLM errors.
- Read `references/cluster_interpretation.md` before writing the final interpretation.
- Treat Alla's LLM text as source material, not as the whole answer. Add your own prioritization, merge/split suspicions, and concrete debugging steps.

## Final Response Shape

Return a concise investigation summary in Russian unless the user asks otherwise:

- executive summary of the run;
- top problematic clusters ordered by impact;
- likely shared root causes by category: test, service, env, data;
- suspicious split/merge candidates;
- concrete next debugging or fix actions;
- report URL or local HTML path when generated.

If the helper returns an error payload, explain the failure and include the relevant HTTP status/detail.
