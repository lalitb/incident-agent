import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import diagnose
from agent.controller import ReviewDecision
from agent.verification import UnresolvableCheck, VerificationNeed
from tests.fixtures import BASE, END, START, metric, report
from tests.test_controller import decision, request
from tests.test_recovery import metric_resolution


def review(missing=False):
    return ReviewDecision(
        reason="Check request activity before concluding." if missing else "No specific actionable gap identified.",
        missing_check=VerificationNeed(claim="Verify request activity.", tool="query_metrics", metric="request_rate")
        if missing else None,
    )


def mock_response(tool, arguments):
    item = metric(arguments["metric"], [1]) if tool == "query_metrics" else {
        "tool": tool, "evidence_id": tool, "data": [],
    }
    return {"ok": True, "elapsed_ms": 1, "result": item}


def report_from_context(**kwargs):
    payload = json.loads(kwargs["content"])
    # Existing test helper needs raw pool samples; this reporter can use the supplied observed_values.
    candidate = report()
    candidate.configuration_comparisons = []
    from agent.schemas import ConfigurationComparison, TraceBreakdown

    for item in payload["evidence"]:
        if item.get("metric") == "pool_limit":
            candidate.configuration_comparisons.append(ConfigurationComparison.model_validate({
                "metric": "pool_limit", "observations": [
                    {"evidence_id": item["evidence_id"], "service_version": series["labels"]["service_version"],
                     "value": value}
                    for series in item["data"] for value in series["observed_values"]
                ],
            }))
        if item["tool"] == "get_trace" and item["data"]:
            candidate.trace_breakdowns.append(TraceBreakdown.model_validate({
                "trace_id": item["trace_id"], "evidence_id": item["evidence_id"],
                "spans": [{key: span[key] for key in ("span_id", "name", "duration_ms")} for span in item["data"]],
            }))
    return candidate, {"usage": None, "provider": "mock"}


class ReviewTests(unittest.TestCase):
    def run_cli(self, responses, gateway=mock_response):
        with tempfile.TemporaryDirectory() as directory, \
             patch("agent.diagnose.RUNS_DIRECTORY", Path(directory)), patch("agent.diagnose.load_environment"), \
             patch("agent.gateway.ToolGateway.execute", side_effect=gateway), \
             patch("agent.controller.generate_structured", side_effect=[(item, {}) for item in responses]) as planner, \
             patch("agent.model.generate_structured", side_effect=report_from_context) as reporter, \
             patch("agent.llm.completion", side_effect=AssertionError("Offline only")), \
             contextlib.redirect_stdout(io.StringIO()):
            diagnose.main(["--adaptive", "--start", START, "--end", END])
            run_directory = next(Path(directory).iterdir())
            state = json.loads((run_directory / "controller.json").read_text())
            evidence = json.loads((run_directory / "evidence.json").read_text())
            saved = json.loads((run_directory / "report.json").read_text())
            return state, evidence, saved, planner.call_count, reporter.call_count

    def test_one_review_can_replan_without_resetting_any_budget(self):
        finish = decision(action="finish")
        finish.resolved_verifications = [metric_resolution()]
        state, evidence, saved, planning_calls, report_calls = self.run_cli([
            decision(request("query_metrics", metric="request_duration_mean_seconds")),
            decision(action="finish"), review(missing=True),
            decision(request("query_metrics", metric="request_rate")), finish,
        ])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(len(state["decisions"]), 4)
        self.assertEqual([entry["step"] for entry in state["decisions"]], [1, 2, 3, 4])
        self.assertEqual(planning_calls, 5)
        self.assertEqual(report_calls, 1)
        self.assertEqual(len(evidence["calls"]), 2)
        self.assertEqual(state["model_usage"]["recorded_model_calls"], 6)
        self.assertEqual(saved["report"]["verification_checks"][0]["status"], "resolved")
        self.assertEqual(state["verification_checks"]["check-001"]["source"], "review")

    def test_review_at_budget_limit_records_unresolvable_gap_and_finalizes(self):
        with patch("agent.controller.MAX_DECISIONS", 2):
            state, evidence, saved, _, report_calls = self.run_cli([
                decision(request("query_metrics", metric="request_duration_mean_seconds")),
                decision(action="finish"), review(missing=True),
            ])
        self.assertEqual(len(state["decisions"]), 2)
        self.assertEqual(len(evidence["calls"]), 1)
        self.assertFalse(state["reviews"][0]["replanned"])
        check = saved["report"]["verification_checks"][0]
        self.assertEqual(check["status"], "unresolvable")
        self.assertIn("exhaustion", check["reason"])
        self.assertEqual(report_calls, 1)

    def test_replanning_targets_review_check_and_does_not_recurse(self):
        finish = decision(action="finish")
        finish.resolved_verifications = [metric_resolution()]
        state, evidence, _, count, _ = self.run_cli([
            decision(request("query_metrics", metric="request_duration_mean_seconds")),
            decision(action="finish"), review(missing=True),
            decision(request("query_metrics", metric="pool_limit")),
            decision(request("query_metrics", metric="request_rate")), finish,
        ])
        self.assertEqual(state["decisions"][2]["status"], "rejected")
        self.assertEqual(len(evidence["calls"]), 2)
        self.assertEqual(count, 6)
        self.assertEqual(len(state["reviews"]), 1)

    def test_replanning_exhaustion_preserves_uncertainty_without_a_second_review(self):
        state, _, saved, count, _ = self.run_cli([
            decision(request("query_metrics", metric="request_duration_mean_seconds")),
            decision(action="finish"), review(missing=True),
            decision(request("query_metrics", metric="request_rate")),
            decision(action="finish"), decision(action="finish"), decision(action="finish"),
        ])
        self.assertEqual(count, 7)
        self.assertEqual(state["stop_reason"], "decision_budget")
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(saved["report"]["verification_checks"][0]["status"], "unresolvable")
        self.assertTrue(saved["report"]["leading_hypothesis"]["verification_needed"])

    def test_no_traces_are_required_for_a_metric_check(self):
        first = decision(request("query_metrics", metric="request_rate"))
        first.verification_needed = [VerificationNeed(
            claim="Verify request activity.", tool="query_metrics", metric="request_rate",
        )]
        finish = decision(action="finish")
        finish.resolved_verifications = [metric_resolution()]
        state, evidence, _, _, _ = self.run_cli([first, finish, review()])
        self.assertEqual([call["tool"] for call in evidence["calls"]], ["query_metrics"])
        self.assertEqual(state["verification_checks"]["check-001"]["status"], "resolved")

    def test_completely_unavailable_adaptive_evidence_gets_an_honest_report(self):
        first = decision(request("query_metrics", metric="request_rate"))
        first.verification_needed = [VerificationNeed(
            claim="Verify request activity.", tool="query_metrics", metric="request_rate",
        )]
        finish = decision(action="finish")
        finish.unresolvable_verifications = [UnresolvableCheck(
            check_id="check-001", reason="The metric backend permanently rejected the request.",
        )]

        def failure(tool, arguments):
            return {"ok": False, "elapsed_ms": 1, "error": {"code": "backend_rejected", "retryable": False}}

        state, evidence, saved, _, _ = self.run_cli([first, finish, review()], gateway=failure)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(evidence["evidence"], [])
        self.assertEqual(saved["report"]["assessment"], "insufficient_evidence")
        self.assertEqual(saved["report"]["confidence"], "low")
        self.assertEqual(saved["report"]["verification_checks"][0]["status"], "unresolvable")
