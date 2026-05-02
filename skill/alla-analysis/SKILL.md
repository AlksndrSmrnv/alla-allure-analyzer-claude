---
name: alla-analysis
description: Use this skill when the user asks to analyze an Allure TestOps/Alla launch, run Alla analysis, inspect launch failure clusters, get the main failing clusters, summarize an Alla run report, or investigate prompts such as “проанализируй прогон 12345”, “разбери launch”, “найди главные кластеры падений”. This skill uses the running alla-server REST API only.
---

# Alla Analysis

## Overview

Use this skill for read-only analysis through a running `alla-server` REST API.
The helper script calls the server, normalizes the large JSON into compact
agent-friendly output, and always sends `push_to_testops=false`.

This workflow is for investigation and summaries. It should not write comments
or report links back to TestOps.

## Quick Start

Analyze by numeric launch ID:

```bash
python skill/alla-analysis/scripts/run_alla_analysis.py --launch-id 12345
```

Resolve exact launch name first, then analyze:

```bash
python skill/alla-analysis/scripts/run_alla_analysis.py --launch-name "Launch name" --project-id 1
```

Generate an HTML file only when the user explicitly asks for a report/link:

```bash
python skill/alla-analysis/scripts/run_alla_analysis.py --launch-id 12345 --html
```

With `--html`, the helper calls `/api/v1/analyze/{launch_id}/html`, writes the
returned HTML to a temp file, and includes `html_report.html_path`. If the
server returns `X-Report-URL`, it is exposed as `html_report.report_url`.

## Server URL

The server address lives inside
`skill/alla-analysis/scripts/run_alla_analysis.py` as `ALLA_SERVER_URL`.

It intentionally does not read environment variables. If the value still
contains `TODO-ALLA-SERVER`, stop and tell the user to set the server URL in
the script.

## What The Helper Does

1. Calls `GET /health` and includes the response in `server.health`.
2. If needed, resolves a launch name through
   `GET /api/v1/launch/resolve?name=...&project_id=...`.
3. Calls `POST /api/v1/analyze/{launch_id}?push_to_testops=false`.
4. Optionally calls `POST /api/v1/analyze/{launch_id}/html?push_to_testops=false`.
5. Produces compact JSON with counters, cluster summaries, top matches from the
   база знаний, representative test context, LLM verdict/error, launch summary
   and duration.

## Output Fields To Use

- `counters.active_failures`: failed + broken minus muted failures.
- `clustering.clusters`: sorted by size descending.
- Per cluster: `label`, `size`, `step_path`, `representative_message`,
  `correlation_hint`, `trace_snippet`, `representative_test.log_snippet`.
- `kb_matches`: top matches with title, category, score, origin and feedback vote.
- `llm.llm_launch_summary.summary_text`: launch-level summary when available.
- Per cluster `llm_verdict` / `llm_error`: source material, not final answer.

## Analysis Workflow

- Read `references/cluster_interpretation.md` before writing the final answer.
- Start from counters and explain whether active failures are concentrated in a
  few clusters or spread out.
- Rank clusters by impact: size first, then missing/weak база знаний matches,
  then LLM errors/skips.
- Treat `origin=feedback_exact` as stronger than ordinary text similarity if
  the current step/message context still agrees.
- Treat Alla's LLM text as source material. Add your own prioritization,
  merge/split suspicions and concrete debugging steps.

## Final Response Shape

Return a concise investigation summary in Russian unless the user asks
otherwise:

- executive summary of the run;
- top problematic clusters ordered by impact;
- likely shared root causes by category: test, service, env, data;
- suspicious split/merge candidates;
- concrete next debugging or fix actions;
- report URL or local HTML path when generated.

If the helper returns an error payload, explain the failure and include the
relevant HTTP status/detail.
