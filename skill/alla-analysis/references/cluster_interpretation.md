# Cluster Interpretation Rules

Use these rules after `scripts/run_alla_analysis.py` returns compact JSON.
The helper calls Alla in read-only mode with `push_to_testops=false`.

## Prioritization

- Start with `counters.active_failures`: it excludes muted failures and is the
  real amount of work for the run.
- Sort clusters by `size` descending.
- Поднимай приоритет кластеров со слабым или отсутствующим совпадением в базе знаний.
- Raise priority for clusters where LLM analysis failed, was skipped, or returned an error.
- Treat muted failures as context, not active work, unless the user explicitly asks about muted tests.

## База знаний

- `origin=feedback_exact`: strongest signal, because a user previously
  confirmed this exact issue signature. Still verify the current step/message
  does not obviously contradict it.
- Score `>= 0.75`: likely known issue, but still check whether the step path
  and representative message agree.
- Score `0.40..0.74`: partial match; present as a hypothesis, not a confirmed
  cause.
- Score `< 0.40` or no match: likely unknown/new issue; recommend collecting
  logs, correlation ids, service owner context, and adding an entry to
  «база знаний» after confirmation.
- A dislike/negative feedback vote means the match should not be treated as a
  recommendation for the same signature.

## LLM Signals

- Surface cluster-level `llm_error` values explicitly.
- Use `llm_verdict` for root-cause hypotheses and recommended actions, but do not paste long verdicts verbatim.
- If there is a launch-level summary, fold it into the executive summary and compare it with the largest clusters.
- If LLM is absent, do not imply an AI conclusion exists; rely on message, log,
  trace and база знаний signals.

## Merge And Split Suspicion

- Potential merge: similar labels, similar representative messages, same step
  path, same correlation hint, or visibly same service/error code across
  multiple clusters.
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
