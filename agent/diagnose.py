import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .controller import run_investigation
from .gateway import ToolGateway, redact
from .llm import ModelCallError, load_environment
from .model import ReportValidationError, generate_report
from .run_records import model_usage, save_json


RUNS_DIRECTORY = Path(__file__).resolve().parents[1] / "runs"


def main(argv=None):
    load_environment()
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        help="UTC/offset start timestamp; required for collection",
    )
    parser.add_argument(
        "--end",
        help="UTC/offset end timestamp; required for collection",
    )
    parser.add_argument(
        "--question",
        default=(
            "What explains the change in checkout latency "
            "during this time window?"
        ),
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Run fixed collection without calling the LLM",
    )
    parser.add_argument(
        "--adaptive",
        action="store_true",
        help="Let the model select queries within controller limits",
    )
    parser.add_argument("--evidence-file", type=Path,
                        help="Report on saved evidence with one LLM call, no collection")

    args = parser.parse_args(argv)

    if args.adaptive and args.collect_only:
        parser.error(
            "--adaptive requires LLM calls and cannot be combined "
            "with --collect-only"
        )
    if args.evidence_file and (args.adaptive or args.collect_only):
        parser.error("--evidence-file cannot be combined with collection modes")
    if args.evidence_file and (args.start or args.end):
        parser.error("--evidence-file uses its saved window; do not supply --start or --end")
    if not args.evidence_file and (not args.start or not args.end):
        parser.error("Collection requires explicit --start and --end timestamps")

    replay = None
    if args.evidence_file:
        replay, _ = redact(json.loads(args.evidence_file.read_text()))
        args.start = replay["window"]["start"]
        args.end = replay["window"]["end"]
        args.question = replay["question"]

    try:
        gateway = ToolGateway(service="checkout", start=args.start, end=args.end)
    except ValueError:
        parser.error("Use timezone-aware start/end timestamps, at most one hour apart")

    base = {
        "service": "checkout",
        "start": args.start,
        "end": args.end,
    }

    question, _ = redact(args.question)

    run_id = uuid4().hex
    run_directory = RUNS_DIRECTORY / run_id
    run_directory.mkdir(parents=True)

    created_at = datetime.now(timezone.utc).isoformat()
    evidence = []
    collection_errors = []
    calls = []

    state = {
        "mode": "replay" if replay else "adaptive" if args.adaptive else "fixed",
        "status": "collecting",
        "stop_reason": None,
        "decisions": [],
        "report_status": "not_requested",
        "remediation_status": "not_requested",
    }

    def save(filename, value):
        return save_json(run_directory / filename, value)

    def checkpoint():
        save("evidence.json", {
            "run_id": run_id,
            "created_at": created_at,
            "question": question,
            "window": {
                "start": args.start,
                "end": args.end,
            },
            "evidence": evidence,
            "collection_errors": collection_errors,
            "calls": calls,
        })
        state["model_usage"] = model_usage(state)
        save("controller.json", state)

    def collect(tool, **extra):
        arguments = {**base, **extra}

        call = {
            "tool": tool,
            "arguments": arguments,
            "status": "started",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        calls.append(call)
        checkpoint()

        response = gateway.execute(tool, arguments)

        call.update({
            "status": "completed" if response["ok"] else "failed",
            "ok": response["ok"],
            "elapsed_ms": response["elapsed_ms"],
            "guardrails": response.get("guardrails"),
        })

        if not response["ok"]:
            call["error"] = response["error"]
            collection_errors.append({
                "tool": tool,
                "error": response["error"],
            })
            checkpoint()

            print(
                f"FAILED {tool}: "
                f"{response['error']['code']}"
            )
            return None

        result = response["result"]
        evidence.append(result)
        call["evidence_id"] = result["evidence_id"]
        checkpoint()

        print(
            f"Collected {tool}: "
            f"{len(result['data'])} result items"
        )
        return result

    def collect_fixed():
        for metric in [
            "request_rate",
            "request_duration_mean_seconds",
            "connection_wait_mean_seconds",
            "pool_limit",
            "pool_utilization",
        ]:
            collect("query_metrics", metric=metric)

        collect(
            "search_logs",
            contains="Checkout started",
            limit=10,
        )

        search = collect(
            "find_traces",
            min_duration_ms=800,
            limit=3,
        )

        if search:
            for match in search["data"]:
                collect(
                    "get_trace",
                    trace_id=match["trace_id"],
                )

        state["stop_reason"] = "fixed_sequence_completed"
        checkpoint()

    print(f"\nRun directory: runs/{run_id}")
    checkpoint()

    try:
        if replay:
            evidence.extend(replay["evidence"])
            collection_errors.extend(replay.get("collection_errors", []))
            state["stop_reason"] = "saved_evidence_loaded"
            state["source_run_id"] = replay.get("run_id")
            checkpoint()
        elif args.adaptive:
            run_investigation(
                question=question,
                base=base,
                evidence=evidence,
                collection_errors=collection_errors,
                calls=calls,
                collect=collect,
                checkpoint=checkpoint,
                state=state,
            )
        else:
            collect_fixed()

        state["status"] = "collected"
        checkpoint()

        if args.collect_only:
            print("Evidence saved. No LLM request made.")
            return

        if not any(item["data"] for item in evidence):
            state["status"] = "no_evidence"
            checkpoint()
            print(
                "No evidence found. Check the investigation window "
                "and backend retention."
            )
            return

        # Make collection limitations visible to the report generator.
        if state["stop_reason"] not in {
            "model_finished",
            "fixed_sequence_completed",
            "saved_evidence_loaded",
        }:
            collection_errors.append({
                "tool": "controller",
                "error": {
                    "code": state["stop_reason"],
                    "message": (
                        "Investigation stopped before the model "
                        "declared evidence gathering complete. "
                        "Assess conclusions using only collected evidence."
                    ),
                },
            })

        state["status"] = "reporting"
        state["report_status"] = "requested"
        checkpoint()

        print(
            "\nRequesting structured diagnosis "
            "from configured LLM..."
        )

        # Uses your existing summarization and report validation.
        def report_progress(metadata):
            state["report_model_call"] = metadata
            checkpoint()

        report, metadata = generate_report(
            question,
            evidence,
            collection_errors,
            on_progress=report_progress,
            verification_needed=list(state.get("verification_needed", {})),
        )

        saved = save("report.json", {
            "run_id": run_id,
            "question": question,
            "report": report.model_dump(),
            "model_call": metadata,
        })

        state["report_model_call"] = metadata
        state["status"] = "completed"
        state["report_status"] = "completed"
        checkpoint()

        print("\nIncident report:")
        print(json.dumps(saved["report"], indent=2))

        print("\nRecorded model usage:")
        print(json.dumps(state["model_usage"], indent=2))

    except (Exception, KeyboardInterrupt) as exc:
        # Show sanitized details for local validation failures.
        # Provider exception payloads remain hidden.
        failed_stage = state["status"]
        state["status"] = (
            "interrupted"
            if isinstance(exc, KeyboardInterrupt)
            else "failed"
        )
        state["failure"] = {
            "stage": failed_stage,
            "type": type(exc).__name__,
        }
        if state["stop_reason"] is None:
            state["stop_reason"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "collection_error"
        if failed_stage == "reporting":
            state["report_status"] = "failed"
            state["report_model_call"] = getattr(exc, "metadata", state.get("report_model_call"))
        if isinstance(exc, ValueError):
            # Only application validation messages, never raw provider payloads.
            detail = redact(str(exc))[0][:2000]
            state["failure"]["detail"] = detail
            print(f"Validation detail: {detail}")
        if isinstance(exc, ModelCallError):
            state["failure"]["detail"] = str(exc)
            print(f"Provider error: {exc}")
        if isinstance(exc, ReportValidationError):
            save("report_rejected.json", {
                "report": exc.report.model_dump(),
                "validation_error": exc.detail,
                "model_call": exc.metadata,
            })
        checkpoint()

        # Avoid printing provider exceptions that might contain payloads.
        raise SystemExit(
            f"Run {state['status']} during {failed_stage}: "
            f"{type(exc).__name__}. "
            f"Saved progress: runs/{run_id}"
        ) from None


if __name__ == "__main__":
    main()
