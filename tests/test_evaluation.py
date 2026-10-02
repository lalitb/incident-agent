import contextlib
import inspect
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.controller import Decision, query_fingerprint
from agent.gateway import TOOLS, ToolGateway
from agent.schemas import IncidentReport
from agent.tools.common import ToolError
from agent.validate_report import validate_report
from evaluations import full_loop
from evaluations.scripted_provider import ScriptedProvider, request
from evaluations.synthetic import ATTACK, BASE, SCENARIOS, TIMESTAMP, TRACE_ID, FixtureEnvironment, fixture_payload


NORMAL_LIMITS = {"decisions": 6, "tool_calls": 8, "query_retries": 1,
                 "review_calls": 1, "report_calls": 1, "evidence_lookups": 2}
FIXED_LIMITS = {"decisions": 0, "tool_calls": 10, "query_retries": 0,
                "review_calls": 0, "report_calls": 1, "evidence_lookups": 0}


def saved_report(result):
    return full_loop.read_json(Path(result["run_directory"]) / "report.json")["report"]


class SyntheticConsistencyTests(unittest.TestCase):
    def test_spans_are_sequential_children_contained_by_the_root(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                items = fixture_payload(scenario)["evidence"]
                trace = next(item for item in items if item["tool"] == "get_trace")
                spans = {span["name"]: span for span in trace["data"]}
                root = spans["POST /checkout"]
                acquisition = spans["db.acquire_connection"]
                query = spans["db.checkout_query"]
                root_start = int(root["start_time_unix_nano"])
                root_end = root_start + round(root["duration_ms"] * 1_000_000)
                self.assertIsNone(root["parent_span_id"])
                self.assertEqual(len({span["span_id"] for span in trace["data"]}), len(trace["data"]))
                for child in (acquisition, query):
                    start = int(child["start_time_unix_nano"])
                    end = start + round(child["duration_ms"] * 1_000_000)
                    self.assertEqual(child["parent_span_id"], root["span_id"])
                    self.assertGreater(child["duration_ms"], 0)
                    self.assertGreaterEqual(start, root_start)
                    self.assertLessEqual(end, root_end)
                acquisition_end = int(acquisition["start_time_unix_nano"]) + round(acquisition["duration_ms"] * 1_000_000)
                self.assertLess(acquisition_end, int(query["start_time_unix_nano"]))
                match = next(item for item in items if item["tool"] == "find_traces")["data"][0]
                self.assertEqual(match["start_time_unix_nano"], root["start_time_unix_nano"])
                self.assertEqual(match["duration_ms"], root["duration_ms"])

    def test_slow_query_completed_throughput_fits_unchanged_pool_capacity(self):
        items = fixture_payload("slow_query_unchanged_pool")["evidence"]
        metrics = {item["metric"]: item["data"] for item in items if item["tool"] == "query_metrics"}
        pool = {point["timestamp"]: point["value"]
                for series in metrics["pool_limit"] for point in series["points"]}
        self.assertEqual(set(pool.values()), {10})
        trace = next(item for item in items if item["tool"] == "get_trace")
        query_seconds = next(span["duration_ms"] for span in trace["data"] if span["name"] == "db.checkout_query") / 1000
        rates = [point for series in metrics["request_rate"] for point in series["points"]]
        baseline = [point["value"] for point in rates if point["timestamp"] < TIMESTAMP + 130]
        degraded = [point for point in rates if point["timestamp"] >= TIMESTAMP + 130]
        self.assertTrue(baseline)
        self.assertTrue(degraded)
        for point in degraded:
            self.assertGreater(point["value"], 0)
            self.assertLess(point["value"], min(baseline))
            self.assertLessEqual(point["value"], pool[point["timestamp"]] / query_seconds)

    def test_aggregate_labels_and_versioned_gauges_align_with_startup_intervals(self):
        aggregates = ("request_rate", "request_duration_mean_seconds", "connection_wait_mean_seconds")
        gauges = ("pool_limit", "pool_in_use", "pool_utilization")
        sample_times = {TIMESTAMP + 10 + 60 * index for index in range(5)}
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                items = fixture_payload(scenario)["evidence"]
                metrics = {item["metric"]: item["data"] for item in items if item["tool"] == "query_metrics"}
                logs = next(item for item in items if item["tool"] == "search_logs")["data"]
                startup = {row["labels"]["service_version"]: int(row["timestamp_unix_nano"]) // 10**9 for row in logs}
                for name in aggregates:
                    self.assertEqual(len(metrics[name]), 1)
                    self.assertEqual(metrics[name][0]["labels"], {"service_name": BASE["service"]})
                    self.assertEqual({point["timestamp"] for point in metrics[name][0]["points"]}, sample_times)
                gauge_values = {}
                for name in gauges:
                    self.assertEqual({series["labels"]["service_version"] for series in metrics[name]}, {"v1", "v2"})
                    gauge_values[name] = {}
                    for series in metrics[name]:
                        version = series["labels"]["service_version"]
                        self.assertEqual(series["labels"]["service_name"], BASE["service"])
                        for point in series["points"]:
                            self.assertGreaterEqual(point["timestamp"], startup[version])
                            if version == "v1":
                                self.assertLess(point["timestamp"], startup["v2"])
                            gauge_values[name][version, point["timestamp"]] = point["value"]
                    self.assertEqual({timestamp for _, timestamp in gauge_values[name]}, sample_times)
                self.assertEqual(gauge_values["pool_limit"].keys(), gauge_values["pool_in_use"].keys())
                self.assertEqual(gauge_values["pool_limit"].keys(), gauge_values["pool_utilization"].keys())
                for key, limit in gauge_values["pool_limit"].items():
                    self.assertAlmostEqual(gauge_values["pool_utilization"][key], gauge_values["pool_in_use"][key] / limit)

    def test_population_wait_and_selected_trace_contradiction_remains_deliberate(self):
        items = fixture_payload("ambiguous_contradictory")["evidence"]
        waits = next(item for item in items if item.get("metric") == "connection_wait_mean_seconds")["data"]
        degraded_waits = [point["value"] for series in waits for point in series["points"]
                          if point["timestamp"] >= TIMESTAMP + 130]
        trace = next(item for item in items if item["tool"] == "get_trace")
        durations = {span["name"]: span["duration_ms"] for span in trace["data"]}
        self.assertTrue(degraded_waits)
        self.assertGreater(min(degraded_waits), .5)
        self.assertGreater(min(degraded_waits), durations["db.acquire_connection"] / 1000)
        self.assertGreater(durations["db.checkout_query"], durations["db.acquire_connection"])
        pool = next(item for item in items if item.get("metric") == "pool_limit")["data"]
        self.assertEqual({point["value"] for series in pool for point in series["points"]}, {10})


class FixtureGatewayTests(unittest.TestCase):
    def test_wrapped_signatures_and_real_gateway_scope_checks(self):
        environment = FixtureEnvironment("known_pool_exhaustion")
        registry = environment.registry()
        for name, original in TOOLS.items():
            self.assertEqual(inspect.signature(registry[name]), inspect.signature(original))
            self.assertIs(registry[name].__wrapped__, original)
        gateway = ToolGateway(**BASE)
        invalid = [
            {**BASE, "metric": "pool_limit", "service": "payments"},
            {**BASE, "metric": "pool_limit", "end": "2026-09-19T10:36:00Z"},
            {**BASE, "metric": "pool_limit", "unexpected": True},
        ]
        with patch("agent.gateway.TOOLS", registry):
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    response = gateway.execute("query_metrics", arguments)
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["error"]["code"], "rejected")
            self.assertFalse(gateway.execute("run_shell", BASE)["ok"])
        self.assertEqual(environment.dispatches, [])

    def test_tool_filters_limits_and_unknown_results_are_not_empty_fallbacks(self):
        environment = FixtureEnvironment("known_pool_exhaustion")
        gateway = ToolGateway(**BASE)
        narrow = {**BASE, "start": "2026-09-19T10:32:00Z", "end": "2026-09-19T10:34:00Z"}
        with patch("agent.gateway.TOOLS", environment.registry()):
            metrics = gateway.execute("query_metrics", {**narrow, "metric": "pool_limit"})["result"]
            self.assertTrue(all(TIMESTAMP + 120 <= point["timestamp"] <= TIMESTAMP + 240
                                for series in metrics["data"] for point in series["points"]))
            logs = gateway.execute("search_logs", {**narrow, "contains": "pool_size=2", "limit": 1})["result"]
            self.assertEqual(len(logs["data"]), 1)
            self.assertIn("pool_size=2", logs["data"][0]["message"])
            self.assertTrue(logs["content_is_untrusted"])
            self.assertTrue(logs["synthetic"])
            no_logs = gateway.execute("search_logs", {**BASE, "contains": "not a fixture log", "limit": 1})
            self.assertTrue(no_logs["ok"])
            self.assertEqual(no_logs["result"]["data"], [])
            traces = gateway.execute("find_traces", {**BASE, "min_duration_ms": 1000, "limit": 1})
            self.assertEqual(len(traces["result"]["data"]), 1)
            no_traces = gateway.execute("find_traces", {**BASE, "min_duration_ms": 1100, "limit": 1})
            self.assertTrue(no_traces["ok"])
            self.assertEqual(no_traces["result"]["data"], [])
            unknown = gateway.execute("get_trace", {**BASE, "trace_id": "2"})
            self.assertFalse(unknown["ok"])
            self.assertEqual(unknown["error"]["code"], "backend_error")
            for tool, extra in [
                ("query_metrics", {"metric": "invented"}),
                ("search_logs", {"contains": "x" * 201, "limit": 1}),
                ("search_logs", {"contains": "", "limit": 101}),
                ("find_traces", {"min_duration_ms": -1, "limit": 1}),
                ("find_traces", {"min_duration_ms": 0, "limit": 21}),
                ("get_trace", {"trace_id": "not-hex"}),
            ]:
                with self.subTest(tool=tool, extra=extra):
                    self.assertEqual(gateway.execute(tool, {**BASE, **extra})["error"]["code"],
                                     "invalid_arguments_or_data")

    def test_failure_fixtures_raise_classified_errors_and_only_transient_recovers(self):
        arguments = {**BASE, "trace_id": TRACE_ID}
        for scenario, retryable in [("transient_recovery", True), ("permanent_unavailable", False)]:
            environment = FixtureEnvironment(scenario)
            with self.subTest(scenario=scenario), self.assertRaises(ToolError) as raised:
                environment.execute("get_trace", arguments)
            self.assertEqual(raised.exception.code, "backend_unavailable")
            self.assertIs(raised.exception.retryable, retryable)
            with patch("agent.gateway.TOOLS", environment.registry()):
                response = ToolGateway(**BASE).execute("get_trace", arguments)
            self.assertIs(response["ok"], retryable)
            if retryable:
                self.assertTrue(response["result"]["data"])
            else:
                self.assertEqual(response["error"]["classification"], "permanent")
        interrupted = FixtureEnvironment("interruption_resume")
        with self.assertRaises(KeyboardInterrupt):
            interrupted.execute("get_trace", arguments)
        self.assertTrue(interrupted.execute("get_trace", arguments)["data"])


class FullLoopEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        with patch("agent.model.validate_report", wraps=validate_report) as validation:
            cls.suite = full_loop.run_suite(Path(cls.temporary.name))
        cls.validated_reports = validation.call_count
        cls.runs = {(result["scenario"], result["mode"]): result for result in cls.suite["runs"]}

    def test_all_cases_reach_real_report_validation_with_normal_limits(self):
        self.assertEqual(len(self.runs), 2 * len(SCENARIOS) - 1)
        self.assertEqual(self.validated_reports, len(self.runs))
        for key, result in self.runs.items():
            with self.subTest(case=key):
                self.assertTrue(result["deterministic_contracts"]["passed"], result["deterministic_contracts"])
                self.assertEqual(result["termination"]["status"], "completed")
                self.assertEqual(result["limits"], NORMAL_LIMITS if result["mode"] == "adaptive" else FIXED_LIMITS)
                self.assertEqual(result["counts"]["report_calls"], 1)
                self.assertEqual(result["provider_attempts"], 0)
                self.assertIsNone(result["model_usage"]["total_tokens"])
                self.assertGreater(result["elapsed_seconds"], 0)
                self.assertEqual(result["diagnosis_quality"]["status"], "human_review_pending")
                self.assertFalse(result["diagnosis_quality"]["scored"])
                self.assertFalse(result["diagnosis_quality"]["actual_model_outputs"])
                self.assertIn("mocked", result["responses"])
                self.assertEqual(result["logical_model_calls"]["total"], result["model_usage"]["recorded_model_calls"])
                saved = full_loop.read_json(Path(result["run_directory"]) / "evidence.json")
                self.assertEqual(result["tool_attempts"], saved["calls"])
        self.assertTrue(self.suite["deterministic_contracts_passed"])

    def test_fixed_and_adaptive_share_data_and_fault_schedule_not_prescribed_order(self):
        for comparison in self.suite["comparisons"]:
            self.assertTrue(comparison["same_fixture_environment"])
            if len(comparison["modes"]) != 2:
                self.assertEqual(comparison["scenario"], "interruption_resume")
                continue
            case = comparison["scenario"]
            fixed, adaptive = self.runs[case, "fixed"], self.runs[case, "adaptive"]
            self.assertEqual(fixed["environment_fingerprint"], adaptive["environment_fingerprint"])
            self.assertEqual(fixed["tool_choices"], [])
            self.assertTrue(adaptive["tool_choices"])
            observations = []
            for result in (fixed, adaptive):
                evidence = full_loop.read_json(Path(result["run_directory"]) / "evidence.json")
                by_id = {item["evidence_id"]: item for item in evidence["evidence"]}
                observations.append({
                    query_fingerprint(call["tool"], call["arguments"]): by_id[call["evidence_id"]]["data"]
                    for call in evidence["calls"] if call.get("ok") is True
                })
            common = observations[0].keys() & observations[1].keys()
            self.assertTrue(common)
            for fingerprint in common:
                self.assertEqual(observations[0][fingerprint], observations[1][fingerprint])

    def test_review_checks_have_stable_ids_and_validated_structured_facts(self):
        result = self.runs["known_pool_exhaustion", "adaptive"]
        self.assertEqual(len(result["reviews"]), 1)
        checks = {check["check_id"]: check for check in result["verification_checks"]}
        self.assertEqual(set(checks), {"check-001", "check-002"})
        self.assertEqual(checks["check-001"]["status"], "resolved")
        self.assertTrue(checks["check-001"]["metric_measurements"])
        self.assertEqual(checks["check-002"]["source"], "review")
        self.assertEqual(checks["check-002"]["status"], "resolved")
        self.assertTrue(checks["check-002"]["trace_breakdowns"])
        self.assertTrue(result["reviews"][0]["replanned"])
        self.assertEqual(saved_report(result)["verification_checks"], result["verification_checks"])

    def test_transient_retry_and_permanent_unavailable_are_distinct(self):
        recovered = self.runs["transient_recovery", "adaptive"]
        attempts = [call for call in recovered["tool_attempts"] if call["tool"] == "get_trace"]
        self.assertEqual(len(attempts), 2)
        self.assertFalse(attempts[0]["ok"])
        self.assertTrue(attempts[0]["error"]["retryable"])
        self.assertTrue(attempts[1]["ok"])
        self.assertEqual(attempts[0]["arguments"], attempts[1]["arguments"])
        self.assertEqual(len(recovered["retry_attempts"]), 1)
        self.assertEqual(recovered["unresolved_checks"], [])
        baseline = self.runs["transient_recovery", "fixed"]
        self.assertEqual(baseline["retry_attempts"], [])
        self.assertTrue(baseline["collection_errors"])
        unavailable = self.runs["permanent_unavailable", "adaptive"]
        failures = [call for call in unavailable["tool_attempts"] if call["tool"] == "get_trace"]
        self.assertEqual(len(failures), 1)
        self.assertFalse(failures[0]["error"]["retryable"])
        self.assertEqual(failures[0]["error"]["code"], "backend_unavailable")
        self.assertEqual(unavailable["retry_attempts"], [])
        self.assertEqual(unavailable["unresolved_checks"][0]["status"], "unresolvable")
        self.assertTrue(unavailable["unresolved_checks"][0]["reason"])
        self.assertIn("check-002", " ".join(saved_report(unavailable)["leading_hypothesis"]["verification_needed"]))
        self.assertEqual(saved_report(unavailable)["trace_breakdowns"], [])

    def test_normal_slow_query_and_contradiction_are_script_regressions_not_quality_scores(self):
        normal = saved_report(self.runs["normal_operation", "adaptive"])
        self.assertEqual((normal["assessment"], normal["confidence"]), ("insufficient_evidence", "low"))
        slow = saved_report(self.runs["slow_query_unchanged_pool", "adaptive"])
        self.assertEqual({observation["value"] for comparison in slow["configuration_comparisons"]
                          for observation in comparison["observations"]}, {10})
        spans = {span["name"]: span["duration_ms"] for trace in slow["trace_breakdowns"] for span in trace["spans"]}
        self.assertGreater(spans["db.checkout_query"], spans["db.acquire_connection"])
        self.assertIn("Mocked fixture response", slow["leading_hypothesis"]["statement"])
        ambiguous = saved_report(self.runs["ambiguous_contradictory", "adaptive"])
        self.assertEqual(ambiguous["assessment"], "insufficient_evidence")
        self.assertTrue(ambiguous["contradicting_findings"])
        for case in ("normal_operation", "slow_query_unchanged_pool", "ambiguous_contradictory"):
            self.assertEqual(self.runs[case, "adaptive"]["diagnosis_quality"]["status"], "human_review_pending")

    def test_resume_keeps_identity_reserved_attempts_and_remaining_budgets(self):
        result = self.runs["interruption_resume", "adaptive"]
        before = result["before_resume"]
        self.assertIsNotNone(before)
        self.assertEqual(result["run_id"], before["run_id"])
        self.assertEqual(result["run_directory"], before["run_directory"])
        self.assertEqual(result["limits"], before["limits"])
        self.assertGreaterEqual(len(result["resume_events"]), 1)
        self.assertEqual(len(result["cli_invocations"]), 2)
        self.assertNotEqual(result["cli_invocations"][0]["exit_code"], 0)
        self.assertEqual(result["cli_invocations"][1]["exit_code"], 0)
        for key, value in before["counts"].items():
            self.assertGreaterEqual(result["counts"][key], value)
        for old, current in zip(before["tool_attempts"], result["tool_attempts"]):
            self.assertEqual(old["attempt_id"], current["attempt_id"])
            self.assertEqual(old["arguments"], current["arguments"])
        interrupted = [call for call in result["tool_attempts"] if call["status"] == "interrupted"]
        self.assertEqual(len(interrupted), 1)
        self.assertTrue(interrupted[0]["error"]["retryable"])
        self.assertEqual(len(result["retry_attempts"]), 1)
        self.assertEqual(result["counts"]["report_calls"], 1)

    def test_injection_reaches_mocked_planner_and_report_without_rubric_leakage(self):
        provider = ScriptedProvider()
        with tempfile.TemporaryDirectory() as directory:
            result = full_loop.run_case(directory, "injected_log", provider=provider)
            report = saved_report(result)
        self.assertTrue(result["injection_delivered_as_untrusted_data"])
        for schema in ("Decision", "IncidentReport"):
            self.assertTrue(any(ATTACK in json.dumps(call["content"], ensure_ascii=False).replace("\\n", "\n")
                                for call in provider.inputs if call["schema"] == schema))
        serialized = json.dumps(provider.inputs)
        self.assertNotIn('"rubric"', serialized)
        self.assertNotIn('"scenario"', serialized)
        self.assertNotIn("human_review_pending", serialized)
        for scenario in SCENARIOS:
            self.assertNotIn(scenario, serialized)
        self.assertNotIn("INJECTION_SUCCESS", json.dumps(report))
        self.assertEqual(result["diagnosis_quality"]["status"], "human_review_pending")

    def test_rejected_tool_choice_is_recorded_but_never_dispatched(self):
        scripted = ScriptedProvider()
        first = True

        def provider(**kwargs):
            nonlocal first
            if kwargs["schema"] is Decision and first:
                first = False
                return Decision(action="query", reason="Mocked invalid metric for a rejection regression.",
                                requests=[request("query_metrics", metric="not_allowed")],
                                verification_needed=[]), {"mocked": True, "usage": None, "attempts": []}
            return scripted(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            result = full_loop.run_case(directory, "known_pool_exhaustion", provider=provider)
        self.assertTrue(result["deterministic_contracts"]["passed"], result["deterministic_contracts"])
        self.assertEqual(len(result["rejected_decisions"]), 1)
        self.assertIn("Metric is not allowed", result["rejected_decisions"][0]["error"])
        self.assertFalse(any(call["arguments"].get("metric") == "not_allowed" for call in result["tool_attempts"]))

    def test_invalid_report_is_a_visible_failure_not_a_success_fallback(self):
        scripted = ScriptedProvider()

        def provider(**kwargs):
            response, metadata = scripted(**kwargs)
            if kwargs["schema"] is IncidentReport:
                response.metric_measurements[0].value += 99
            return response, metadata

        with tempfile.TemporaryDirectory() as directory:
            result = full_loop.run_case(directory, "known_pool_exhaustion", provider=provider)
            rejected = Path(result["run_directory"]) / "report_rejected.json"
            self.assertTrue(rejected.exists())
        self.assertFalse(result["deterministic_contracts"]["passed"])
        self.assertEqual(result["termination"]["status"], "failed")
        self.assertIsNone(result["final_assessment"])
        self.assertEqual(result["termination"]["failure"]["type"], "ReportValidationError")

    def test_cli_defaults_offline_and_returns_failure_for_failed_contracts(self):
        for passed in (True, False):
            with patch("evaluations.full_loop.run_suite", return_value={
                "runs": [], "deterministic_contracts_passed": passed,
            }) as runner, contextlib.redirect_stdout(io.StringIO()):
                status = full_loop.main(["unused-output", "--case", "normal_operation"])
            self.assertIs(runner.call_args.kwargs["actual_model"], False)
            self.assertEqual(status, 0 if passed else 1)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "adaptive"):
                full_loop.run_case(directory, "interruption_resume", mode="fixed")
            self.assertEqual(list(Path(directory).iterdir()), [])
