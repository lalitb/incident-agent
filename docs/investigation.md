# Investigation reference

This is the implementation detail behind the [README's first example](../README.md).
The project keeps fixed collection, adaptive investigation, and saved-evidence
reporting as separate paths.

## Choose a mode

| Mode | Telemetry | Model requests |
| --- | --- | --- |
| Offline fixture evaluation | Synthetic, local | Scripted responses; no provider calls |
| Actual-model fixture evaluation | Synthetic, local | Configured provider |
| `--collect-only` | Running telemetry backends | None |
| Fixed diagnosis | Running telemetry backends | Report only |
| `--adaptive` | Running telemetry backends | Planning, review, and report |
| `--evidence-file` | Already saved evidence | Report only, in a new run |
| `--resume` | Original scope and remaining work | Uses the original remaining allowances |

The [evaluation guide](../evaluations/README.md) covers fixtures; the
[telemetry lab guide](telemetry-lab.md) covers collection from running backends.
Run model-driven commands sequentially: request pacing is per process.

## Execution limits

| Resource | Adaptive limit |
| --- | --- |
| Planning calls | 6 |
| Telemetry attempts | 8, including failures and retries |
| Identical-query retries | 1, only after a retryable failure |
| Pre-report reviews | 1 |
| Report calls | 1 |
| Verification checks | 12 |
| Local evidence lookups | 2, each also using a planning call |

Fixed collection has seven initial queries and up to three trace retrievals,
followed by at most one report call. It does not retry failed queries.

The controller validates an entire proposed batch before execution. Tools,
arguments, service, and time window are constrained. A trace must be discovered
before retrieval. Successful identical queries, including empty results, cannot
repeat. Rejected decisions consume planning allowance but execute no tools.

Network failures, HTTP 408/429, and selected transient 5xx responses are
retryable. Rejected requests, invalid data, and oversized results are permanent.
Failure messages do not expose raw backend responses. A successful empty result
is neither zero nor proof of a hypothesis.

Model-provider requests start at least 30 seconds apart within a process.
HTTP 429 permits at most three retries per logical call, normally waiting
60, 120, then 180 seconds. Longer provider delays are honored only within the
180-second wait limit. Recognized daily/zero quotas and other provider failures
stop without retrying; SDK retries are disabled. Adaptive runs therefore permit
at most eight logical model calls and 32 provider attempts.

These are execution limits, **not a dollar spending cap**. The bounded spending
guards used for saved actual-model evaluations were evaluation instrumentation,
not a feature of the normal CLI.

## Verification and review

Checks have stable IDs and retain their history. They start pending and can
become resolved or unresolvable with a recorded reason.

A resolution needs relevant evidence IDs, an explanation, and applicable
structured metric or span measurements. Python checks reference existence,
declared tool/metric compatibility, non-missing evidence, and measurement values.
Whether those facts explain the claim remains a model judgment.

Discovery and trace retrieval are allowed collection follow-ups, but their
evidence is not interchangeable for resolution. Likewise, a check declared for
one metric cannot be resolved by adding citations to unrelated metric targets.
Saved runs show that the model can repeatedly misunderstand these rules and
exhaust planning allowance; report validation does not hide that outcome.

An empty or inconclusive targeted result, permanent failure, or exhausted retry
may justify an unresolvable check. Remaining pending checks are closed with a
reason when collection ends. Their IDs and reasons remain in the report and
prevent high confidence.

The single review happens before the report. It can propose one actionable
missing check and return it to the controller if planning and tool budgets
remain. Replanning resets neither budget and never triggers another review.
The review sees evidence and decisions, not a draft report, so it cannot assess
wording introduced only in the final answer.

## State, context, and resume

`checkpoint.json` is the authoritative snapshot. The application reserves a slot
before external execution, then commits its outcome and evidence together.
`evidence.json` and `controller.json` are readable views with generation numbers;
they may lag after a crash. Resume uses the checkpoint, not a mixture of views.
A local lock prevents two writers.

For a supported unfinished adaptive collection run:

```sh
.venv/bin/python -m agent.diagnose --resume "runs/<RUN_ID>"
```

Do not supply a new mode, question, service, or time window. Resume restores the
run identity, evidence, failures, decisions, checks, query attempts, lookup
results, and remaining limits. It can make model and backend requests.

Unfinished decisions are abandoned without refunding their planning slots.
Requests never dispatched use no tool slot. An in-flight tool call is marked
outcome unknown; its reserved slot remains spent, and only its original retry
allowance can remain available.

An interrupted review is not repeated. An interrupted or failed report has
spent the sole report allowance, so resume rejects another report request.
To report separately on saved evidence, explicitly start a new report-only run:

```sh
.venv/bin/python -m agent.diagnose --evidence-file "runs/<RUN_ID>/evidence.json"
```

This uses the saved question/window, creates a new ID, and makes no collection
or review calls. It can incur a new model charge. It is not a budget-preserving
resume. Completed runs, old schemas, fixed runs, and inconsistent checkpoints
are not resumable. Evaluation-only setup runs are not native-resume examples;
the fixture evaluation runner manages its own supported interruption exercise.

Planning and review use a hard 80,000-byte UTF-8 context limit. They reuse
one-minute metric summaries and compact history. Further compaction preserves
evidence IDs, provenance, missing counts, and notices of omitted data.
Summarization is lossy; contradictory details can be omitted. Full collected,
redacted evidence remains on disk.

`context_records` describes the reductions. A local lookup returns at most
10 rows and 8,000 bytes from saved evidence; metric lookups return bucket details,
not the original per-sample values. Collection stops if essential context
cannot fit.

## Reports and approval

The reporter checks structured observations against evidence:

- Metric measurements name their evidence, metric, labels, bucket, statistic,
  value, and unit. Python recomputes the summary and compares the value.
- Configuration comparisons include observed pool limits.
- Trace breakdowns reproduce retrieved spans; search results are not span evidence.
- Timeline `source_timestamp` citations must be logs or retrieved traces.
  `sample_timestamp` and `metric_bucket` require metrics. Discovery results are
  never valid timeline citations, even when they include timestamps.

Reporting retains a 150,000-byte summarized-input limit and an 8,192-output-token
allowance. Provider errors preserve progress rather than creating an answer.
Rejected report candidates are saved separately; invalid provider response text
is not saved.

Adaptive runs can produce an insufficient-evidence report even when every query
fails. Empty fixed collection and empty report-only replay stop without a report.
Collection-only runs never request one.

Reported token totals are `null` when unknown; available subtotals are recorded
separately. Normal run records do not contain a billed dollar cost or hidden
chain-of-thought. Exact numbers, valid citations, or a completed report do not
prove a causal explanation.

Approval is a separate local simulation:

```sh
.venv/bin/python -m agent.remediate "runs/<RUN_ID>"
.venv/bin/python -m agent.remediate "runs/<RUN_ID>" --approve
# Alternatively: --reject
```

This records a choice in `remediation.json` and updates local status. It executes
no shell command, deployment change, or database mutation.

## Read the implementation

Start with [diagnose.py](../agent/diagnose.py) and [controller.py](../agent/controller.py),
then follow an accepted request through [gateway.py](../agent/gateway.py) to
[tools/telemetry.py](../agent/tools/telemetry.py).

[verification.py](../agent/verification.py), [context.py](../agent/context.py), and
[checkpoint.py](../agent/checkpoint.py) hold the state and evidence rules.
[model.py](../agent/model.py), [schemas.py](../agent/schemas.py), and
[validate_report.py](../agent/validate_report.py) define the reporting boundary;
[llm.py](../agent/llm.py) handles provider requests.

Current versions are `incident-run-v2`, checkpoint `1`, planner protocol
`adaptive-v4`, and report envelope `structured-observations-v2`. The clarified
prompts are `adaptive-v4.1` and `structured-observations-v2.1`. Historical artifacts
retain their original contracts; see [examples](../examples/README.md).

Common-pattern redaction is incomplete; reviewed synthetic examples do not
establish general injection resistance. Startup events are not deployment proof,
bucket boundaries are not exact onset times, and selected traces are not
population averages. RAG and native function calling remain separate exercises.
