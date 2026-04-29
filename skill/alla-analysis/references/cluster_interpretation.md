# Cluster Interpretation Rules

Use these rules after `scripts/run_alla_analysis.py` returns compact JSON.

## Prioritization

- Sort clusters by `size` descending.
- Поднимай приоритет кластеров со слабым или отсутствующим совпадением в базе знаний.
- Raise priority for clusters where LLM analysis failed, was skipped, or returned an error.
- Treat muted failures as context, not active work, unless the user explicitly asks about muted tests.

## База знаний

- Score `>= 0.75`: likely known issue, but still check whether the step path and representative message agree.
- Score `0.40..0.74`: partial match; present as a hypothesis, not a confirmed cause.
- Score `< 0.40` or no match: likely unknown/new issue; recommend collecting logs, correlation ids, service owner context, and adding an entry to «база знаний» after confirmation.
- `match_origin=feedback_exact` is stronger than text-only similarity when the current cluster context still matches.

## LLM Signals

- Surface cluster-level `llm_error` values explicitly.
- Use `llm_verdict` for root-cause hypotheses and recommended actions, but do not paste long verdicts verbatim.
- If there is a launch-level summary, fold it into the executive summary and compare it with the largest clusters.

## Merge And Split Suspicion

- Potential merge: similar labels, similar representative messages, same step path, or same correlation hint across multiple clusters.
- Potential split: one large cluster with mixed step paths, mixed exception categories, or a vague label covering visibly different messages.
- Suggest merge/split review only as an action, not as a fact, unless the data is very strong.

## Root Cause Categories

- `test`: assertions, waits, test data setup inside the test, selector drift, test framework failures.
- `service`: API 4xx/5xx, backend validation, downstream timeouts, business logic regressions.
- `env`: infrastructure, network, unavailable dependencies, deployment/configuration issues.
- `data`: missing or stale fixtures, account state, duplicate entities, inconsistent reference data.

## Recommended Output

- Keep the answer compact and decision-oriented.
- Для каждого важного кластера укажи: label, size, strongest evidence, уверенность совпадения с базой знаний, likely category, next action.
- Prefer concrete next steps over restating raw JSON.
