# Run the checkout experiment

[Back to the project](../README.md)

Follow the steps in order, using the same terminal. The experiment takes about
three minutes; model requests take additional time and may wait for provider quota.

## Before you start

You should know basic Python, JSON, terminal commands, and HTTP requests.
The [short observability guide](../prerequisites/README.md) covers the remaining
basics using this demo. Advanced telemetry query languages are not required.

Python **3.14.6** was used locally; the agent declares Python 3.11+. You also need
Docker Compose, a running container engine, and free ports for the
[telemetry services](../telemetry-lab/compose.yaml) and the experiment on port 8011.

The agent loads `.env` through `python-dotenv`; existing environment variables
win. [.env.example](../.env.example) contains blank credentials and uses
`LLM_MODEL=moonshot/kimi-k3` with `MOONSHOT_API_KEY`, the last model exercised live.
Gemini, OpenRouter, and OpenAI configuration alternatives are listed there too;
they are not a promise of model availability. Keep `.env` and `kimi-key` private.

Provider availability, quotas, and charges depend on your account. Moonshot and
OpenAI are not assumed free. OpenRouter requires an explicit `:free` model and
requests zero pricing with provider fallback disabled. No billing is enabled and
no provider is switched automatically.

Collection needs telemetry backends but no API key. Diagnosis and report replay
need a key. Tests, saved-run evaluation, and simulated approval work offline.

## 1. Install and start the telemetry stack

Clone the project first, or skip the first two commands if you are already in its
directory. Separate virtual environments keep the demo's pinned telemetry
dependencies apart from the agent.

```sh
git clone https://github.com/lalitb/incident-agent.git
cd incident-agent

python3 -m venv .venv
.venv/bin/python -m pip install .
python3 -m venv .venv-demo
.venv-demo/bin/python -m pip install -r demo/requirements.txt

# Create only if absent; edit locally to supply your model and key.
test -f .env || cp .env.example .env

docker compose -f telemetry-lab/compose.yaml up -d --wait
```

## 2. Generate baseline and incident traffic

The experiment helper starts the checkout app on port 8011, verifies each pool
setting, generates 60 seconds of traffic per phase, waits for export, and stops
its app processes. Allow about three minutes. Other checkout traffic can mix
into this service window; stop that traffic for a clean comparison.

```sh
EXPERIMENT="runs/experiment-$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -u -m evaluations.experiment "$EXPERIMENT" \
  --python "$PWD/.venv-demo/bin/python" --seconds 60

START=$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["window"]["start"])' "$EXPERIMENT/ground_truth.json")
END=$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["window"]["end"])' "$EXPERIMENT/ground_truth.json")
printf 'Investigation window: %s to %s\n' "$START" "$END"
```

Use a completed experiment's window. `ground_truth.json` records actual UTC times,
configuration, and client measurements; only its window is passed to diagnosis.
Collection requires explicit timezone-aware start/end values at most one hour
apart. Historical windows stop working when backend retention expires.

## 3. Investigate and inspect the report

```sh
# Fixed collection without inference or an API key
.venv/bin/python -m agent.diagnose --collect-only --start "$START" --end "$END"

# Fixed collection plus a model-generated report
.venv/bin/python -u -m agent.diagnose --start "$START" --end "$END"

# Adaptive collection plus a report, on the same window and model
.venv/bin/python -u -m agent.diagnose --adaptive --start "$START" --end "$END"

# Replace <RUN_ID> with the ID printed as "Run directory: runs/...".
.venv/bin/python -m agent.evaluate "runs/<RUN_ID>" --scenario incident
```

## Reuse saved evidence

Run diagnosis commands separately; request pacing is per process. To isolate
report generation from telemetry collection, replay saved evidence. This makes
one logical model call and uses the saved window, so omit `--start` and `--end`:

```sh
.venv/bin/python -m agent.diagnose --evidence-file "runs/<RUN_ID>/evidence.json"
```

## Try simulated approval

For a successful report using the current schema:

```sh
RUN="runs/<RUN_ID>"
.venv/bin/python -m agent.remediate "$RUN"           # proposal only
.venv/bin/python -m agent.remediate "$RUN" --approve # explicit approval
# Or, instead of approval:
.venv/bin/python -m agent.remediate "$RUN" --reject
```

A new proposal defaults to `not_requested`. Approval/rejection updates
`remediation.json` and controller status. **It executes no shell command,
deployment change, or database mutation.**

## Troubleshooting and cleanup

- **No telemetry:** check the UTC window, traffic, and `docker compose -f telemetry-lab/compose.yaml ps`. Generate a fresh experiment if data expired.
- **Provider failure:** inspect model metadata, attempts, and retry stop reason in `controller.json`. Check your account quota and `.env`; existing environment variables override it.
- **Rejected decision:** inspect the validation error and subsequent recovery in `controller.json`.
- **Rejected report:** inspect `failure.detail`, the candidate, and cited evidence. Do not relax measurement checks to accept an invented value.

The experiment stops its app processes. Stop the telemetry environment while
preserving its containers and volumes with:

```sh
docker compose -f telemetry-lab/compose.yaml stop
```

Avoid `down -v` when preserving data. Stopping containers does not extend retention.
If ClickHouse was already running before cleanup, follow the [optional ClickHouse exercise](../exercises/clickhouse/README.md)
stop command; configuration edits do not stop existing containers. Deeper design
trade-offs belong in the companion Medium article; its link is pending publication.
