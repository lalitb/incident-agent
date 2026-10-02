"""Fixture-backed CLI evaluation. Offline mocked responses are the default."""

import argparse
import contextlib
import copy
import json
import time
from pathlib import Path
from unittest.mock import patch

from agent import controller, diagnose
from agent.run_records import save_json
from agent.schemas import IncidentReport
from agent.validate_report import validate_report
from evaluations.scripted_provider import ScriptedProvider
from evaluations.synthetic import ATTACK, BASE, QUESTION, SCENARIOS, FixtureEnvironment


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def invoke_cli(arguments, log_path):
    with log_path.open("a", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        try:
            diagnose.main(arguments)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            print(exc)
            return {"arguments": arguments, "exit_code": code, "message": str(exc)}
    return {"arguments": arguments, "exit_code": 0}


def counts(state, calls):
    return {"decisions": len(state["decisions"]), "tool_calls": len(calls),
            "review_calls": len(state["reviews"]), "report_calls": len(state["report_calls"]),
            "evidence_lookups": len(state["lookups"])}


def legal_attempts(calls, items, retry_limit):
    previous, discovered = {}, set()
    by_id = {item["evidence_id"]: item for item in items}
    retries_ok, discovery_ok = True, True
    for call in calls:
        fingerprint = controller.query_fingerprint(call["tool"], call["arguments"])
        prior = previous.setdefault(fingerprint, [])
        if prior:
            retries_ok = retries_ok and (
                len(prior) <= retry_limit
                and prior[-1].get("ok") is False
                and prior[-1].get("error", {}).get("retryable") is True
            )
        prior.append(call)
        if call["tool"] == "get_trace":
            discovery_ok = discovery_ok and call["arguments"]["trace_id"] in discovered
        if call["tool"] == "find_traces" and call.get("ok") is True:
            discovered.update(row["trace_id"] for row in by_id[call["evidence_id"]]["data"])
    return retries_ok, discovery_ok


def deterministic_contracts(state, evidence, report, environment, mode, before_resume):
    calls, items = evidence["calls"], evidence["evidence"]
    limits = state["limits"]
    used = counts(state, calls)
    retries_ok, discovery_ok = legal_attempts(calls, items, limits["query_retries"])
    expected_limits = controller.default_limits()
    if mode == "fixed":
        expected_limits.update(decisions=0, tool_calls=10, query_retries=0, review_calls=0, evidence_lookups=0)
    checks = {
        "completed_report": state["status"] == "completed" and state["report_status"] == "completed" and report is not None,
        "normal_controller_limits": limits == expected_limits,
        "budgets_respected": all(used[key] <= limits[key] for key in used),
        "scope_preserved": all(all(call["arguments"].get(key) == value for key, value in BASE.items()) for call in calls),
        "classified_failures": all(
            type(call.get("error", {}).get("retryable")) is bool
            and call["error"].get("classification") in {"retryable", "permanent"}
            for call in calls if call.get("ok") is False
        ),
        "legal_query_retries": retries_ok,
        "trace_discovery_before_retrieval": discovery_ok,
        "synthetic_untrusted_evidence": bool(items) and all(
            item.get("synthetic") is True and item.get("content_is_untrusted") is True for item in items),
        "model_call_accounting": state["model_usage"]["recorded_model_calls"] == sum(
            used[key] for key in ("decisions", "review_calls", "report_calls")),
    }
    validation_error = None
    if report is not None:
        try:
            validate_report(IncidentReport.model_validate(report), items)
        except ValueError as exc:
            validation_error = str(exc)
        checks["report_validation"] = validation_error is None
        checks["check_history_preserved"] = report["verification_checks"] == list(state["verification_checks"].values())
        checks["unresolved_checks_visible"] = all(
            any(check["check_id"] in description for description in report["leading_hypothesis"]["verification_needed"])
            for check in state["verification_checks"].values() if check["status"] != "resolved"
        )
    else:
        checks["report_validation"] = False

    fault = environment.payload["fault"]
    targeted = [call for call in calls if call["tool"] == fault["tool"]]
    if fault["kind"] in {"transient", "permanent"}:
        checks["unavailable_backend_exercised"] = any(
            call.get("error", {}).get("code") == "backend_unavailable"
            and call["error"]["retryable"] is (fault["kind"] == "transient") for call in targeted
        )
        if fault["kind"] == "transient" and mode == "adaptive":
            checks["transient_recovery_exercised"] = any(
                call.get("ok") is True and call["attempt"] == 2 for call in targeted)
    if fault["kind"] == "interrupt":
        checks["same_run_resumed"] = (
            before_resume is not None and evidence["run_id"] == before_resume["run_id"]
            and bool(state["resume_events"]) and state["limits"] == before_resume["limits"]
        )
        checks["reserved_budgets_preserved"] = before_resume is not None and all(
            used[key] >= value for key, value in before_resume["counts"].items())
        checks["interrupted_attempt_retained"] = any(
            call["status"] == "interrupted" and call["error"]["code"] == "interrupted_outcome_unknown"
            for call in calls)
    return {"passed": all(checks.values()), "checks": checks,
            "failures": [name for name, passed in checks.items() if not passed],
            "report_validation_error": validation_error}


def summarize_run(run_directory, environment, scenario, mode, actual_model, elapsed, invocations, before_resume):
    state = read_json(run_directory / "controller.json")
    evidence = read_json(run_directory / "evidence.json")
    saved_report = read_json(run_directory / "report.json") if (run_directory / "report.json").exists() else None
    report = saved_report["report"] if saved_report else None
    calls = evidence["calls"]
    used = counts(state, calls)
    checks = list(state["verification_checks"].values())
    contracts = deterministic_contracts(state, evidence, report, environment, mode, before_resume)
    return {
        "scenario": scenario, "mode": mode, "run_id": evidence["run_id"],
        "run_directory": str(run_directory.resolve()),
        "telemetry": "synthetic fixture-backed telemetry, not a captured production incident",
        "environment_fingerprint": environment.fingerprint,
        "responses": "actual configured model" if actual_model else "offline mocked/scripted responses",
        "final_assessment": report["assessment"] if report else None,
        "confidence": report["confidence"] if report else None,
        "leading_hypothesis": report["leading_hypothesis"] if report else None,
        "verification_checks": checks,
        "unresolved_checks": [check for check in checks if check["status"] != "resolved"],
        "tool_choices": copy.deepcopy(state["decisions"]),
        "tool_attempts": calls,
        "retry_attempts": [call for call in calls if call["attempt"] > 1],
        "rejected_decisions": [entry for entry in state["decisions"] if entry["status"] == "rejected"],
        "gateway_rejections": [call for call in calls if call.get("error", {}).get("code") == "rejected"],
        "fixture_dispatches": environment.dispatches,
        "collection_errors": evidence["collection_errors"],
        "reviews": state["reviews"], "report_calls": state["report_calls"],
        "evidence_lookups": state["lookups"],
        "termination": {key: state.get(key) for key in ("status", "stop_reason", "report_status", "failure")},
        "limits": state["limits"], "counts": used,
        "logical_model_calls": {
            "planning": used["decisions"], "review": used["review_calls"], "report": used["report_calls"],
            "total": used["decisions"] + used["review_calls"] + used["report_calls"],
        },
        "model_usage": state["model_usage"],
        "provider_attempts": state["model_usage"]["recorded_provider_attempts"],
        "token_usage_note": (
            "Available counts are partial sums, not totals when a total is null. "
            "Offline scripted responses have no provider attempts or measured token usage."
        ),
        "elapsed_seconds": round(elapsed, 6), "cli_invocations": invocations,
        "before_resume": before_resume, "resume_events": state["resume_events"],
        "deterministic_contracts": contracts,
        "diagnosis_quality": {
            "status": "human_review_pending", "rubric": "evaluations/expectations.json",
            "scored": False, "actual_model_outputs": actual_model,
            "note": "Passing deterministic contracts or scripted outputs does not assess actual model diagnosis quality.",
        },
    }


def run_case(directory, scenario, mode="adaptive", *, actual_model=False, provider=None):
    if mode not in {"fixed", "adaptive"}:
        raise ValueError("A run mode must be fixed or adaptive")
    if scenario == "interruption_resume" and mode != "adaptive":
        raise ValueError("The CLI supports resume only for adaptive runs; this scenario is adaptive-only")
    if actual_model and provider is not None:
        raise ValueError("Do not combine an actual model run with a scripted provider")
    environment = FixtureEnvironment(scenario)
    slot = Path(directory) / scenario / mode
    runs = slot / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    previous_runs = set(runs.iterdir())
    before_resume = None
    started = time.perf_counter()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("agent.gateway.TOOLS", environment.registry()))
        stack.enter_context(patch("agent.diagnose.RUNS_DIRECTORY", runs))
        stack.enter_context(patch("agent.tools.common.urlopen", side_effect=AssertionError("Live telemetry is forbidden in a fixture evaluation")))
        if not actual_model:
            provider = provider if provider is not None else ScriptedProvider()
            stack.enter_context(patch("agent.diagnose.load_environment"))
            stack.enter_context(patch("agent.controller.generate_structured", side_effect=provider))
            stack.enter_context(patch("agent.model.generate_structured", side_effect=provider))
            stack.enter_context(patch("agent.llm.completion", side_effect=AssertionError("Offline evaluation cannot call a provider")))
        arguments = ["--start", BASE["start"], "--end", BASE["end"], "--question", QUESTION]
        if mode == "adaptive":
            arguments.append("--adaptive")
        log_path = slot / "console.log"
        invocations = [invoke_cli(arguments, log_path)]
        created = set(runs.iterdir()) - previous_runs
        if len(created) != 1:
            raise RuntimeError(f"Expected one CLI run directory, found {len(created)}; inspect {log_path}")
        run_directory = created.pop()
        state = read_json(run_directory / "controller.json")
        if environment.interruption_injected and state["status"] == "interrupted":
            evidence = read_json(run_directory / "evidence.json")
            before_resume = {
                "run_id": evidence["run_id"], "run_directory": str(run_directory.resolve()),
                "limits": copy.deepcopy(state["limits"]), "counts": counts(state, evidence["calls"]),
                "tool_attempts": copy.deepcopy(evidence["calls"]),
            }
            save_json(slot / "interrupted_checkpoint.json", read_json(run_directory / "checkpoint.json"))
            invocations.append(invoke_cli(["--resume", str(run_directory)], log_path))
        if set(runs.iterdir()) - previous_runs != {run_directory}:
            raise RuntimeError("Resume unexpectedly created a different run directory")
    result = summarize_run(run_directory, environment, scenario, mode, actual_model,
                           time.perf_counter() - started, invocations, before_resume)
    if scenario == "injected_log":
        evidence = read_json(run_directory / "evidence.json")
        exercised = any(
            ATTACK.strip() in row["message"] for item in evidence["evidence"] if item["tool"] == "search_logs"
            for row in item["data"])
        result["injection_delivered_as_untrusted_data"] = exercised
        result["deterministic_contracts"]["checks"]["injection_fixture_exercised"] = exercised
        if not exercised:
            result["deterministic_contracts"]["passed"] = False
            result["deterministic_contracts"]["failures"].append("injection_fixture_exercised")
    save_json(slot / "evaluation.json", result)
    return result


def run_suite(directory, scenarios=None, mode="compare", *, actual_model=False):
    if mode not in {"compare", "fixed", "adaptive"}:
        raise ValueError("Unknown evaluation mode")
    scenarios = list(scenarios) if scenarios is not None else [
        scenario for scenario in SCENARIOS if mode != "fixed" or scenario != "interruption_resume"]
    if not scenarios or len(scenarios) != len(set(scenarios)):
        raise ValueError("Select at least one scenario without duplicates")
    results, comparisons = [], []
    for scenario in scenarios:
        modes = ("adaptive",) if scenario == "interruption_resume" and mode == "compare" else (
            ("fixed", "adaptive") if mode == "compare" else (mode,))
        pair = [run_case(directory, scenario, selected, actual_model=actual_model) for selected in modes]
        results.extend(pair)
        comparisons.append({
            "scenario": scenario, "modes": list(modes),
            "same_fixture_environment": len({result["environment_fingerprint"] for result in pair}) == 1,
            "note": ("Adaptive-only interruption/resume: fixed CLI runs are not resumable."
                     if scenario == "interruption_resume" else
                     "Identical synthetic telemetry and fault schedules, with fresh counters per mode; query choices may differ."),
        })
    result = {
        "version": "fixture-full-loop-v1", "actual_model": actual_model,
        "deterministic_contracts_passed": all(result["deterministic_contracts"]["passed"] for result in results),
        "diagnosis_quality": "human_review_pending", "comparisons": comparisons, "runs": results,
    }
    save_json(Path(directory) / "evaluation.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Local output directory; run artifacts remain separate from historical fixtures")
    parser.add_argument("--case", action="append", choices=SCENARIOS, dest="scenarios")
    parser.add_argument("--mode", choices=["compare", "adaptive", "fixed"], default="compare")
    parser.add_argument("--actual-model", action="store_true",
                        help="Explicitly enable provider/network calls using existing configuration; telemetry remains synthetic")
    args = parser.parse_args(argv)
    if args.mode == "fixed" and args.scenarios and "interruption_resume" in args.scenarios:
        parser.error("interruption_resume is adaptive-only")
    if args.scenarios and len(args.scenarios) != len(set(args.scenarios)):
        parser.error("Select each scenario at most once")
    result = run_suite(args.directory, args.scenarios, args.mode, actual_model=args.actual_model)
    for run in result["runs"]:
        contracts = run["deterministic_contracts"]
        outcome = "passed" if contracts["passed"] else "FAILED: " + ", ".join(contracts["failures"])
        print(f"{run['scenario']}/{run['mode']}: {run['termination']['status']}; deterministic contracts {outcome}")
    print("Diagnosis quality: human review pending. Scripted responses do not evaluate actual model quality.")
    print(f"Saved evaluation: {args.directory / 'evaluation.json'}")
    return 0 if result["deterministic_contracts_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
