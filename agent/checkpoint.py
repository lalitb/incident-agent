"""One atomic snapshot is authoritative; readable run files are derived views."""

import json
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone

from .controller import (
    PLANNER_VERSION, Decision, ToolRequest, check_query_allowed, default_limits, query_fingerprint,
    request_arguments,
)
from .gateway import ToolGateway
from .run_records import save_json
from .schemas import VerificationCheck
from .verification import VerificationResolution, validate_resolution


CHECKPOINT_VERSION = 1
RUN_SCHEMA_VERSION = "incident-run-v2"
REPORT_SCHEMA_VERSION = "structured-observations-v2"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def run_lock(directory):
    with (directory / ".run.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("This run is already active in another process") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def write_checkpoint(directory, run):
    run["checkpoint_version"] = CHECKPOINT_VERSION
    run["schema_version"] = RUN_SCHEMA_VERSION
    run["generation"] = run.get("generation", 0) + 1
    save_json(directory / "checkpoint.json", run)
    evidence_view = {key: value for key, value in run.items() if key not in {"state", "report"}}
    evidence_view["verification_checks"] = run["state"]["verification_checks"]
    save_json(directory / "evidence.json", evidence_view)
    save_json(directory / "controller.json", {
        **run["state"], "schema_version": RUN_SCHEMA_VERSION,
        "checkpoint_version": CHECKPOINT_VERSION, "generation": run["generation"],
    })
    if run.get("report") is not None:
        save_json(directory / "report.json", run["report"])


def validate_checkpoint(run):
    if run.get("checkpoint_version") != CHECKPOINT_VERSION or run.get("schema_version") != RUN_SCHEMA_VERSION:
        raise ValueError("Incompatible checkpoint version; historical runs cannot be resumed")
    state = run["state"]
    if state["mode"] != "adaptive" or state["planner_version"] != PLANNER_VERSION:
        raise ValueError("Only compatible adaptive investigations support --resume")
    if state["phase"] not in {"collecting", "reviewing", "replanning", "reporting", "done"}:
        raise ValueError("Invalid checkpoint phase")
    if not isinstance(run["run_id"], str) or not run["run_id"] or not isinstance(run["question"], str):
        raise ValueError("Invalid checkpoint identity")
    if type(run["generation"]) is not int or run["generation"] < 1:
        raise ValueError("Invalid checkpoint generation")
    if datetime.fromisoformat(run["created_at"]).tzinfo is None:
        raise ValueError("Checkpoint creation time must include a timezone")
    base = {"service": run["service"], **run["window"]}
    gateway = ToolGateway(**base)
    limits = state["limits"]
    if set(limits) != set(default_limits()) or any(
        type(limits[key]) is not int or not 0 <= limits[key] <= maximum
        for key, maximum in default_limits().items()
    ):
        raise ValueError("Incompatible or missing execution limits")
    for field, limit in (("decisions", "decisions"), ("reviews", "review_calls"),
                         ("report_calls", "report_calls"), ("lookups", "evidence_lookups")):
        if not isinstance(state[field], list) or len(state[field]) > limits[limit]:
            raise ValueError("Checkpoint model or lookup budget is inconsistent")
    if [entry["step"] for entry in state["decisions"]] != list(range(1, len(state["decisions"]) + 1)):
        raise ValueError("Checkpoint planning history is inconsistent")
    for entry in state["decisions"]:
        if entry["status"] not in {"requested", "received", "accepted", "completed", "rejected", "failed", "interrupted"}:
            raise ValueError("Invalid checkpoint decision status")
        if "decision" in entry:
            Decision.model_validate(entry["decision"])
    calls, evidence = run["calls"], run["evidence"]
    by_id = {item["evidence_id"]: item for item in evidence}
    if len(by_id) != len(evidence) or len(calls) > limits["tool_calls"]:
        raise ValueError("Checkpoint evidence or tool budget is inconsistent")
    known_evidence, previous = [], []
    for index, call in enumerate(calls, 1):
        if call["attempt_id"] != f"tool-{index:03d}" or call["status"] not in {
            "started", "completed", "failed", "interrupted",
        }:
            raise ValueError("Checkpoint tool attempt history is inconsistent")
        if any(call["arguments"].get(key) != value for key, value in base.items()):
            raise ValueError("Checkpoint query is outside the original scope")
        gateway._validate(call["tool"], call["arguments"])
        extras = {key: value for key, value in call["arguments"].items() if key not in base}
        step = call["decision_step"]
        if type(step) is not int or not 1 <= step <= len(state["decisions"]):
            raise ValueError("Checkpoint tool attempt has no reserved planning step")
        decision = state["decisions"][step - 1].get("decision", {})
        planned = [item for item in decision.get("requests", [])
                   if item["tool"] == call["tool"]
                   and {key: value for key, value in item.items() if key != "tool" and value is not None} == extras]
        if decision.get("action") != "query" or not planned:
            raise ValueError("Checkpoint tool attempt has no matching accepted request")
        request = ToolRequest.model_validate({
            "tool": call["tool"], "metric": None, "contains": None, "limit": None,
            "min_duration_ms": None, "trace_id": None, **extras,
        })
        request_arguments(request, known_evidence)
        check_query_allowed(call["tool"], call["arguments"], previous, limits["query_retries"])
        fingerprint = query_fingerprint(call["tool"], call["arguments"])
        count = sum(query_fingerprint(item["tool"], item["arguments"]) == fingerprint for item in previous)
        if call["fingerprint"] != fingerprint or call["attempt"] != count + 1:
            raise ValueError("Checkpoint query retry accounting is inconsistent")
        if any(item["decision_step"] == step and item["fingerprint"] == fingerprint for item in previous):
            raise ValueError("Checkpoint executed an identical query twice within one decision")
        if call["status"] == "completed":
            item = by_id.get(call["evidence_id"])
            if call.get("ok") is not True or item is None or item["tool"] != call["tool"]:
                raise ValueError("Completed checkpoint query lacks committed evidence")
            if item in known_evidence:
                raise ValueError("Evidence is reused by multiple attempts")
            known_evidence.append(item)
        elif call["status"] in {"failed", "interrupted"}:
            if call.get("ok") is not False or type(call["error"]["retryable"]) is not bool:
                raise ValueError("Failed checkpoint query lacks classified failure")
        elif "ok" in call or "evidence_id" in call:
            raise ValueError("In-flight checkpoint query is incorrectly marked complete")
        previous.append(call)
    if len(known_evidence) != len(evidence):
        raise ValueError("Checkpoint evidence has no committed tool attempt")
    for identifier, value in state["verification_checks"].items():
        check = VerificationCheck.model_validate(value)
        if check.check_id != identifier:
            raise ValueError("Checkpoint check identity is inconsistent")
        if check.status == "resolved":
            resolution = VerificationResolution.model_validate({
                key: value[key] for key in VerificationResolution.model_fields
            })
            validate_resolution(value, resolution, evidence)
        elif check.status == "unresolvable" and not (check.reason and check.reason.strip()):
            raise ValueError("Unresolvable checkpoint check lacks a reason")
    if (not isinstance(run["collection_errors"], list)
            or not isinstance(state["resume_events"], list)
            or not isinstance(state["context_records"], list)):
        raise ValueError("Checkpoint histories are missing")


def load_resume(directory):
    path = directory / "checkpoint.json"
    if not path.is_file():
        raise ValueError("No versioned checkpoint.json found; only current adaptive runs support --resume")
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
        validate_checkpoint(run)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Checkpoint is incomplete or inconsistent; no budgets were reset") from None
    state = run["state"]
    if state["status"] == "completed" or state["phase"] == "done" or run.get("report") is not None:
        raise ValueError("This investigation is completed and cannot be resumed")
    if len(state["report_calls"]) >= state["limits"]["report_calls"]:
        raise ValueError("Report-call budget already spent; an interrupted/failed report cannot be requested again")

    event = {"resumed_at": utc_now(), "previous_status": state["status"],
             "previous_failure": state.get("failure"),
             "interrupted_attempts": [], "abandoned_decisions": []}
    for call in run["calls"]:
        if call["status"] != "started":
            continue
        error = {
            "code": "interrupted_outcome_unknown", "classification": "retryable", "retryable": True,
            "message": "Interrupted read-only query; outcome unknown. Its tool slot remains spent.",
        }
        call.update(status="interrupted", ok=False, error=error, elapsed_ms=None)
        run["collection_errors"].append({"tool": call["tool"], "attempt_id": call["attempt_id"], "error": error})
        event["interrupted_attempts"].append(call["attempt_id"])
    for entry in state["decisions"]:
        finished = entry.get("decision", {}).get("action") == "finish" and entry["status"] == "accepted"
        if entry["status"] in {"requested", "received", "accepted"} and not finished:
            entry.update(status="interrupted", error="Unfinished decision abandoned; its planning slot remains spent.")
            event["abandoned_decisions"].append(entry["step"])
    for entry in [*state["decisions"], *state["reviews"], *state["lookups"]]:
        if entry["status"] in {"requested", "received"}:
            entry.update(status="interrupted", error="Interrupted operation is not repeated.")
        metadata = entry.get("model_call") or {}
        for attempt in metadata.get("attempts", []):
            if attempt["status"] == "requested":
                attempt["status"] = "interrupted"
        if entry["status"] == "interrupted" and metadata.get("status") in {"requested", "waiting", "received"}:
            metadata["status"] = "interrupted"
    if state["phase"] == "reviewing" and any(entry["status"] in {"interrupted", "failed"} for entry in state["reviews"]):
        run["collection_errors"].append({"tool": "review", "error": {
            "code": "interrupted_review", "message": "The review opportunity was spent before interruption/failure; it is not repeated.",
        }})
    if state.get("stop_reason") in {"interrupted", "provider_error", "collection_error"}:
        state["stop_reason"] = None
    state["resume_events"].append(event)
    state.pop("failure", None)
    state["status"] = "collecting"
    return run
