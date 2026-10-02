"""Mock responses derived only from supplied observations, never from the rubric.

This is a deterministic plumbing exercise, not an assessment of model quality.
"""

import copy
import json
from time import perf_counter

from agent.controller import Decision, ReviewDecision
from agent.schemas import IncidentReport


LATENCY = "request_duration_mean_seconds"
WAIT = "connection_wait_mean_seconds"


def request(tool, **arguments):
    return {"tool": tool, "metric": None, "contains": None, "limit": None,
            "min_duration_ms": None, "trace_id": None, **arguments}


def metric_facts(items):
    facts = []
    for item in items:
        if item["tool"] != "query_metrics":
            continue
        column = item["bucket_columns"].index("sample_mean")
        for series in item["data"]:
            for bucket in series["buckets"]:
                if bucket[column] is not None:
                    facts.append({
                        "evidence_id": item["evidence_id"], "metric": item["metric"],
                        "labels": series["labels"], "bucket_start_utc": bucket[0],
                        "statistic": "sample_mean", "value": float(bucket[column]), "unit": item["unit"],
                    })
    return facts


def trace_facts(items):
    return [
        {"evidence_id": item["evidence_id"], "trace_id": item["trace_id"],
         "spans": [{key: span[key] for key in ("span_id", "name", "duration_ms")} for span in item["data"]]}
        for item in items if item["tool"] == "get_trace" and item["data"]
    ]


def values(items, metric):
    return [fact["value"] for fact in metric_facts(items) if fact["metric"] == metric]


def elevated_latency(items):
    samples = values(items, LATENCY)
    return bool(samples) and max(samples) > 2 * samples[0]


def decision_for_check(check, payload):
    items, calls = payload["evidence"], payload["previous_calls"]
    targeted = [
        call for call in calls if call["tool"] == check["tool"]
        and (check["tool"] != "query_metrics" or call["arguments"]["metric"] == check["metric"])
    ]
    if targeted and (targeted[-1].get("ok") is True
                     or targeted[-1].get("error", {}).get("retryable") is False
                     or targeted[-1].get("attempt", 1) > payload["max_retries_per_query"]):
        return [], {"check_id": check["check_id"],
                    "reason": "The targeted result was empty or unavailable and no permitted retry remains."}
    if check["tool"] == "get_trace":
        searches = [item for item in items if item["tool"] == "find_traces"]
        traces = [row for item in searches for row in item["data"]]
        if searches and not traces:
            return [], {"check_id": check["check_id"], "reason": "The targeted trace search returned no matching IDs."}
        if not traces:
            requests = [request("find_traces", min_duration_ms=800, limit=1)]
            if not any(item.get("metric") == "pool_limit" for item in items):
                requests.append(request("query_metrics", metric="pool_limit"))
            return requests, None
        requests = [request("get_trace", trace_id=traces[0]["trace_id"])]
        if not any(item["tool"] == "search_logs" for item in items):
            requests.append(request("search_logs", contains="Checkout started", limit=10))
        return requests, None
    if check["tool"] == "query_metrics":
        return [request("query_metrics", metric=check["metric"])], None
    if check["tool"] == "search_logs":
        return [request("search_logs", contains="", limit=10)], None
    return [request("find_traces", min_duration_ms=0, limit=1)], None


def planning_response(payload):
    result = {"action": "finish", "reason": "Mocked planner: return the collected observations for bounded review.",
              "requests": [], "verification_needed": [], "resolved_verifications": [],
              "unresolvable_verifications": []}
    if not payload["previous_calls"]:
        result.update(action="query", requests=[
            request("query_metrics", metric=LATENCY), request("query_metrics", metric=WAIT)],
            verification_needed=[{"claim": "Compare returned request-latency buckets.",
                                  "tool": "query_metrics", "metric": LATENCY}])
        return result

    for check in payload["verification_checks"].values():
        if check["status"] != "pending":
            continue
        items = [
            item for item in payload["evidence"] if item["tool"] == check["tool"] and item["data"]
            and (check["tool"] != "query_metrics" or item["metric"] == check["metric"])
        ]
        metrics, traces = metric_facts(items), trace_facts(items)
        if items and (check["tool"] != "query_metrics" or metrics):
            result["resolved_verifications"].append({
                "check_id": check["check_id"], "evidence_ids": [item["evidence_id"] for item in items],
                "explanation": "Mocked resolution cites the returned targeted observations; causal relevance still needs human review.",
                "metric_measurements": metrics, "trace_breakdowns": traces,
            })
            continue
        requests, unavailable = decision_for_check(check, payload)
        if unavailable:
            result["unresolvable_verifications"].append(unavailable)
        else:
            result.update(action="query", requests=requests,
                          reason="Mocked planner: collect the outstanding check, retrying only a classified retryable failure.")
    return result


def review_response(payload):
    missing = None
    if elevated_latency(payload["evidence"]) and not trace_facts(payload["evidence"]):
        missing = {"claim": "Compare connection-acquisition and query-execution spans in a retrieved trace.",
                   "tool": "get_trace", "metric": None}
    return {"reason": "Mocked one-shot review: inspect a sampled mechanism when latency increased; this is not a causal score.",
            "missing_check": missing}


def reporting_response(payload):
    items = payload["evidence"]
    metrics, traces = metric_facts(items), trace_facts(items)
    configuration = [
        {"service_version": series["labels"]["service_version"], "value": value,
         "evidence_id": item["evidence_id"]}
        for item in items if item.get("metric") == "pool_limit"
        for series in item["data"] for value in series["observed_values"]
    ]
    spans = [span for trace in traces for span in trace["spans"]]
    acquisition = max((span["duration_ms"] for span in spans if span["name"] == "db.acquire_connection"), default=0)
    query = max((span["duration_ms"] for span in spans if span["name"] == "db.checkout_query"), default=0)
    latency, wait = values(items, LATENCY), values(items, WAIT)
    conflicting = bool(traces and wait and latency) and query > acquisition and max(wait) > max(latency) / 2
    likely = elevated_latency(items) and bool(traces) and not conflicting
    if not elevated_latency(items):
        statement = "No incident cause established: the returned latency samples are stable."
    elif conflicting:
        statement = "Aggregate connection waiting and the selected query-dominated trace disagree; no unique cause is established."
    elif not traces:
        statement = "Latency increased, but the sampled mechanism is unavailable; no incident cause is established."
    elif acquisition > query:
        statement = "Connection acquisition dominates the selected trace; a pool constraint is a plausible explanation, not proven causation."
    else:
        statement = "Query execution dominates the selected trace rather than connection acquisition; a slow query is a plausible explanation."
    evidence_ids = [item["evidence_id"] for item in items if item["data"]]
    missing = ["Synthetic samples do not establish exact onset, representative sampling, or proven causation."]
    missing.extend(f"Collection error for {entry['tool']}: {entry['error']['code']}."
                   for entry in payload["collection_errors"])
    return {
        "assessment": "likely_cause_identified" if likely else "insufficient_evidence",
        "metric_measurements": metrics,
        "configuration_comparisons": [{"metric": "pool_limit", "observations": configuration}] if configuration else [],
        "trace_breakdowns": traces, "timeline_observations": [],
        "leading_hypothesis": {"statement": "Mocked fixture response: " + statement, "evidence_ids": evidence_ids,
                               "verification_needed": []},
        "supporting_findings": [{
            "statement": "These structured observations were copied from synthetic telemetry; a selected trace is not a population average.",
            "evidence_ids": evidence_ids,
        }] if evidence_ids else [],
        "contradicting_findings": [{
            "statement": "Aggregate wait and sampled span dominance support different explanations.",
            "evidence_ids": [fact["evidence_id"] for fact in traces]
                            + [item["evidence_id"] for item in items if item.get("metric") == WAIT],
        }] if conflicting else [],
        "confidence": "medium" if likely else "low", "missing_information": missing,
        "recommended_next_steps": ["Human review of the observations and remaining checks; no remediation was executed."],
        "verification_checks": [],
    }


class ScriptedProvider:
    def __init__(self):
        self.inputs = []

    def __call__(self, *, instructions, content, schema, on_progress=None, max_tokens=4096):
        started = perf_counter()
        payload = json.loads(content)
        self.inputs.append({"schema": schema.__name__, "instructions": instructions, "content": copy.deepcopy(payload)})
        metadata = {
            "provider": "offline_mock", "model": "scripted-fixture-v1", "mocked": True,
            "status": "requested", "usage": None, "attempts": [], "max_output_tokens": max_tokens,
        }
        if on_progress:
            on_progress(copy.deepcopy(metadata))
        responses = {Decision: planning_response, ReviewDecision: review_response, IncidentReport: reporting_response}
        if schema not in responses:
            raise ValueError(f"Unsupported scripted schema: {schema.__name__}")
        result = schema.model_validate(responses[schema](payload))
        metadata.update(status="completed", elapsed_ms=round((perf_counter() - started) * 1000, 3))
        if on_progress:
            on_progress(copy.deepcopy(metadata))
        return result, metadata
