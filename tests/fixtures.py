from agent.schemas import IncidentReport


START = "2026-09-19T10:30:00Z"
END = "2026-09-19T10:35:00Z"
BASE = {"service": "checkout", "start": START, "end": END}
TIMESTAMP = 1789813800
TRACE_ID = "00000000000000000000000000000001"


def metric(name, values, version="v2"):
    return {
        "evidence_id": name, "tool": "query_metrics", "metric": name,
        "data": [{"labels": {"service_version": version},
                  "points": [{"timestamp": TIMESTAMP + index * 10, "value": value}
                             for index, value in enumerate(values)]}],
    }


def search():
    return {"evidence_id": "search", "tool": "find_traces",
            "data": [{"trace_id": TRACE_ID, "duration_ms": 1040}]}


def trace():
    return {
        "evidence_id": "trace", "tool": "get_trace", "trace_id": TRACE_ID,
        "data": [{"span_id": str(index), "name": name, "duration_ms": duration,
                  "start_time_unix_nano": str(TIMESTAMP * 10**9)}
                 for index, (name, duration) in enumerate([
                     ("POST /checkout", 1040.123),
                     ("db.acquire_connection", 833.456),
                     ("db.checkout_query", 205.123)])],
    }


def incident():
    pool = metric("pool_limit", [10], "v1")
    pool["data"].extend(metric("pool_limit", [2])["data"])
    return [pool, metric("request_duration_mean_seconds", [.21, .22, 1.04]),
            search(), trace()]


def report(evidence=(), *, incident_case=False):
    configuration = []
    traces = []
    for item in evidence:
        if item.get("metric") == "pool_limit":
            configuration.append({
                "metric": "pool_limit",
                "observations": [
                    {"service_version": series["labels"]["service_version"],
                     "value": value, "evidence_id": item["evidence_id"]}
                    for series in item["data"]
                    for value in sorted({p["value"] for p in series["points"]})],
            })
        if item["tool"] == "get_trace" and item["data"]:
            traces.append({
                "trace_id": item["trace_id"], "evidence_id": item["evidence_id"],
                "spans": [{key: span[key] for key in ("span_id", "name", "duration_ms")}
                          for span in item["data"]],
            })
    return IncidentReport.model_validate({
        "assessment": "likely_cause_identified" if incident_case else "insufficient_evidence",
        "metric_measurements": [],
        "configuration_comparisons": configuration,
        "trace_breakdowns": traces,
        "timeline_observations": [],
        "leading_hypothesis": {
            "statement": ("A smaller pool likely adds connection wait under this demo load."
                          if incident_case else "No incident cause established from available evidence."),
            "evidence_ids": ["pool_limit", "trace"] if incident_case else [],
        },
        "supporting_findings": ([{
            "statement": "The selected trace has 833.456 ms acquiring and 205.123 ms querying; this is not a population average.",
            "evidence_ids": ["trace"],
        }] if incident_case else []),
        "contradicting_findings": [],
        "confidence": "medium" if incident_case else "low",
        "missing_information": ["Exact incident onset and representative sampling are unavailable."],
        "recommended_next_steps": ["Review the pool setting under comparable load."],
    })
