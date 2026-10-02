import json
import os

from .gateway import redact


def save_json(destination, value):
    sanitized, _ = redact(value)
    temporary = destination.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(sanitized, indent=2, allow_nan=False))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return sanitized


def model_usage(state):
    calls = [entry.get("model_call") for entry in state["decisions"]]
    calls.extend(entry.get("model_call") for entry in state.get("reviews", []))
    if state.get("report_calls"):
        calls.extend(entry.get("model_call") for entry in state["report_calls"])
    elif state.get("report_status", "not_requested") != "not_requested":
        calls.append(state.get("report_model_call"))
    totals = {}
    available = {}
    unknown_attempt_usage = any(
        attempt["status"] != "completed"
        for call in calls for attempt in (call or {}).get("attempts", [])
    )
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        values = [(call or {}).get("usage") or {} for call in calls]
        counts = [usage.get(name) for usage in values]
        available[name] = sum(count for count in counts if type(count) is int)
        # Partial usage is not a total. JSON null means unknown.
        totals[name] = (sum(counts) if not unknown_attempt_usage and all(type(n) is int for n in counts)
                        else None)
    attempts = sum(len((call or {}).get("attempts", [])) for call in calls)
    return {"recorded_model_calls": len(calls), "recorded_provider_attempts": attempts,
            "available_token_usage": available, **totals}
