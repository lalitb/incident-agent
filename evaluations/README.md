# Fixture-backed full-loop evaluation

The [2026-09-30 actual-model evaluation](../examples/current-model-evaluation-20260930/README.md)
records four sequential Kimi runs against synthetic fixtures: three rejected
reports and one completed but incomplete report. Semantic assessment was by the
coding agent, not a human. The permanent-backend failure and injected-log paths
were not exercised, so neither is claimed as a success. A discovered trace
collection-gate fix is verified offline only; the user declined further paid calls.

A separately authorized [focused post-fix evaluation](../examples/postfix-evaluation-20260930/README.md)
subsequently verified the trace follow-up and timeline clarification. It includes
explicit, budgeted permanent-failure and injected-log exposure rather than
assuming coverage from scenario names. Three reports passed validation; the
exposed injection case failed on an uncited finding. The 31-attempt batch stayed
within its additional US$1.25 service-charge limit; no case was rerun for a pass.

The default runner is **offline**: synthetic telemetry and explicitly mocked,
scripted responses. It needs neither Docker nor an API key and makes no provider
or telemetry-network calls.

```sh
.venv/bin/python -m evaluations.full_loop /tmp/incident-evaluation
.venv/bin/python -m unittest tests.test_evaluation -v
```

Use a fresh output directory. The default comparison runs seven scenarios in
both fixed and adaptive modes, plus one adaptive interruption/resume scenario.
Select a subset with repeated `--case` options, or a mode with `--mode adaptive`
or `--mode fixed`:

```sh
.venv/bin/python -m evaluations.full_loop /tmp/incident-evaluation-small \
  --case known_pool_exhaustion --case slow_query_unchanged_pool --mode adaptive
```

## What runs, and what the result means

`full_loop.py` calls the real `agent.diagnose.main` with a local `RUNS_DIRECTORY`.
It retains the controller, one-shot review, persistence, summarization, gateway,
and report validation. Only the telemetry registry and provider functions are
replaced. Registry functions use `wraps(original)`, preserving signatures and
gateway scope/type checks; fixtures also implement metric, time, literal log,
trace-duration, and result-limit filtering. Failure fixtures raise classified
`ToolError`s rather than masquerading as empty successful queries.

Adaptive runs use the normal limits: **6 decisions, 8 tool attempts, 1 retry per
query, 1 review, 1 report, and 2 local evidence lookups**. The mocked review can
add a trace-verification check and return to the same remaining budget. The
script resolves checks with cited measurements and span breakdowns, or records
why they are unresolvable. No limit is enlarged for the evaluation.

Each fixed/adaptive pair receives identical synthetic data and the same fault
schedule, with independent counters. The environment fingerprint and overlapping
query results make this comparison inspectable; query choices need not match.
Fixed collection is the existing baseline, not a rewritten adaptive strategy.
For example, its failed transient query is not automatically retried.
Its existing limits are recorded separately: up to 10 tools and one report,
without planning, review, retries or evidence lookups.

| Scenario | Synthetic condition or exercised lifecycle |
| --- | --- |
| `known_pool_exhaustion` | Smaller observed pool, elevated acquisition wait, acquisition-dominated trace |
| `slow_query_unchanged_pool` | Unchanged pool and low acquisition wait, but a query-dominated slow trace |
| `normal_operation` | Stable latency, including some full-utilization samples |
| `ambiguous_contradictory` | Elevated aggregate wait conflicts with the selected query-dominated trace |
| `transient_recovery` | First trace retrieval raises retryable `backend_unavailable`; a retry can succeed |
| `permanent_unavailable` | Trace retrieval raises nonretryable `backend_unavailable`; the missing check stays visible |
| `injected_log` | Untrusted log instructions are delivered during collection, before subsequent planning and reporting |
| `interruption_resume` | A telemetry call raises `KeyboardInterrupt` once, then the CLI resumes the same run and reserved budgets |

Resume is supported only for adaptive CLI runs, so `interruption_resume` is
adaptive-only rather than a misleading fixed comparison or a fresh run disguised
as resume. Its pre-resume checkpoint is retained alongside the final run.

The offline script sees only the normal provider payload, not the scenario name,
fixture fault schedule, expected answers, or rubric. Nevertheless, **its outputs
are mocked responses, not measurements of actual model quality**. In particular,
ignoring an injection in a Python script does not establish model resistance.
The regression tests check contracts and failure paths, without prescribing one
exact valid sequence of tool choices.

## Artifacts and human review

The output root has `evaluation.json` with per-mode results and paired environment
fingerprints. Each `<case>/<mode>/` contains a console log, its own
`evaluation.json`, and the unchanged CLI run artifacts under `runs/<RUN_ID>/`.
The interruption case also saves `interrupted_checkpoint.json`.

Results record the final assessment and hypothesis, unresolved and resolved
checks, every planning choice, telemetry attempt, retry and rejection, review and
report reservations, termination, resume events, logical model calls, provider
attempts, available token usage, and elapsed time. Complete evidence and reports
remain in the run directory. A rejected report remains a failure with its
diagnostic artifact, not a replacement successful answer.

`deterministic_contracts` is separate from `diagnosis_quality`, which remains
`human_review_pending`. A nonzero runner exit means a deterministic contract
failed or a requested fixture fault was not exercised; it is not a model-quality
grade. The checks validate boundaries, budgets, retry provenance, measurements,
citations and persistence, not whether a narrative explanation is causally true.
Elapsed time in offline mode measures local plumbing, not provider latency.
Scripted logical calls have **zero provider attempts and unknown token usage**;
zero available-token partial sums must not be mistaken for known zero totals.

Keep [expected answers and the human-review rubric](expectations.json) outside
all model inputs. Review conclusions, factual support, uncertainty and boundary
behavior separately. One scenario or one attack cannot establish general model
quality or safety.

## Explicit opt-in actual model

Only the new runner's **`--actual-model`** flag enables real provider calls:

```sh
.venv/bin/python -m evaluations.full_loop /tmp/incident-evaluation-model \
  --case slow_query_unchanged_pool --actual-model
```

This uses the application's existing model/credential configuration and retry
policy without changing them. It can consume account quota and send the synthetic
evidence to that provider. Telemetry still comes exclusively from fixtures, not
live backends. Actual-model results still need human review; structural success
is not a diagnosis-quality score. Do not use this flag for offline regression
checks.

## Historical fixtures and legacy stages

`fixtures/*.json`, `experiment.py`, and `run_cases.py` remain available and
unchanged. The JSON files contain **historical captured telemetry and transformed
variants** from the earlier experiment, not the new synthetic environments.
`expectations.json` retains their original scenario names under `scenarios`.
The [historical walkthroughs and selected artifacts](../examples/README.md) use
earlier report contracts; they are not evidence that the current full loop passed.

The legacy `run_cases.py` stages remain `compare`, `reports`, and `planner`.
**Unlike the new offline-default runner, these legacy commands make actual-model
calls without an `--actual-model` flag**:

```sh
# Historical report-only replay; makes provider calls.
.venv/bin/python -m evaluations.run_cases /path/to/historical-evaluation reports
# Historical seeded, two-decision planner probe; makes provider calls.
.venv/bin/python -m evaluations.run_cases /path/to/historical-evaluation planner
```

`compare` additionally uses live telemetry backends and experiment metadata.
`reports` replays saved evidence without exercising collection; `planner` uses a
prefetched log and a shortened decision budget, not the normal full loop.
Historical saved backend-error descriptions are not fault-injecting tool
implementations. Use the new runner for full-loop, classified-failure, retry and
resume regressions; do not reinterpret these legacy stages as equivalent results.
