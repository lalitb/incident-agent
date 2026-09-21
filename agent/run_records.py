import json

from .gateway import redact


def save_json(destination, value):
    sanitized, _ = redact(value)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(sanitized, indent=2, allow_nan=False),
                         encoding="utf-8")
    temporary.replace(destination)
    return sanitized


def model_usage(state):
    calls = [entry.get("model_call") for entry in state["decisions"]]
    if state.get("report_status", "not_requested") != "not_requested":
        calls.append(state.get("report_model_call"))
    totals = {}
    unknown_attempt_usage = any(
        attempt["status"] != "completed"
        for call in calls for attempt in (call or {}).get("attempts", [])
    )
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        values = [(call or {}).get("usage") or {} for call in calls]
        counts = [usage.get(name) for usage in values]
        # Partial usage is not a total. JSON null means unknown.
        totals[name] = (sum(counts) if not unknown_attempt_usage and all(type(n) is int for n in counts)
                        else None)
    attempts = sum(len((call or {}).get("attempts", [])) for call in calls)
    return {"recorded_model_calls": len(calls), "recorded_provider_attempts": attempts, **totals}
