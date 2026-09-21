import copy
import json
import unittest
from unittest.mock import patch

from agent.controller import Decision, run_investigation
from agent.llm import ModelCallError, ModelResponseError
from tests.fixtures import BASE, TRACE_ID, metric, search, trace


def request(tool, **kwargs):
    return {"tool": tool, "metric": None, "contains": None, "limit": None,
            "min_duration_ms": None, "trace_id": None, **kwargs}


def decision(*requests, action="query"):
    return Decision(action=action, reason="Collect relevant evidence", requests=list(requests), verification_needed=[])


class ControllerTests(unittest.TestCase):
    def run_decisions(self, decisions, *, failed_tools=False):
        state = {"decisions": [], "stop_reason": None}
        evidence, errors, calls, snapshots, contexts = [], [], [], [], []
        sequence = iter(decisions)

        def planner(**kwargs):
            contexts.append(json.loads(kwargs["content"]))
            next_decision = next(sequence)
            if isinstance(next_decision, Exception):
                raise next_decision
            return next_decision, {"provider": "mock", "usage": None}

        def collect(tool, **arguments):
            calls.append({"tool": tool, "arguments": {**BASE, **arguments}})
            if failed_tools:
                errors.append({"tool": tool, "error": {"code": "backend_error"}})
            elif tool == "find_traces":
                evidence.append(search())
            elif tool == "get_trace":
                evidence.append(trace())
            elif tool == "query_metrics":
                evidence.append(metric(arguments["metric"], [1]))
            else:
                evidence.append({"tool": tool, "evidence_id": "logs", "data": []})

        with patch("agent.controller.generate_structured", side_effect=planner):
            run_investigation(question="Explain latency", base=BASE, evidence=evidence,
                              collection_errors=errors, calls=calls, collect=collect,
                              checkpoint=lambda: snapshots.append(copy.deepcopy(state)), state=state)
        return state, calls, snapshots, contexts

    def test_duplicate_then_discovered_trace_recovers(self):
        search_query = decision(request("find_traces", limit=2, min_duration_ms=100))
        state, calls, snapshots, contexts = self.run_decisions([
            search_query, search_query,
            decision(request("get_trace", trace_id=TRACE_ID)), decision(action="finish")])
        self.assertEqual(state["stop_reason"], "model_finished")
        self.assertEqual([c["tool"] for c in calls], ["find_traces", "get_trace"])
        self.assertEqual(contexts[2]["previous_decisions"][1]["error"], "Repeated identical query")
        self.assertEqual(contexts[2]["remaining_decisions"], 4)
        self.assertTrue(any(s["decisions"][-1]["status"] == "rejected" for s in snapshots))
        self.assertEqual(calls[1]["arguments"]["start"], BASE["start"])

    def test_six_invalid_decisions_exhaust_budget_without_tools(self):
        state, calls, _, contexts = self.run_decisions([decision()] * 6)
        self.assertEqual(state["stop_reason"], "decision_budget")
        self.assertEqual(len(contexts), 6)
        self.assertEqual(calls, [])

    def test_schema_rejection_can_recover(self):
        state, calls, _, contexts = self.run_decisions([
            ModelResponseError("Response schema validation failed: literal_error", {}),
            decision(action="finish")])
        self.assertEqual(state["stop_reason"], "model_finished")
        self.assertEqual(contexts[1]["previous_decisions"][0]["status"], "rejected")

    def test_batch_is_atomic_for_duplicate_unknown_id_and_bad_arguments(self):
        good = request("query_metrics", metric="request_rate")
        for bad in [good, request("get_trace", trace_id=TRACE_ID),
                    request("search_logs", contains="x" * 201, limit=1),
                    request("find_traces", min_duration_ms=60001, limit=1)]:
            with self.subTest(bad=bad):
                state, calls, _, _ = self.run_decisions([
                    decision(good, bad), decision(good), decision(action="finish")])
                self.assertEqual(state["decisions"][0]["status"], "rejected")
                self.assertEqual(len(calls), 1)

    def test_failed_attempts_consume_tool_budget(self):
        decisions = [decision(request("search_logs", contains=str(i), limit=1),
                              request("search_logs", contains=str(i + 1), limit=1))
                     for i in range(0, 8, 2)]
        state, calls, _, contexts = self.run_decisions(decisions, failed_tools=True)
        self.assertEqual(len(calls), 8)
        self.assertEqual(len(contexts), 4)
        self.assertEqual(state["stop_reason"], "tool_budget")
        self.assertEqual(len(contexts[-1]["collection_errors"]), 6)

    def test_failed_query_cannot_be_repeated(self):
        query = decision(request("query_metrics", metric="request_rate"))
        state, calls, _, _ = self.run_decisions([query, query, decision(action="finish")], failed_tools=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state["decisions"][1]["error"], "Repeated identical query")

    def test_over_budget_batch_rejected_then_single_call_allowed(self):
        queries = [request("search_logs", contains=str(i), limit=1) for i in range(10)]
        state, calls, _, _ = self.run_decisions([
            decision(*queries[:2]), decision(*queries[2:4]), decision(*queries[4:6]),
            decision(queries[6]), decision(*queries[7:9]), decision(queries[7])])
        self.assertEqual(len(calls), 8)
        self.assertEqual(state["decisions"][4]["status"], "rejected")
        self.assertEqual(state["stop_reason"], "tool_budget")

    def test_context_budget_stops_before_model_call(self):
        with patch("agent.controller.MAX_CONTEXT_BYTES", 1):
            state, calls, _, contexts = self.run_decisions([])
        self.assertEqual(state["stop_reason"], "context_budget")
        self.assertEqual(contexts, [])

    def test_provider_error_does_not_retry(self):
        with self.assertRaises(ModelCallError):
            self.run_decisions([ModelCallError({"usage": None})])
