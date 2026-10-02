# Investigate a local checkout application

The [offline example](../README.md#quick-start) is the easiest starting point.
This optional exercise replaces synthetic telemetry with a local application,
OpenTelemetry Collector, Prometheus, Loki, and Tempo. PostgreSQL supports the
application; Grafana is available for inspection at <http://localhost:3000>.

You need Docker Compose, a running container engine, and available ports for
the [telemetry stack](../telemetry-lab/compose.yaml) and the checkout app on 8011.
The commands below run from the repository root, after installing the agent
as described in the README.

## Start the lab

Use a separate environment for the demo application's dependencies:

```sh
python3 -m venv .venv-demo
.venv-demo/bin/python -m pip install -r demo/requirements.txt
docker compose -f telemetry-lab/compose.yaml up -d --wait
```

The experiment compares a pool of **10** connections with a pool of **2** under
the same ten-client load-generator settings. Configured database work is
0.2 seconds. Equal concurrency does not imply equal throughput: each client
waits for its response before sending another request.

```sh
EXPERIMENT="runs/experiment-$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -u -m evaluations.experiment "$EXPERIMENT" \
  --python "$PWD/.venv-demo/bin/python" --seconds 60

START=$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["window"]["start"])' "$EXPERIMENT/ground_truth.json")
END=$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["window"]["end"])' "$EXPERIMENT/ground_truth.json")
printf 'Investigation window: %s to %s\n' "$START" "$END"
```

Allow about three minutes. The helper starts the app, verifies each setting,
generates traffic, waits for export, and stops its app processes. Avoid other
checkout traffic during the experiment so observations are easier to compare.

`ground_truth.json` records actual UTC times, settings, and client measurements
separately from model input. Only the window is passed to diagnosis.

## Collect and investigate

Use the same terminal so `START` and `END` remain available.

```sh
# Fixed collection: no model request or API key needed.
.venv/bin/python -m agent.diagnose --collect-only --start "$START" --end "$END"

# Fixed collection followed by a model-generated report.
.venv/bin/python -m agent.diagnose --start "$START" --end "$END"

# Let the model choose queries, then generate a report.
.venv/bin/python -m agent.diagnose --adaptive --start "$START" --end "$END"
```

The last two commands require the [model configuration](../README.md#try-a-real-model)
and may incur provider charges. Run them separately rather than concurrently.
The default service is `checkout`; collection needs timezone-aware timestamps
with a positive interval of at most one hour.

Each command prints its run directory. To inspect one:

```sh
.venv/bin/python -m agent.evaluate "runs/<RUN_ID>" --scenario incident
```

For report-only replay or interrupted adaptive runs, see the
[investigation reference](investigation.md#state-context-and-resume).

## Understand the comparison

Historical results in the [original experiment](../examples/ground_truth.json)
showed client mean latency rising from about 212 ms to 1,029 ms, while selected
traces spent 818-838 ms acquiring a connection and 204-205 ms executing the query.
These are historical observations, not guaranteed outputs. Client percentiles,
server metric means, and individual trace durations describe different things.

Use the completed experiment's window while telemetry remains in retention.
An old window may return no data. Missing telemetry is not evidence that the
service was healthy.

If no observations appear, check the time window and service health:

```sh
docker compose -f telemetry-lab/compose.yaml ps
```

Provider failures or rejected reports leave progress and diagnostics in the
run directory. Read those records rather than relaxing measurement checks to
accept an unsupported answer.

## Stop the lab

The experiment stops its app processes. Stop the telemetry environment while
preserving containers and volumes with:

```sh
docker compose -f telemetry-lab/compose.yaml stop
```

Stopping services does not extend data retention. ClickHouse is excluded from
this stack; its configuration remains in a separate
[optional SQL exercise](../exercises/clickhouse/README.md).
