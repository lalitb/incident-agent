"""Evidence contracts are deterministic; whether evidence explains a claim is not."""

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import MetricMeasurement, TraceBreakdown, VerificationCheck
from .validate_report import usable_evidence, validate_metric, validate_trace_breakdown


METRICS = {
    "request_rate", "request_duration_mean_seconds", "connection_wait_mean_seconds",
    "pool_limit", "pool_utilization",
}
MAX_CHECKS = 12


class VerificationNeed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    claim: str = Field(min_length=1, max_length=600)
    tool: Literal["query_metrics", "search_logs", "find_traces", "get_trace"]
    metric: str | None


class VerificationResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    check_id: str
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    explanation: str = Field(min_length=1, max_length=600)
    metric_measurements: list[MetricMeasurement] = Field(default_factory=list, max_length=24)
    trace_breakdowns: list[TraceBreakdown] = Field(default_factory=list, max_length=8)


class UnresolvableCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    check_id: str
    reason: str = Field(min_length=1, max_length=600)


def targets_check(check, tool, arguments, *, discovery=True):
    if discovery and {check["tool"], tool} == {"find_traces", "get_trace"}:
        # Discovery and retrieval are collection follow-ups, not interchangeable proof.
        # request_arguments separately requires every retrieved ID to have been discovered.
        return True
    return tool == check["tool"] and (
        tool != "query_metrics" or arguments.get("metric") == check["metric"]
    )


def declare_checks(checks, needs, step, source="planner"):
    result = deepcopy(checks)
    for need in needs:
        if not need.claim.strip():
            raise ValueError("Verification claim must not be blank")
        if (need.tool == "query_metrics" and need.metric not in METRICS
                or need.tool != "query_metrics" and need.metric is not None):
            raise ValueError("Invalid verification target parameters")
        existing = next((check for check in result.values() if check["claim"] == need.claim), None)
        if existing:
            if (existing["tool"], existing["metric"]) != (need.tool, need.metric):
                raise ValueError("Cannot replace an existing verification target")
            continue
        if len(result) >= MAX_CHECKS:
            raise ValueError("Verification check limit reached")
        identifier = f"check-{len(result) + 1:03d}"
        result[identifier] = VerificationCheck(
            check_id=identifier, **need.model_dump(), flagged_step=step, source=source,
        ).model_dump()
    return result


def validate_resolution(check, resolution, evidence):
    if not resolution.explanation.strip():
        raise ValueError("Resolution needs a nonblank explanation")
    by_id = {item["evidence_id"]: item for item in evidence}
    identifiers = set(resolution.evidence_ids)
    if len(identifiers) != len(resolution.evidence_ids):
        raise ValueError("Duplicate resolution evidence IDs")
    for identifier in identifiers:
        item = by_id.get(identifier)
        if item is None:
            raise ValueError("Unknown resolution evidence ID")
        if not targets_check(check, item["tool"], item, discovery=False):
            raise ValueError("Resolution evidence does not match the check target")
        if not usable_evidence(item):
            raise ValueError("Empty or missing telemetry cannot resolve a check")

    metric_ids, trace_ids = set(), set()
    for fact in resolution.metric_measurements:
        if fact.evidence_id not in identifiers or check["tool"] != "query_metrics":
            raise ValueError("Metric fact must cite this check's metric evidence")
        validate_metric(fact, by_id[fact.evidence_id])
        metric_ids.add(fact.evidence_id)
    for fact in resolution.trace_breakdowns:
        if fact.evidence_id not in identifiers or check["tool"] != "get_trace":
            raise ValueError("Span fact must cite this check's retrieved traces")
        validate_trace_breakdown(fact, by_id[fact.evidence_id])
        trace_ids.add(fact.evidence_id)
    if check["tool"] == "query_metrics" and metric_ids != identifiers:
        raise ValueError("Metric resolution requires a structured measurement for every citation")
    if check["tool"] == "get_trace" and trace_ids != identifiers:
        raise ValueError("Trace resolution requires structured span measurements for every citation")
    return {
        "references_and_target": "passed",
        "structured_facts": "passed" if metric_ids or trace_ids else "not_applicable",
        "semantic_relevance": "model_judgment_not_verified",
    }


def update_checks(checks, resolutions, unavailable, evidence, calls, retry_limit):
    result = deepcopy(checks)
    ids = [item.check_id for item in [*resolutions, *unavailable]]
    if len(ids) != len(set(ids)):
        raise ValueError("A check can transition only once per decision")

    def pending(identifier):
        if identifier not in result or result[identifier]["status"] != "pending":
            raise ValueError("Only an existing pending check may change status")
        return result[identifier]

    for resolution in resolutions:
        check = pending(resolution.check_id)
        validation = validate_resolution(check, resolution, evidence)
        check.update(**resolution.model_dump(), status="resolved", reason=None, validation=validation)
    for unavailable_check in unavailable:
        check = pending(unavailable_check.check_id)
        if not unavailable_check.reason.strip():
            raise ValueError("An unresolvable check requires a reason")
        targeted = [call for call in calls if targets_check(check, call["tool"], call["arguments"])]
        by_id = {item["evidence_id"]: item for item in evidence}
        exhausted = any(
            (call.get("ok") is False and (
                not call.get("error", {}).get("retryable", False)
                or call.get("attempt", 1) > retry_limit))
            or (call.get("ok") is True and (
                call["tool"] == check["tool"]
                or not usable_evidence(by_id[call["evidence_id"]])))
            for call in targeted
        )
        if not exhausted:
            raise ValueError("Unresolvable needs an unavailable, exhausted, empty, or inconclusive targeted result")
        check.update(status="unresolvable", reason=unavailable_check.reason)
    return result


def close_pending_checks(state, reason):
    for check in state.get("verification_checks", {}).values():
        if check["status"] == "pending":
            check.update(status="unresolvable", reason=reason)


def unresolved_descriptions(checks):
    return [
        f"{check['check_id']}: {check['claim']} ({check['status']}: "
        f"{check['reason'] or 'not yet verified'})"
        for check in checks if check["status"] != "resolved"
    ]
