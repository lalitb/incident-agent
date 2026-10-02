import argparse
import json
from datetime import datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from .checkpoint import REPORT_SCHEMA_VERSION, load_resume, run_lock, utc_now, write_checkpoint
from .controller import (
    check_query_allowed, initialize_state, previous_attempts, query_fingerprint,
    review_investigation, run_investigation,
)
from .gateway import ToolGateway, redact
from .llm import ModelCallError, load_environment
from .model import ReportValidationError, generate_report
from .run_records import model_usage, save_json
from .verification import close_pending_checks


RUNS_DIRECTORY = Path(__file__).resolve().parents[1] / "runs"
DEFAULT_QUESTION = "What explains the change in checkout latency during this time window?"


def new_run(question, service, start, end, mode):
    state = {
        "mode": mode, "status": "collecting", "phase": "collecting", "stop_reason": None,
        "report_status": "not_requested", "remediation_status": "not_requested",
    }
    initialize_state(state)
    if mode != "adaptive":
        state["limits"].update(decisions=0, tool_calls=10 if mode == "fixed" else 0,
                               query_retries=0, review_calls=0, evidence_lookups=0)
    return {
        "run_id": uuid4().hex, "created_at": utc_now(), "question": redact(question)[0],
        "service": service, "window": {"start": start, "end": end},
        "evidence": [], "collection_errors": [], "calls": [], "state": state, "report": None,
    }


def collect_fixed(collect):
    for metric in ("request_rate", "request_duration_mean_seconds", "connection_wait_mean_seconds",
                   "pool_limit", "pool_utilization"):
        collect("query_metrics", metric=metric)
    collect("search_logs", contains="Checkout started", limit=10)
    search = collect("find_traces", min_duration_ms=800, limit=3)
    if search:
        for match in search["data"][:3]:
            collect("get_trace", trace_id=match["trace_id"])


def execute_run(run, directory, gateway, *, collect_only=False):
    state = run["state"]
    evidence, errors, calls = run["evidence"], run["collection_errors"], run["calls"]
    base = {"service": run["service"], **run["window"]}
    started = perf_counter()
    previous_elapsed = state.get("active_elapsed_ms", 0)

    def checkpoint():
        state["active_elapsed_ms"] = round(previous_elapsed + (perf_counter() - started) * 1000, 2)
        state["wall_elapsed_ms"] = round(
            (datetime.fromisoformat(utc_now()) - datetime.fromisoformat(run["created_at"])).total_seconds() * 1000, 2,
        )
        state["model_usage"] = model_usage(state)
        write_checkpoint(directory, run)

    def collect(tool, **extra):
        arguments = {**base, **extra}
        if len(calls) >= state["limits"]["tool_calls"]:
            raise ValueError("Tool budget exhausted")
        check_query_allowed(tool, arguments, calls, state["limits"]["query_retries"])
        attempt = len(previous_attempts(tool, arguments, calls)) + 1
        call = {
            "attempt_id": f"tool-{len(calls) + 1:03d}", "tool": tool, "arguments": arguments,
            "fingerprint": query_fingerprint(tool, arguments), "attempt": attempt,
            "is_retry": attempt > 1, "status": "started", "started_at": utc_now(),
            "decision_step": state["decisions"][-1]["step"] if state["mode"] == "adaptive" else None,
        }
        calls.append(call)
        # Reserve before dispatch. A lost response is not a free execution on resume.
        checkpoint()
        response = redact(gateway.execute(tool, arguments))[0]
        if response["ok"] and any(item["evidence_id"] == response["result"]["evidence_id"] for item in evidence):
            response = {"ok": False, "elapsed_ms": response["elapsed_ms"], "error": {
                "code": "invalid_result", "message": "Tool returned a duplicate evidence ID.", "retryable": False,
            }}
        call.update(status="completed" if response["ok"] else "failed", ok=response["ok"],
                    elapsed_ms=response["elapsed_ms"], guardrails=response.get("guardrails"))
        if not response["ok"]:
            error = response["error"]
            error["retryable"] = error.get("retryable") is True
            error["classification"] = "retryable" if error["retryable"] else "permanent"
            call["error"] = error
            errors.append({"tool": tool, "attempt_id": call["attempt_id"], "error": error})
            checkpoint()
            print(f"FAILED {tool}: {error['code']} ({error['classification']})")
            return None
        result = response["result"]
        evidence.append(result)
        call["evidence_id"] = result["evidence_id"]
        checkpoint()
        print(f"Collected {tool}: {len(result['data'])} result items")
        return result

    def investigate():
        run_investigation(
            question=run["question"], base=base, evidence=evidence, collection_errors=errors,
            calls=calls, collect=collect, checkpoint=checkpoint, state=state,
        )

    print(f"\nRun directory: {directory}")
    checkpoint()
    try:
        if state["mode"] == "adaptive":
            if state["phase"] in {"collecting", "replanning"}:
                was_replanning = state["phase"] == "replanning"
                if state["stop_reason"] is None:
                    investigate()
                state["status"] = "collected"
                state["phase"] = "reporting" if was_replanning else "reviewing"
                state.setdefault("initial_stop_reason", state["stop_reason"])
                checkpoint()
            if state["phase"] == "reviewing":
                state["status"] = "reviewing"
                checkpoint()
                if state["reviews"]:
                    # A completed review may have committed just before interruption.
                    replan = state["reviews"][-1].get("replanned", False)
                else:
                    replan = review_investigation(
                        question=run["question"], base=base, evidence=evidence, collection_errors=errors,
                        calls=calls, checkpoint=checkpoint, state=state,
                    )
                if replan:
                    state.update(phase="replanning", status="collecting", stop_reason=None)
                    checkpoint()
                    investigate()
                state.update(phase="reporting", status="collected")
                checkpoint()
        elif state["mode"] == "fixed":
            collect_fixed(collect)
            state.update(status="collected", phase="reporting", stop_reason="fixed_sequence_completed")
            checkpoint()

        if collect_only:
            state["phase"] = "done"
            checkpoint()
            print("Evidence saved. No LLM request made.")
            return
        if state["mode"] != "adaptive" and not any(item["data"] for item in evidence):
            state.update(status="no_evidence", phase="done")
            checkpoint()
            print("No evidence found. Check the investigation window and backend retention.")
            return

        close_pending_checks(
            state, f"Collection ended ({state['stop_reason']}); this check could not be resolved within the original limits.",
        )
        if state["stop_reason"] not in {"model_finished", "fixed_sequence_completed", "saved_evidence_loaded"}:
            if not any(item["tool"] == "controller" for item in errors):
                errors.append({"tool": "controller", "error": {
                    "code": state["stop_reason"],
                    "message": "Collection stopped at a controller limit. Use only collected evidence; uncertainty remains.",
                }})
        if len(state["report_calls"]) >= state["limits"]["report_calls"]:
            raise ValueError("Report-call budget exhausted")
        state.update(status="reporting", phase="reporting", report_status="requested")
        report_entry = {"status": "requested"}
        state["report_calls"].append(report_entry)
        checkpoint()
        print("\nRequesting structured diagnosis from configured LLM...")

        def report_progress(metadata):
            report_entry["model_call"] = metadata
            state["report_model_call"] = metadata
            checkpoint()

        report, metadata = generate_report(
            run["question"], evidence, errors, on_progress=report_progress,
            verification_checks=list(state["verification_checks"].values()),
        )
        report_entry.update(status="completed", model_call=metadata)
        run["report"] = {
            "schema_version": REPORT_SCHEMA_VERSION, "run_id": run["run_id"], "question": run["question"],
            "report": report.model_dump(), "model_call": metadata,
        }
        state.update(report_model_call=metadata, status="completed", phase="done", report_status="completed")
        checkpoint()
        print("\nIncident report:")
        print(json.dumps(redact(run["report"]["report"])[0], indent=2))
        print("\nRecorded model usage:")
        print(json.dumps(state["model_usage"], indent=2))
    except (Exception, KeyboardInterrupt) as exc:
        failed_stage = state["status"]
        state["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        state["failure"] = {"stage": failed_stage, "type": type(exc).__name__}
        if state["stop_reason"] is None:
            state["stop_reason"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "collection_error"
        if failed_stage == "reporting":
            metadata = getattr(exc, "metadata", state.get("report_model_call"))
            state.update(report_status="failed", report_model_call=metadata)
            state["report_calls"][-1].update(status=state["status"], model_call=metadata)
        if isinstance(exc, ValueError):
            state["failure"]["detail"] = redact(str(exc))[0][:2000]
            print(f"Validation detail: {state['failure']['detail']}")
        if isinstance(exc, ModelCallError):
            state["failure"]["detail"] = str(exc)
            print(f"Provider error: {exc}")
        if isinstance(exc, ReportValidationError):
            save_json(directory / "report_rejected.json", {
                "schema_version": REPORT_SCHEMA_VERSION, "report": exc.report.model_dump(),
                "validation_error": exc.detail, "model_call": exc.metadata,
            })
        checkpoint()
        raise SystemExit(
            f"Run {state['status']} during {failed_stage}: {type(exc).__name__}. Saved progress: {directory}"
        ) from None


def main(argv=None):
    load_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="Timezone-aware start; required for collection")
    parser.add_argument("--end", help="Timezone-aware end; required for collection")
    parser.add_argument("--service", help="Investigated service (default: checkout)")
    parser.add_argument("--question", help="Investigation question")
    parser.add_argument("--collect-only", action="store_true", help="Fixed collection without model calls")
    parser.add_argument("--adaptive", action="store_true", help="Bounded model-directed collection")
    parser.add_argument("--evidence-file", type=Path, help="Separate report-only run on saved evidence")
    parser.add_argument("--resume", type=Path, help="Resume a compatible unfinished adaptive run")
    args = parser.parse_args(argv)
    if args.resume:
        if any((args.start, args.end, args.service, args.question, args.adaptive, args.collect_only, args.evidence_file)):
            parser.error("--resume restores the original scope and mode; do not combine it with other investigation flags")
        try:
            with run_lock(args.resume):
                run = load_resume(args.resume)
                gateway = ToolGateway(service=run["service"], **run["window"])
                execute_run(run, args.resume, gateway)
        except (OSError, ValueError) as exc:
            parser.error(redact(str(exc))[0][:2000])
        return
    if args.adaptive and args.collect_only:
        parser.error("--adaptive cannot be combined with --collect-only")
    if args.evidence_file and any((args.adaptive, args.collect_only, args.start, args.end, args.service, args.question)):
        parser.error("--evidence-file uses its saved question/window; do not combine it with collection or scope flags")
    if not args.evidence_file and (not args.start or not args.end):
        parser.error("Collection requires explicit --start and --end timestamps")

    replay = None
    try:
        if args.evidence_file:
            replay = redact(json.loads(args.evidence_file.read_text(encoding="utf-8")))[0]
            args.start, args.end = replay["window"]["start"], replay["window"]["end"]
            args.question, args.service = replay["question"], replay.get("service", "checkout")
        service = args.service or "checkout"
        gateway = ToolGateway(service=service, start=args.start, end=args.end)
    except (OSError, ValueError, KeyError):
        parser.error("Invalid evidence file or scope; use an allowed service and timezone-aware window of at most one hour")
    mode = "replay" if replay is not None else "adaptive" if args.adaptive else "fixed"
    run = new_run(args.question or DEFAULT_QUESTION, service, args.start, args.end, mode)
    if replay is not None:
        run["evidence"] = replay["evidence"]
        run["collection_errors"] = replay.get("collection_errors", [])
        run["state"].update(
            phase="reporting", status="collected", stop_reason="saved_evidence_loaded",
            source_run_id=replay.get("run_id"), verification_checks=replay.get("verification_checks", {}),
        )
    directory = RUNS_DIRECTORY / run["run_id"]
    directory.mkdir(parents=True)
    with run_lock(directory):
        execute_run(run, directory, gateway, collect_only=args.collect_only)


if __name__ == "__main__":
    main()
