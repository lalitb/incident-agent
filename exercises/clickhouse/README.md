# Optional SQL-verification exercise

ClickHouse is unnecessary for the agent. This preserves the earlier telemetry
storage setup for an optional lesson: compare measurements retrieved with SQL
against the agent's claims. Copies of the same telemetry in two backends are not
independent corroboration. No SQL agent tool or automatic verification is added.

From the project root, opt in before generating traffic:

```sh
docker compose -f telemetry-lab/compose.yaml \
  -f exercises/clickhouse/compose.yaml up -d --wait
```

The override adds ClickHouse and points the Collector at a configuration that
also sends it metrics, logs, and traces. Its paths are relative to the first
Compose file; use the order shown. It retains the original named data volume.

For a manual exercise, inspect the exported schema and retrieve checkout span
names and durations for one known trace ID. Compare acquisition and query spans
with the corresponding Tempo evidence. Keep the query read-only and scoped to
that trace. Do not give the model arbitrary SQL access.

Return to the default configuration without deleting stored data:

```sh
docker compose -f telemetry-lab/compose.yaml \
  -f exercises/clickhouse/compose.yaml stop clickhouse
docker compose -f telemetry-lab/compose.yaml up -d --wait
```

The cleanup changed configuration files only; an already running ClickHouse
container stays running until you explicitly stop it. Do not use `down -v` if
you want to preserve its data.
