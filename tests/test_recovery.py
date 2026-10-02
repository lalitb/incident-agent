import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from agent.gateway import ToolGateway
from agent.model import generate_report
from agent.schemas import MetricMeasurement
from agent.tools.common import ToolError, fetch_json
from agent.validate_report import validate_report
from agent.verification import (
    UnresolvableCheck, VerificationNeed, VerificationResolution, declare_checks, update_checks,
)
from tests import test_controller
from tests.fixtures import BASE, metric, report, search, trace
from tests.test_controller import decision, request


def metric_resolution(evidence_id="request_rate", value=1.0):
    return VerificationResolution(
        check_id="check-001", evidence_ids=[evidence_id],
        explanation="The returned bucket measures request activity; this is not evidence of a cause.",
        metric_measurements=[MetricMeasurement(
            evidence_id=evidence_id, metric="request_rate", labels={"service_version": "v2"},
            bucket_start_utc=BASE["start"], statistic="sample_mean", value=value, unit="requests/second",
        )],
    )


class RecoveryTests(unittest.TestCase):
    run_decisions = test_controller.ControllerTests.run_decisions

    def test_retryable_failure_then_success_consumes_two_slots(self):
        query = decision(request("query_metrics", metric="request_rate"))
        state, calls, _, contexts = self.run_decisions(
            [query, query, query, decision(action="finish")], outcomes=["retryable", "success"],
        )
        self.assertEqual([call["attempt"] for call in calls], [1, 2])
        self.assertEqual(contexts[1]["remaining_tool_calls"], 7)
        self.assertEqual(contexts[2]["remaining_tool_calls"], 6)
        self.assertEqual(state["decisions"][2]["status"], "rejected")
        self.assertEqual(len(contexts[2]["collection_errors"]), 1)

    def test_retry_is_bounded_even_when_second_failure_is_retryable(self):
        query = decision(request("query_metrics", metric="request_rate"))
        state, calls, _, _ = self.run_decisions(
            [query, query, query, decision(action="finish")], outcomes=["retryable", "retryable"],
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(state["decisions"][2]["status"], "rejected")

    def test_successful_empty_queries_cannot_repeat(self):
        query = decision(request("query_metrics", metric="request_rate"))
        state, calls, _, _ = self.run_decisions(
            [query, query, decision(action="finish")], outcomes=["empty"],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(state["decisions"][1]["status"], "rejected")

    def test_retry_does_not_bypass_last_tool_slot(self):
        query = decision(request("query_metrics", metric="request_rate"))
        with patch("agent.controller.MAX_TOOL_CALLS", 1):
            state, calls, _, _ = self.run_decisions([query], outcomes=["retryable"])
        self.assertEqual(state["stop_reason"], "tool_budget")
        self.assertEqual(len(calls), 1)

    def test_retry_in_invalid_batch_does_not_execute_or_spend_retry(self):
        query = request("query_metrics", metric="request_rate")
        invalid = request("get_trace", trace_id="undiscovered")
        state, calls, _, _ = self.run_decisions(
            [decision(query), decision(query, invalid), decision(query), decision(action="finish")],
            outcomes=["retryable", "success"],
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]["attempt"], 2)
        self.assertEqual(state["decisions"][1]["status"], "rejected")

    def test_http_failure_classes_never_reveal_backend_payload(self):
        for status, retryable in [(408, True), (429, True), (503, True), (401, False), (404, False), (400, False)]:
            error = HTTPError("http://private/secret", status, "private response", {},
                              io.BytesIO(b"private backend credentials"))
            with self.subTest(status=status), patch("agent.tools.common.urlopen", side_effect=error):
                with self.assertRaises(ToolError) as caught:
                    fetch_json("tempo", "/api/search")
            self.assertIs(caught.exception.retryable, retryable)
            self.assertNotIn("private", str(caught.exception))
        with patch("agent.tools.common.urlopen", side_effect=URLError("private network details")):
            with self.assertRaises(ToolError) as caught:
                fetch_json("tempo", "/api/search")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("private", str(caught.exception))

    def test_gateway_failure_class_is_structured_and_safe(self):
        for retryable in (True, False):
            with patch("agent.tools.telemetry.fetch_json", side_effect=ToolError(
                "private backend response", code="backend_unavailable", retryable=retryable,
            )):
                result = ToolGateway(**BASE).execute("query_metrics", {**BASE, "metric": "request_rate"})
            self.assertIs(result["error"]["retryable"], retryable)
            self.assertEqual(result["error"]["classification"], "retryable" if retryable else "permanent")
            self.assertNotIn("private", json.dumps(result))


class VerificationTests(unittest.TestCase):
    def checks(self, tool="query_metrics", metric_name="request_rate"):
        return declare_checks({}, [VerificationNeed(
            claim="Check the relevant observation.", tool=tool, metric=metric_name,
        )], 1)

    def test_stable_identifiers_and_resolved_history_are_preserved(self):
        checks = self.checks()
        needed = VerificationNeed(claim="Check the relevant observation.", tool="query_metrics", metric="request_rate")
        self.assertEqual(declare_checks(checks, [needed], 3), checks)
        updated = update_checks(checks, [metric_resolution()], [], [metric("request_rate", [1])], [], 1)
        self.assertEqual(updated["check-001"]["status"], "resolved")
        self.assertEqual(updated["check-001"]["evidence_ids"], ["request_rate"])
        self.assertEqual(updated["check-001"]["validation"]["structured_facts"], "passed")
        self.assertEqual(updated["check-001"]["validation"]["semantic_relevance"], "model_judgment_not_verified")
        self.assertEqual(checks["check-001"]["status"], "pending")
        with self.assertRaisesRegex(ValueError, "pending"):
            update_checks(updated, [metric_resolution()], [], [metric("request_rate", [1])], [], 1)

    def test_resolution_requires_references_explanation_target_and_real_measurements(self):
        evidence = [metric("request_rate", [1]), metric("pool_limit", [10]), search()]
        invalid = [
            {"evidence_ids": ["unknown"]},
            {"evidence_ids": ["pool_limit"]},
            {"explanation": " "},
            {"metric_measurements": []},
            {"metric_measurements": metric_resolution(value=123.0).metric_measurements},
        ]
        for change in invalid:
            resolution = metric_resolution().model_copy(update=change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                update_checks(self.checks(), [resolution], [], evidence, [], 1)
        for values in ([], [None]):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "Empty or missing"):
                update_checks(self.checks(), [metric_resolution()], [], [metric("request_rate", values)], [], 1)

    def test_trace_discovery_and_invented_spans_cannot_resolve_span_check(self):
        evidence = [search(), trace()]
        resolution = VerificationResolution(
            check_id="check-001", evidence_ids=["search"], explanation="Search is not span evidence.",
        )
        checks = self.checks("get_trace", None)
        with self.assertRaisesRegex(ValueError, "target"):
            update_checks(checks, [resolution], [], evidence, [], 1)
        resolution.evidence_ids = ["trace"]
        resolution.trace_breakdowns = report([trace()]).trace_breakdowns
        resolution.trace_breakdowns[0].spans[1].duration_ms += 20
        with self.assertRaisesRegex(ValueError, "span duration"):
            update_checks(checks, [resolution], [], evidence, [], 1)

    def test_success_is_not_automatic_resolution_but_can_be_inconclusive(self):
        checks = self.checks()
        evidence = [metric("request_rate", [1])]
        call = {"tool": "query_metrics", "arguments": {"metric": "request_rate"},
                "ok": True, "evidence_id": "request_rate", "attempt": 1}
        updated = update_checks(checks, [], [], evidence, [call], 1)
        self.assertEqual(updated["check-001"]["status"], "pending")
        updated = update_checks(checks, [], [
            UnresolvableCheck(check_id="check-001", reason="The sample is insufficient to establish a trend."),
        ], evidence, [call], 1)
        self.assertEqual(updated["check-001"]["status"], "unresolvable")

    def test_permanent_or_exhausted_failure_allows_incomplete_conclusion(self):
        unavailable = UnresolvableCheck(check_id="check-001", reason="Metric backend remained unavailable.")
        for retryable, attempts, allowed in [(False, 1, True), (True, 1, False), (True, 2, True)]:
            calls = [{"tool": "query_metrics", "arguments": {"metric": "request_rate"},
                      "ok": False, "attempt": attempts, "error": {"retryable": retryable}}]
            if allowed:
                updated = update_checks(self.checks(), [], [unavailable], [], calls, 1)
                self.assertEqual(updated["check-001"]["status"], "unresolvable")
                self.assertEqual(updated["check-001"]["reason"], unavailable.reason)
            else:
                with self.assertRaisesRegex(ValueError, "Unresolvable needs"):
                    update_checks(self.checks(), [], [unavailable], [], calls, 1)

    def test_model_cannot_drop_unresolvable_checks_from_report(self):
        checks = self.checks()
        checks["check-001"].update(status="unresolvable", reason="Telemetry permanently unavailable.")
        candidate = report()
        with patch("agent.model.generate_structured", return_value=(candidate, {})):
            result, _ = generate_report("What changed?", [], [], verification_checks=list(checks.values()))
        self.assertEqual(result.assessment, "insufficient_evidence")
        self.assertEqual(result.verification_checks[0].reason, "Telemetry permanently unavailable.")
        self.assertIn("check-001", result.leading_hypothesis.verification_needed[0])
        self.assertIn("Telemetry permanently unavailable", result.missing_information[-1])

    def test_validation_does_not_claim_to_prove_the_semantic_explanation(self):
        resolution = metric_resolution()
        resolution.explanation = "An intentionally unsupported interpretation for this contract-boundary test."
        updated = update_checks(self.checks(), [resolution], [], [metric("request_rate", [1])], [], 1)
        self.assertEqual(updated["check-001"]["validation"]["semantic_relevance"], "model_judgment_not_verified")

    def test_saved_report_cannot_omit_the_known_controller_history(self):
        checks = self.checks()
        checks["check-001"].update(status="unresolvable", reason="Unavailable in the saved evidence.")
        with self.assertRaisesRegex(ValueError, "history was changed or omitted"):
            validate_report(report(), [], verification_checks=list(checks.values()))
