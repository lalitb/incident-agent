# Evaluate behavior, not just successful execution

Offline checks need neither Docker nor an API key:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

They verify controller budgets, scope, duplicate recovery, atomic rejection,
trace discovery, failure records, redaction, approval, and structured reporting.
They do not score whether narrative interpretations are correct. A regression
explicitly demonstrates that unsupported prose can pass structural validation.

Cleanup verification on 2026-09-21: **83 tests passed, with no skips**. This includes
fixed/adaptive collection and saved-evidence replay with mocked providers/backends
and real report validation. Default and optional ClickHouse Compose configurations,
exporter references, CLI options, and documentation links were also checked.
No live provider request or new telemetry experiment was run during cleanup.

## One actual-model report

Reuse saved evidence without starting telemetry backends:

```sh
.venv/bin/python -u -m agent.diagnose \
  --evidence-file evaluations/fixtures/known_pool_incident.json
.venv/bin/python -m agent.evaluate "runs/<RUN_ID>" --scenario incident
```

This makes a provider request and uses your account quota. Other fixtures cover
normal operation, ambiguity, missing traces/backend failure, and a malicious log.
Keep [expected answers](expectations.json) out of model input. Manually score each
report as pass/partial/fail for conclusion, claim support, uncertainty, and tool
boundaries. A rejected report is an outcome to inspect, not silently discard.

For a batch, `.venv/bin/python -m evaluations.run_cases evaluations reports` replays the
five fixtures using the configured model. It writes logs and a run manifest in
`evaluations/`. Review a single report first. The `planner` stage uses the actual
model with a fixture-backed gateway to compare clean and injected logs; report
replay alone cannot test tool selection. One attack does not prove resistance.

## Live comparison

The README's experiment helper records actual UTC times and configurations
separately from model input. Run fixed and adaptive modes on the same completed
window and configured model. Compare queries, retrieved evidence, citations,
conclusions, uncertainty, tool calls, reported tokens, elapsed time, and rejections.
Confirm both collection and reporting completed; collecting evidence alone is
not a successful end-to-end diagnosis.

See the [historical run walkthroughs and selected artifacts](../examples/README.md).
They use the earlier contract, not the cleaned-up report schema. The full local
development archive is excluded from this repository.
