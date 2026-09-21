import base64
import json
import math
import re

from .common import (
    ToolError,
    evidence,
    fetch_json,
    validate_limit,
    validate_service,
    validate_window,
)


def query_metrics(service, metric, start, end):
    validate_service(service)
    start_seconds, end_seconds = validate_window(start, end)

    # service.name becomes service_name in our Collector's
    # Prometheus exporter configuration.
    labels = f'{{service_name="{service}"}}'

    def mean_duration(name):
        return (
            f"sum(rate({name}_sum{labels}[30s])) / "
            f"sum(rate({name}_count{labels}[30s]))"
        )

    queries = {
        "request_rate": (
            f"sum(rate(checkout_requests_total{labels}[30s]))"
        ),
        "request_duration_mean_seconds": mean_duration(
            "checkout_duration_seconds"
        ),
        "connection_wait_mean_seconds": mean_duration(
            "checkout_connection_wait_seconds"
        ),
        "pool_limit": f"checkout_pool_limit{labels}",
        "pool_in_use": f"checkout_pool_in_use{labels}",
        "pool_utilization": (
            f"checkout_pool_in_use{labels} / "
            f"checkout_pool_limit{labels}"
        ),
    }

    if metric not in queries:
        raise ValueError(
            f"Unknown metric. Choose one of: {sorted(queries)}"
        )

    path = "/api/v1/query_range"
    params = {
        "query": queries[metric],
        "start": start_seconds,
        "end": end_seconds,
        "step": 10,
        "timeout": "10s",
    }

    response = fetch_json("prometheus", path, params)

    if (
        response.get("status") != "success"
        or response.get("data", {}).get("resultType") != "matrix"
    ):
        raise ToolError("Unexpected Prometheus response structure")

    series = []
    for item in response["data"]["result"]:
        points = []

        for timestamp, raw_value in item["values"]:
            value = float(raw_value)

            # A ratio can be NaN when no requests occurred.
            # Preserve this as missing, never silently turn it into zero.
            points.append({
                "timestamp": timestamp,
                "value": value if math.isfinite(value) else None,
            })

        series.append({
            "labels": item["metric"],
            "points": points,
        })

    return evidence(
        "query_metrics",
        "prometheus",
        path,
        params,
        series,
        metric=metric,
        warnings=response.get("warnings", []),
    )


def find_traces(service, start, end, min_duration_ms=0, limit=10):
    validate_service(service)
    validate_limit(limit, 20)
    start_seconds, end_seconds = validate_window(start, end)

    if (
        type(min_duration_ms) is not int
        or not 0 <= min_duration_ms <= 60_000
    ):
        raise ValueError("min_duration_ms must be an integer from 0 to 60000")

    # Target the checkout request span, not any arbitrary child span.
    query = (
        f'{{ resource.service.name = "{service}" '
        f'&& name = "POST /checkout" '
        f'&& duration >= {min_duration_ms}ms }}'
    )

    path = "/api/search"
    params = {
        "q": query,
        "start": math.floor(start_seconds),
        "end": math.ceil(end_seconds),
        "limit": limit,
    }

    response = fetch_json("tempo", path, params)
    traces = response.get("traces", [])

    if not isinstance(traces, list):
        raise ToolError("Unexpected Tempo search response")

    # Tempo's limited search is not a global slowest-traces ranking.
    data = [
        {
            "trace_id": normalize_trace_id(item["traceID"]),
            "root_service": item.get("rootServiceName"),
            "root_span": item.get("rootTraceName"),
            "start_time_unix_nano": item.get("startTimeUnixNano"),
            "duration_ms": item.get("durationMs"),
        }
        for item in traces[:limit]
    ]

    return evidence(
        "find_traces",
        "tempo",
        path,
        params,
        data,
        possibly_truncated=len(traces) >= limit,
        selection="Limited matching traces; not ranked slowest",
    )


def _hex_id(value, byte_count):
    """Tempo JSON may encode span IDs as hex or base64."""
    if not value:
        return None

    if re.fullmatch(rf"[0-9a-fA-F]{{{byte_count * 2}}}", value):
        return value.lower()

    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != byte_count:
        raise ToolError("Invalid span ID length in Tempo response")

    return decoded.hex()


def get_trace(service, trace_id, start, end):
    validate_service(service)
    start_seconds, end_seconds = validate_window(start, end)

    trace_id = normalize_trace_id(trace_id)

    path = f"/api/traces/{trace_id.lower()}"
    params = {
        "start": math.floor(start_seconds),
        "end": math.ceil(end_seconds),
    }

    response = fetch_json("tempo", path, params)

    # Older Tempo JSON uses "batches"; OTLP uses "resourceSpans".
    batches = response.get("batches")
    if batches is None:
        batches = response.get("resourceSpans")

    if not isinstance(batches, list):
        raise ToolError("Unexpected Tempo trace response structure")

    spans = []

    for batch in batches:
        attributes = {
            item["key"]: item["value"]
            for item in batch.get("resource", {}).get("attributes", [])
        }
        service_name = attributes.get(
            "service.name", {}
        ).get("stringValue")

        if service_name != service:
            continue

        groups = (
            batch.get("scopeSpans")
            or batch.get("instrumentationLibrarySpans")
            or []
        )

        for group in groups:
            for span in group.get("spans", []):
                started = int(span["startTimeUnixNano"])
                ended = int(span["endTimeUnixNano"])

                spans.append({
                    "name": span["name"],
                    "span_id": _hex_id(span.get("spanId"), 8),
                    "parent_span_id": _hex_id(
                        span.get("parentSpanId"), 8
                    ),
                    "start_time_unix_nano": str(started),
                    "duration_ms": (ended - started) / 1_000_000,
                    "status": span.get("status", {}),
                })

    spans.sort(key=lambda item: int(item["start_time_unix_nano"]))

    return evidence(
        "get_trace",
        "tempo",
        path,
        params,
        spans[:100],
        trace_id=trace_id.lower(),
        service=service,
        truncated=len(spans) > 100,
        matching_span_count=len(spans),
    )


def search_logs(service, start, end, contains="", limit=30):
    validate_service(service)
    validate_limit(limit, 100)
    start_seconds, end_seconds = validate_window(start, end)

    if not isinstance(contains, str) or len(contains) > 200:
        raise ValueError("contains must be a string of at most 200 characters")

    query = f'{{service_name="{service}"}}'
    if contains:
        # Encode the literal instead of inserting unescaped query syntax.
        query += " |= " + json.dumps(contains, ensure_ascii=False)

    path = "/loki/api/v1/query_range"
    params = {
        "query": query,
        "start": str(int(start_seconds * 1_000_000_000)),
        "end": str(int(end_seconds * 1_000_000_000)),
        "limit": limit,
        "direction": "backward",
    }

    response = fetch_json("loki", path, params)

    if (
        response.get("status") != "success"
        or response.get("data", {}).get("resultType") != "streams"
    ):
        raise ToolError("Unexpected Loki response structure")

    logs = []
    for stream in response["data"]["result"]:
        for entry in stream["values"]:
            timestamp, line = entry[:2]
            logs.append({
                "timestamp_unix_nano": timestamp,
                "labels": stream["stream"],
                "message": line[:2000],
                "message_truncated": len(line) > 2000,
            })

    logs.sort(
        key=lambda item: int(item["timestamp_unix_nano"]),
        reverse=True,
    )

    return evidence(
        "search_logs",
        "loki",
        path,
        params,
        logs[:limit],
        possibly_truncated=len(logs) >= limit,
        content_is_untrusted=True,
    )

def normalize_trace_id(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[0-9a-fA-F]{1,32}", value)
        or int(value, 16) == 0
    ):
        raise ValueError("Invalid trace ID")
    return value.lower().zfill(32)