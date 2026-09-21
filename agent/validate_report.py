import math
from datetime import datetime

from .schemas import IncidentReport
from .summarize import summarize_evidence


def require(condition, message):
    if not condition:
        raise ValueError(f"Rejected report: {message}")


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "timeline timestamps must include a timezone")
    return result


def cap_confidence(report):
    if report.confidence != "high" or not report.leading_hypothesis.verification_needed:
        return None
    report.confidence = "low" if report.assessment == "insufficient_evidence" else "medium"
    return {"from": "high", "to": report.confidence,
            "reason": "winning hypothesis still requires verification",
            "gap_count": len(report.leading_hypothesis.verification_needed)}


def validate_source_interval(start, end, sources):
    intervals = []
    for item in sources:
        if item["tool"] == "search_logs":
            times = [int(row["timestamp_unix_nano"]) / 1e9 for row in item["data"]]
            intervals.append([(value, value) for value in times])
        else:
            intervals.append([
                (int(span["start_time_unix_nano"]) / 1e9,
                 int(span["start_time_unix_nano"]) / 1e9 + span["duration_ms"] / 1000)
                for span in item["data"]])

    endpoints = {time for group in intervals for pair in group for time in pair}
    if start != end and all(item["tool"] == "search_logs" for item in sources):
        require(any(first != last and abs(start.timestamp() - first) <= 0.000001
                    and abs(end.timestamp() - last) <= 0.000001
                    for first in endpoints for last in endpoints),
                "a log range must match distinct log events; individual log timestamps are instants, not durations")
    # Several citations may collectively support the endpoints of an interval.
    for value in (start, end):
        require(any(abs(value.timestamp() - point) <= 0.000001 for point in endpoints),
                "source timestamp is absent from its evidence")
    for group in intervals:
        require(any(first <= end.timestamp() + 0.000001 and last >= start.timestamp() - 0.000001
                    for first, last in group), "source citation is outside the claimed interval")


def validate_metric(measurement, item):
    require(item.get("metric") == measurement.metric, "metric does not match its citation")
    summary = summarize_evidence([item])[0]
    require(measurement.unit == summary["unit"], "metric unit does not match its evidence")
    timestamp = parse_time(measurement.bucket_start_utc)
    column = summary["bucket_columns"].index(measurement.statistic)
    values = []
    for series in summary["data"]:
        if series["labels"] != measurement.labels:
            continue
        for bucket in series["buckets"]:
            if parse_time(bucket[0]) == timestamp:
                values.append(bucket[column])
    require(bool(values), "metric labels or bucket are absent from its evidence")
    require(any(value is not None and measurement.value == value for value in values),
            "metric value differs from its cited bucket statistic")


def validate_timeline(observation, sources):
    start, end = parse_time(observation.start_utc), parse_time(observation.end_utc)
    require(start <= end, "timeline interval is reversed")
    if observation.precision == "source_timestamp":
        require(all(item["tool"] in {"search_logs", "get_trace"} for item in sources),
                "source timestamps must cite logs or retrieved traces")
        validate_source_interval(start, end, sources)
        return

    require(all(item["tool"] == "query_metrics" for item in sources), "metric timing must cite metrics")
    for item in sources:
        timestamps = {float(point["timestamp"]) for series in item["data"] for point in series["points"]}
        if observation.precision == "metric_bucket":
            require(start < end, "a metric bucket must be an interval")
            buckets = {math.floor(timestamp / 60) * 60 for timestamp in timestamps}
            require(start.timestamp() in buckets and end.timestamp() - 60 in buckets,
                    "metric bucket is absent from its evidence")
        else:
            require(start == end, "a sample timestamp must be an instant")
            require(start.timestamp() in timestamps, "sample timestamp is absent from its evidence")


def validate_report(report: IncidentReport, evidence):
    by_id = {item["evidence_id"]: item for item in evidence}
    require(len(by_id) == len(evidence), "duplicate evidence IDs")

    def lookup(evidence_id, expected_tool=None):
        require(evidence_id in by_id, f"unknown evidence ID {evidence_id}")
        item = by_id[evidence_id]
        if expected_tool:
            require(item["tool"] == expected_tool, f"{evidence_id} must reference {expected_tool}")
        return item

    findings = [report.leading_hypothesis, *report.supporting_findings, *report.contradicting_findings]
    for index, finding in enumerate(findings):
        if index != 0 or report.assessment == "likely_cause_identified":
            require(bool(finding.evidence_ids), "a finding has no citations")
        for evidence_id in finding.evidence_ids:
            lookup(evidence_id)

    if report.assessment == "insufficient_evidence":
        require(report.confidence == "low", "insufficient evidence requires low confidence")
    if report.confidence == "high":
        require(not report.leading_hypothesis.verification_needed,
                "high confidence is not allowed with unverified hypothesis claims")
        signals = {item["tool"] for item in evidence if item["data"]
                   and item["tool"] in {"query_metrics", "search_logs", "get_trace"}}
        require(len(signals) >= 2, "high confidence requires multiple collected signal types")

    for measurement in report.metric_measurements:
        validate_metric(measurement, lookup(measurement.evidence_id, "query_metrics"))

    expected_configuration = set()
    for item in evidence:
        if item.get("metric") == "pool_limit" and item["tool"] == "query_metrics":
            for series in item["data"]:
                version = series.get("labels", {}).get("service_version")
                for point in series["points"]:
                    value = point["value"]
                    if version is not None and type(value) in (int, float) and math.isfinite(value):
                        expected_configuration.add((item["evidence_id"], version, value))

    reported_configuration = set()
    for comparison in report.configuration_comparisons:
        require(comparison.metric == "pool_limit", "unsupported configuration metric")
        for observation in comparison.observations:
            item = lookup(observation.evidence_id, "query_metrics")
            require(item.get("metric") == comparison.metric, "configuration metric does not match its citation")
            fact = (observation.evidence_id, observation.service_version, observation.value)
            require(fact in expected_configuration, "configuration value/version is absent from its evidence")
            reported_configuration.add(fact)
    require(expected_configuration <= reported_configuration, "observed configuration values were omitted")

    expected_traces = {item["evidence_id"] for item in evidence if item["tool"] == "get_trace" and item["data"]}
    reported_traces = set()
    for breakdown in report.trace_breakdowns:
        item = lookup(breakdown.evidence_id, "get_trace")
        require(breakdown.trace_id == item["trace_id"], "trace ID does not match its evidence")
        actual_spans = {span["span_id"]: span for span in item["data"]}
        reported_ids = [span.span_id for span in breakdown.spans]
        require(len(reported_ids) == len(set(reported_ids)), "duplicate spans in a trace breakdown")
        require(set(reported_ids) == set(actual_spans), "trace breakdown omitted or invented retrieved spans")
        for span in breakdown.spans:
            actual = actual_spans[span.span_id]
            require(span.name == actual["name"], "span name does not match its evidence")
            require(math.isfinite(span.duration_ms) and math.isclose(
                span.duration_ms, actual["duration_ms"], rel_tol=0, abs_tol=0.01),
                "span duration differs from its evidence")
        reported_traces.add(breakdown.evidence_id)
    require(expected_traces <= reported_traces, "a retrieved trace was omitted from the breakdowns")

    for observation in report.timeline_observations:
        require(bool(observation.evidence_ids), "timeline observation has no citations")
        validate_timeline(observation, [lookup(identifier) for identifier in observation.evidence_ids])
    # Narrative interpretations and recommendations require human review.
