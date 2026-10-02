"""Deterministic, explicitly lossy planning context; original evidence stays on disk."""

import json
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field

from .gateway import redact
from .summarize import summarize_evidence
from .validate_report import usable_evidence


CONTEXT_VERSION = "planning-context-v1"
MAX_LOOKUP_BYTES = 8_000


class ContextLimit(ValueError):
    def __init__(self, message, manifest):
        super().__init__(message)
        self.manifest = manifest


class EvidenceLookup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_id: str
    offset: int = Field(ge=0, le=100_000)
    limit: int = Field(ge=1, le=10)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def compact_decisions(decisions):
    result = []
    for entry in decisions:
        decision = entry.get("decision", {})
        result.append({
            "step": entry["step"], "status": entry["status"],
            "action": decision.get("action"), "reason": decision.get("reason"),
            "requests": decision.get("requests", []), "error": entry.get("error"),
            "evidence_lookup": decision.get("evidence_lookup"),
            "resolved_check_ids": [
                item["check_id"] for item in decision.get("resolved_verifications", [])
            ],
        })
    return result


def clip_strings(value, limit=400):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f" [TRUNCATED: {len(value) - limit} characters omitted]"
    if isinstance(value, list):
        return [clip_strings(child, limit) for child in value]
    if isinstance(value, dict):
        return {key: clip_strings(child, limit) for key, child in value.items()}
    return value


def evidence_card(original, summary):
    card = {key: value for key, value in summary.items() if key != "data"}
    card.update(
        data_omitted=True, data_row_count=len(original["data"]),
        has_usable_data=usable_evidence(original),
        detail_notice="Data omitted from planning context, not an empty result. Use evidence_lookup.",
    )
    if original["tool"] == "query_metrics":
        buckets = [bucket for series in summary["data"] for bucket in series["buckets"]]
        minima = [bucket[3] for bucket in buckets if bucket[3] is not None]
        maxima = [bucket[5] for bucket in buckets if bucket[5] is not None]
        card["overview"] = {
            "valid_samples": sum(bucket[1] for bucket in buckets),
            "missing_samples": sum(bucket[2] for bucket in buckets),
            "minimum_across_series": min(minima) if minima else None,
            "maximum_across_series": max(maxima) if maxima else None,
            "notice": "Extrema span different series/times; they do not establish a trend or a cause.",
        }
    return card


def compact_summary(item):
    result = deepcopy(item)
    if item["tool"] == "query_metrics":
        for series in result["data"]:
            buckets = series["buckets"]
            indices = {0, len(buckets) - 1} if buckets else set()
            valid = [index for index, bucket in enumerate(buckets) if bucket[3] is not None]
            if valid:
                indices.add(min(valid, key=lambda index: buckets[index][3]))
                indices.add(max(valid, key=lambda index: buckets[index][5]))
            series["buckets"] = [buckets[index] for index in sorted(indices)]
            series["omitted_bucket_count"] = len(buckets) - len(indices)
    elif len(result["data"]) > 8:
        result["omitted_row_count"] = len(result["data"]) - 8
        result["data"] = result["data"][:7] + result["data"][-1:]
    result["data"] = clip_strings(result["data"])
    result["detail_notice"] = (
        "Compact preview: intermediate buckets/rows and long text may be omitted. "
        "Contradictions may be in omitted detail; use evidence_lookup before resolving such a check."
    )
    return result


def build_context(*, question, base, evidence, collection_errors, calls, state, max_bytes):
    summaries = summarize_evidence(evidence)
    manifest = {
        "version": CONTEXT_VERSION, "metric_bucket_seconds": 60, "limit_bytes": max_bytes,
        "summarized_evidence_ids": [item["evidence_id"] for item in evidence
                                    if item["tool"] == "query_metrics"],
        "compacted_evidence_ids": [], "omitted_data_evidence_ids": [],
        "history": "Operational decisions only; provider metadata and resolved-check facts omitted.",
        "loss_notice": "Summaries are lossy. All evidence IDs remain indexed; raw collected data is in evidence.json.",
    }
    checks = {
        identifier: {key: value for key, value in check.items()
                     if key not in {"metric_measurements", "trace_breakdowns", "validation"}}
        for identifier, check in state.get("verification_checks", {}).items()
    }
    context = {
        "question": question, "scope": base,
        "remaining_decisions": state["limits"]["decisions"] - len(state["decisions"]),
        "remaining_tool_calls": state["limits"]["tool_calls"] - len(calls),
        "remaining_evidence_lookups": state["limits"]["evidence_lookups"] - len(state.get("lookups", [])),
        "remaining_reviews": state["limits"]["review_calls"] - len(state.get("reviews", [])),
        "max_retries_per_query": state["limits"]["query_retries"],
        "evidence": summaries, "collection_errors": collection_errors,
        "previous_calls": [{key: call[key] for key in (
            "attempt_id", "tool", "arguments", "status", "ok", "attempt", "error", "evidence_id",
        ) if key in call} for call in calls],
        "previous_decisions": compact_decisions(state["decisions"]),
        "verification_checks": checks,
        "evidence_lookup_results": [entry.get("result") for entry in state.get("lookups", [])
                                    if entry.get("result") is not None],
        "context_manifest": manifest,
    }
    context, _ = redact(context)
    manifest = context["context_manifest"]
    if len(encode(context).encode("utf-8")) > max_bytes:
        context["evidence"] = [compact_summary(item) for item in context["evidence"]]
        manifest["compacted_evidence_ids"] = [item["evidence_id"] for item in evidence]

    # Largest-first omission is deterministic, not a relevance judgment that might hide a contradiction.
    by_id = {item["evidence_id"]: item for item in evidence}
    full_summaries = {item["evidence_id"]: item for item in summaries}
    ranked = sorted(range(len(evidence)), key=lambda index: (
        -len(encode(context["evidence"][index])), index,
    ))
    for index in ranked:
        if len(encode(context).encode("utf-8")) <= max_bytes:
            break
        identifier = context["evidence"][index]["evidence_id"]
        context["evidence"][index] = redact(evidence_card(by_id[identifier], full_summaries[identifier]))[0]
        manifest["omitted_data_evidence_ids"].append(identifier)

    content = encode(context)
    manifest = {**manifest, "payload_bytes": len(content.encode("utf-8"))}
    if manifest["payload_bytes"] > max_bytes:
        raise ContextLimit("Scope, evidence provenance, checks and feedback exceed the context limit", manifest)
    return content, manifest


def lookup_evidence(evidence, request):
    item = next((item for item in evidence if item["evidence_id"] == request.evidence_id), None)
    if item is None:
        raise ValueError("Evidence lookup references an unknown ID")
    summary = summarize_evidence([item])[0]
    if item["tool"] == "query_metrics":
        rows = [
            {"labels": series["labels"], "omitted_label_names": series["omitted_label_names"],
             "bucket_columns": summary["bucket_columns"], "bucket": bucket,
             "observed_values": series.get("observed_values")}
            for series in summary["data"] for bucket in series["buckets"]
        ]
    else:
        rows = summary["data"]
    if request.offset >= len(rows) and (rows or request.offset):
        raise ValueError("Evidence lookup offset is outside the result")
    result = {key: value for key, value in summary.items() if key != "data"}
    result.update(
        data=clip_strings(rows[request.offset:request.offset + request.limit], limit=2000),
        offset=request.offset, total_rows=len(rows),
        notice="Local saved evidence, not a new backend query. Metrics use 60-second buckets; long text is marked TRUNCATED.",
    )
    result, _ = redact(result)
    while result["data"]:
        result["next_offset"] = (
            request.offset + len(result["data"])
            if request.offset + len(result["data"]) < len(rows) else None
        )
        if len(encode(result).encode("utf-8")) <= MAX_LOOKUP_BYTES:
            return result
        result["data"].pop()
    if rows:
        raise ValueError("A saved evidence row exceeds the lookup byte limit; inspect evidence.json locally")
    result["next_offset"] = None
    if len(encode(result).encode("utf-8")) > MAX_LOOKUP_BYTES:
        raise ValueError("Evidence lookup metadata exceeds its byte limit")
    return result
