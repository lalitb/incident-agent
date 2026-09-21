import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dotenv import load_dotenv

from agent import diagnose, llm
from agent.controller import Decision
from agent.model import ReportValidationError
from agent.remediate import review_action
from agent.run_records import model_usage
from tests.fixtures import BASE, END, START, incident, metric, report
from tests.test_controller import decision, request


class ModelTests(unittest.TestCase):
    def setUp(self):
        spacing = patch("agent.llm._next_request_at", 0)
        sleeping = patch("agent.llm.sleep")
        spacing.start()
        self.sleeping = sleeping.start()
        self.addCleanup(spacing.stop)
        self.addCleanup(sleeping.stop)

    def call(self, response=None, error=None):
        with patch("agent.llm.load_environment"), patch.dict(os.environ, {
            "LLM_MODEL": "gemini/gemini-2.5-flash-lite", "GEMINI_API_KEY": "test-key-value"
        }), patch("agent.llm.completion", return_value=response, side_effect=error) as provider:
            try:
                result = llm.generate_structured("Choose a tool", '{"question":"latency"}', Decision)
                return result, provider
            except Exception as exc:
                return exc, provider

    def test_usage_missing_and_no_provider_retry(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=decision(action="finish").model_dump_json()))],
            usage=None, model="gemini-2.5-flash-lite", id="fake-response")
        (result, metadata), provider = self.call(response)
        self.assertIsNone(metadata["usage"])
        self.assertEqual(provider.call_args.kwargs["num_retries"], 0)
        self.assertEqual(provider.call_count, 1)
        error, provider = self.call(error=RuntimeError("quota test-key-value secret=unsafe"))
        self.assertIsInstance(error, llm.ModelCallError)
        self.assertNotIn("test-key-value", str(error))
        self.assertNotIn("unsafe", json.dumps(error.metadata))
        self.assertEqual(provider.call_count, 1)

    def test_invalid_schema_diagnostic_has_no_response_values(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"secret":"unsafe"}'))],
            usage=None)
        error, _ = self.call(response)
        self.assertIsInstance(error, llm.ModelResponseError)
        self.assertNotIn("unsafe", str(error))
        self.assertIn("schema validation failed", str(error))

    def test_truncation_records_finish_reason_without_retry_or_content(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length", message=SimpleNamespace(content="private partial output"))], usage=None)
        error, provider = self.call(response)
        self.assertIsInstance(error, llm.ModelResponseError)
        self.assertEqual(error.metadata["finish_reason"], "length")
        self.assertEqual(error.metadata["max_output_tokens"], 4096)
        self.assertIn("output-token limit", error.detail)
        self.assertNotIn("private partial output", json.dumps(error.metadata))
        self.assertEqual(provider.call_count, 1)

    def test_payment_required_is_reported_without_retry(self):
        class APIError(Exception):
            status_code = 402

        error, provider = self.call(error=APIError("private provider payload"))
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(error.metadata["http_status"], 402)
        self.assertIn("provider requires payment", str(error))
        self.assertNotIn("private provider payload", str(error))

    def test_openrouter_routing_and_local_schema_bounds(self):
        for reason, valid in [("Enough evidence", True), ("x" * 601, False)]:
            candidate = decision(action="finish").model_dump()
            candidate["reason"] = reason
            response = SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=json.dumps(candidate)))], usage=None)
            with patch("agent.llm.load_environment"), patch.dict(os.environ, {
                "LLM_MODEL": "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
                "OPENROUTER_API_KEY": "fake-openrouter-key"
            }), patch("agent.llm.completion", return_value=response) as provider:
                if valid:
                    result, metadata = llm.generate_structured("Choose a tool", '{}', Decision)
                    self.assertEqual(metadata["provider"], "openrouter")
                else:
                    with self.assertRaises(llm.ModelResponseError):
                        llm.generate_structured("Choose a tool", '{}', Decision)
            arguments = provider.call_args.kwargs
            self.assertEqual(arguments["api_key"], "fake-openrouter-key")
            self.assertEqual(arguments["model"], "openrouter/nvidia/nemotron-3-super-120b-a12b:free")
            self.assertIs(arguments["response_format"], Decision)
            self.assertEqual(arguments["extra_body"]["provider"], {
                "require_parameters": True, "allow_fallbacks": False,
                "max_price": {"prompt": 0, "completion": 0, "request": 0},
            })
            self.assertNotIn("fake-openrouter-key", json.dumps(arguments["messages"]))

    def test_openrouter_paid_models_and_auto_router_are_rejected(self):
        for model in ("openrouter/openai/gpt-oss-120b", "openrouter/openrouter/free"):
            with patch("agent.llm.load_environment"), patch.dict(os.environ, {"LLM_MODEL": model}), \
                 patch("agent.llm.completion") as provider:
                with self.assertRaisesRegex(ValueError, "ending in :free"):
                    llm.generate_structured("Choose a tool", '{}', Decision)
                provider.assert_not_called()

    def test_moonshot_uses_its_key_and_disables_k26_thinking(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=decision(action="finish").model_dump_json()))],
            usage=None)
        with patch("agent.llm.load_environment"), patch.dict(os.environ, {
            "LLM_MODEL": "moonshot/kimi-k2.6", "MOONSHOT_API_KEY": "fake-moonshot-key"
        }), patch("agent.llm.completion", return_value=response) as provider:
            result, metadata = llm.generate_structured("Choose a tool", '{}', Decision)
        arguments = provider.call_args.kwargs
        self.assertEqual(result.action, "finish")
        self.assertEqual(metadata["provider"], "moonshot")
        self.assertEqual(arguments["model"], "moonshot/kimi-k2.6")
        self.assertEqual(arguments["api_key"], "fake-moonshot-key")
        self.assertEqual(arguments["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(arguments["timeout"], 120)
        self.assertIs(arguments["response_format"], Decision)
        self.assertNotIn("fake-moonshot-key", json.dumps(arguments["messages"]))

    def test_moonshot_k3_uses_low_reasoning_and_preserves_schema_validation(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"unexpected": "value"}'))], usage=None)
        with patch("agent.llm.load_environment"), patch.dict(os.environ, {
            "LLM_MODEL": "moonshot/kimi-k3", "MOONSHOT_API_KEY": "fake-moonshot-key"
        }), patch("agent.llm.completion", return_value=response) as provider:
            with self.assertRaises(llm.ModelResponseError):
                llm.generate_structured("Choose a tool", '{}', Decision)
        self.assertEqual(provider.call_args.kwargs["extra_body"], {"reasoning_effort": "low"})
        self.assertIs(provider.call_args.kwargs["response_format"], Decision)

    def test_embedded_instruction_cannot_add_a_shell_tool(self):
        malicious = decision(action="finish").model_dump()
        malicious.update(action="query", requests=[request("shell", contains="reveal credentials")])
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps(malicious)))], usage=None)
        error, provider = self.call(response)
        self.assertIsInstance(error, llm.ModelResponseError)
        self.assertIn("literal_error", error.detail)

    def test_credentials_are_removed_before_provider_payload(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=decision(action="finish").model_dump_json()))],
            usage=None)
        with patch("agent.llm.load_environment"), patch.dict(os.environ, {
            "LLM_MODEL": "gemini/gemini-2.5-flash-lite", "GEMINI_API_KEY": "runtime-credential-value"
        }), patch("agent.llm.completion", return_value=response) as provider:
            llm.generate_structured("Choose a tool", json.dumps({
                "question": "runtime-credential-value", "api_key": "other-secret"}), Decision)
        messages = json.dumps(provider.call_args.kwargs["messages"])
        self.assertNotIn("runtime-credential-value", messages)
        self.assertNotIn("other-secret", messages)
        json.loads(provider.call_args.kwargs["messages"][1]["content"])

    def test_dotenv_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("LLM_MODEL=from-file\nGEMINI_API_KEY=fake-file-key\n")
            with patch.dict(os.environ, {"LLM_MODEL": "from-environment"}, clear=True):
                # Exercise the adapter's override flag using a synthetic file.
                with patch("agent.llm.load_dotenv", side_effect=lambda path, **kw: load_dotenv(env_file, **kw)) as loader:
                    llm.load_environment()
                self.assertFalse(loader.call_args.kwargs["override"])
                self.assertEqual(os.environ["LLM_MODEL"], "from-environment")
                self.assertEqual(os.environ["GEMINI_API_KEY"], "fake-file-key")

    def test_usage_total_is_unknown_if_any_call_is_unknown(self):
        state = {"decisions": [{"model_call": {"usage": {"input_tokens": 10}}},
                               {"model_call": {"usage": None}}]}
        self.assertIsNone(model_usage(state)["input_tokens"])
        self.assertIsNone(model_usage({"decisions": [], "report_status": "failed"})["total_tokens"])

    def test_rate_limit_retries_are_paced_and_checkpointed(self):
        class RateLimitError(Exception):
            status_code = 429

        metadata, snapshots = {}, []
        with patch("agent.llm.perf_counter", return_value=100), patch(
            "agent.llm.completion", side_effect=[RateLimitError('"retryDelay": "75s"'), "response"]
        ) as provider:
            result = llm.request_completion(metadata, on_progress=lambda value: snapshots.append(value))
        self.assertEqual(result, "response")
        self.assertEqual(provider.call_count, 2)
        self.sleeping.assert_called_once_with(76)
        self.assertEqual(metadata["attempts"][0]["status"], "failed")
        self.assertTrue(any(s["status"] == "waiting" for s in snapshots))

    def test_rate_limit_retry_budget_and_daily_quota_stop(self):
        class RateLimitError(Exception):
            status_code = 429

        for message, attempts, reason in [
            ("slow down", 4, "retry_budget"),
            ('"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"', 1, "daily_quota"),
            ("Rate limit exceeded: free-models-per-day.", 1, "daily_quota"),
            ("quota exceeded, limit: 0", 1, "zero_quota"),
            ('"retryDelay": "600s"', 1, "retry_wait_budget"),
        ]:
            with self.subTest(reason=reason), patch("agent.llm._next_request_at", 0), patch(
                "agent.llm.completion", side_effect=RateLimitError(message)
            ) as provider:
                metadata = {}
                with self.assertRaises(RateLimitError):
                    llm.request_completion(metadata)
                self.assertEqual(provider.call_count, attempts)
                self.assertEqual(metadata["retry_stop_reason"], reason)

    def test_successful_requests_are_also_spaced(self):
        with patch("agent.llm.perf_counter", return_value=100), patch("agent.llm.completion"):
            llm.request_completion({})
            llm.request_completion({})
        self.sleeping.assert_called_once_with(30)

    def test_usage_remains_unknown_after_rate_limited_attempt(self):
        usage = {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
        state = {"decisions": [{"model_call": {"usage": usage, "attempts": [
            {"status": "failed"}, {"status": "completed"}]}}]}
        self.assertIsNone(model_usage(state)["total_tokens"])
        self.assertEqual(model_usage(state)["recorded_provider_attempts"], 2)


class RunTests(unittest.TestCase):
    def test_fixed_and_adaptive_collection_reach_real_report_validation(self):
        for adaptive in (False, True):
            collected = []

            def collect(tool, arguments):
                response = self.response(tool, arguments)
                collected.append(response["result"])
                return response

            with self.subTest(adaptive=adaptive), tempfile.TemporaryDirectory() as directory, \
                 patch("agent.diagnose.RUNS_DIRECTORY", Path(directory)), \
                 patch("agent.diagnose.load_environment"), \
                 patch("agent.gateway.ToolGateway.execute", side_effect=collect), \
                 patch("agent.controller.generate_structured", side_effect=[
                     (decision(request("query_metrics", metric="request_rate")), {}),
                     (decision(action="finish"), {})]), \
                 patch("agent.model.generate_structured", side_effect=lambda **kwargs: (report(collected), {})), \
                 contextlib.redirect_stdout(io.StringIO()):
                diagnose.main(["--start", START, "--end", END, *(["--adaptive"] if adaptive else [])])
                run_dir = next(Path(directory).iterdir())
                state = json.loads((run_dir / "controller.json").read_text())
                saved = json.loads((run_dir / "report.json").read_text())
                self.assertEqual(state["status"], "completed")
                self.assertEqual(state["report_status"], "completed")
                self.assertEqual(saved["model_call"]["prompt_version"], "structured-observations-v1")

    def test_collection_requires_explicit_window_before_creating_a_run(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                "agent.diagnose.RUNS_DIRECTORY", Path(directory)), patch(
                "agent.diagnose.load_environment"), contextlib.redirect_stderr(io.StringIO()):
            for args in [[], ["--start", START], ["--end", END],
                         ["--evidence-file", "unused.json", "--start", START]]:
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    diagnose.main(args)
                self.assertEqual(error.exception.code, 2)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_saved_evidence_replay_uses_its_window_and_real_report_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "evidence.json"
            fixture.write_text(json.dumps({"question": "Explain latency", "window": {
                "start": START, "end": END}, "evidence": incident(), "collection_errors": []}))
            with patch("agent.diagnose.RUNS_DIRECTORY", root / "runs"), \
                 patch("agent.diagnose.load_environment"), \
                 patch("agent.gateway.ToolGateway.execute", side_effect=AssertionError("No backend queries")), \
                 patch("agent.model.generate_structured", return_value=(report(incident(), incident_case=True), {})), \
                 contextlib.redirect_stdout(io.StringIO()):
                diagnose.main(["--evidence-file", str(fixture)])
            run_dir = next((root / "runs").iterdir())
            state = json.loads((run_dir / "controller.json").read_text())
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["report_status"], "completed")
            self.assertTrue((run_dir / "report.json").exists())

    def run_main(self, directory, args, gateway_result, report_result=None, report_error=None):
        output = io.StringIO()
        with patch("agent.diagnose.RUNS_DIRECTORY", Path(directory)), \
             patch("agent.diagnose.load_environment"), \
             patch("agent.diagnose.ToolGateway") as gateway, \
             patch("agent.diagnose.generate_report", return_value=report_result, side_effect=report_error) as model, \
             contextlib.redirect_stdout(output):
            gateway.return_value.execute.side_effect = gateway_result
            try:
                diagnose.main(["--start", START, "--end", END, *args])
            except SystemExit as exc:
                self.assertIsInstance(exc.code, str)
            run_directory = next(Path(directory).iterdir())
            state = json.loads((run_directory / "controller.json").read_text())
            return run_directory, state, output.getvalue(), model.call_count

    @staticmethod
    def response(tool, arguments):
        if tool == "query_metrics":
            item = metric(arguments["metric"], [1])
        else:
            item = {"evidence_id": tool, "tool": tool, "data": []}
        return {"ok": True, "elapsed_ms": 1, "result": item}

    def test_collect_only_and_fixed_diagnosis(self):
        for args, expected_calls in [(["--collect-only"], 0), ([], 1)]:
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                run_dir, state, _, calls = self.run_main(directory, args, self.response, (report(), {"usage": None}))
                self.assertEqual(calls, expected_calls)
                self.assertEqual(state["report_status"], "not_requested" if args else "completed")
                saved = json.loads((run_dir / "evidence.json").read_text())
                self.assertEqual(len(saved["calls"]), 7)
                self.assertEqual(state["stop_reason"], "fixed_sequence_completed")

    def test_empty_and_failed_collection_have_records_without_report(self):
        for result in [lambda tool, args: {"ok": True, "elapsed_ms": 1, "result": {
                "tool": tool, "evidence_id": args.get("metric", tool), "data": []}},
                lambda tool, args: {"ok": False, "elapsed_ms": 1, "error": {"code": "backend_error"}}]:
            with tempfile.TemporaryDirectory() as directory:
                run_dir, state, _, calls = self.run_main(directory, [], result)
                self.assertEqual(state["status"], "no_evidence")
                self.assertEqual(calls, 0)
                self.assertTrue((run_dir / "evidence.json").exists())

    def test_rejected_report_diagnostic_is_checkpointed_and_redacted(self):
        candidate = report()
        candidate.leading_hypothesis.statement = "password=do-not-save"
        error = ReportValidationError("Rejected report: incorrect span; password=do-not-save", candidate,
                                      {"provider": "mock", "usage": None})
        with tempfile.TemporaryDirectory() as directory:
            run_dir, state, output, calls = self.run_main(directory, [], self.response, report_error=error)
            self.assertEqual(state["report_status"], "failed")
            self.assertIn("incorrect span", state["failure"]["detail"])
            self.assertNotIn("do-not-save", output)
            self.assertNotIn("do-not-save", (run_dir / "report_rejected.json").read_text())
            self.assertFalse((run_dir / "report.json").exists())
            self.assertEqual(calls, 1)

    def test_adaptive_provider_failure_stops_and_preserves_progress(self):
        error = llm.ModelCallError({"provider": "mock", "error_type": "RateLimitError", "usage": None})
        with tempfile.TemporaryDirectory() as directory, patch(
            "agent.controller.generate_structured", side_effect=error) as planner:
            run_dir, state, _, report_calls = self.run_main(directory, ["--adaptive"], self.response)
            self.assertEqual(planner.call_count, 1)
            self.assertEqual(report_calls, 0)
            self.assertEqual(state["stop_reason"], "provider_error")
            self.assertEqual(state["decisions"][0]["status"], "failed")
            self.assertTrue((run_dir / "evidence.json").exists())

    def test_adaptive_duplicate_recovery_with_final_report(self):
        query = decision(request("query_metrics", metric="request_rate"))
        with tempfile.TemporaryDirectory() as directory, patch(
            "agent.controller.generate_structured", side_effect=[
                (query, {}), (query, {}), (decision(action="finish"), {})]):
            run_dir, state, _, calls = self.run_main(directory, ["--adaptive"], self.response,
                                                    (report(), {"usage": None}))
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["decisions"][1]["status"], "rejected")
            self.assertEqual(calls, 1)
            self.assertEqual(len(json.loads((run_dir / "evidence.json").read_text())["calls"]), 1)

    def test_remediation_is_only_a_local_simulation(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            evidence = incident()
            candidate = report(evidence, incident_case=True)
            candidate.recommended_next_steps = ["$(touch NEVER_EXECUTE); DROP TABLE checkout"]
            (run_dir / "report.json").write_text(json.dumps({"report": candidate.model_dump()}))
            (run_dir / "evidence.json").write_text(json.dumps({"evidence": evidence}))
            (run_dir / "controller.json").write_text('{}')
            with patch("subprocess.run", side_effect=AssertionError("must not execute")), \
                 patch("os.system", side_effect=AssertionError("must not execute")):
                for status in ["not_requested", "approved", "rejected"]:
                    record = review_action(run_dir, status)
                    self.assertEqual(record["status"], status)
                    self.assertTrue(record["simulation_only"])
                    self.assertEqual(record["simulated_result"] is not None, status == "approved")
            self.assertFalse((run_dir / "NEVER_EXECUTE").exists())
