import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import diagnose
from agent.checkpoint import load_resume, run_lock, validate_checkpoint, write_checkpoint
from agent.run_records import save_json
from agent.remediate import review_action
from agent.verification import UnresolvableCheck, VerificationNeed
from tests.fixtures import END, START
from tests.test_controller import decision, request
from tests.test_recovery import metric_resolution
from tests.test_review import mock_response, report_from_context, review


class SimulatedProcessDeath(BaseException):
    """Skip the CLI's orderly interruption handler to test its durable boundary."""


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, options in [
            ("agent.diagnose.RUNS_DIRECTORY", {"new": self.root}),
            ("agent.diagnose.load_environment", {}),
            ("agent.llm.completion", {"side_effect": AssertionError("No live requests")}),
            ("agent.model.generate_structured", {"side_effect": report_from_context}),
        ]:
            patcher = patch(name, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.error_output = contextlib.redirect_stderr(io.StringIO())
        self.error_output.__enter__()
        self.addCleanup(self.error_output.__exit__, None, None, None)

    @property
    def directory(self):
        return next(path for path in self.root.iterdir() if path.is_dir())

    def read(self, name="checkpoint.json"):
        return json.loads((self.directory / name).read_text())

    def first_query(self):
        query = decision(request("query_metrics", metric="request_rate"))
        query.verification_needed = [VerificationNeed(
            claim="Verify request activity.", tool="query_metrics", metric="request_rate",
        )]
        return query

    def finish(self):
        result = decision(action="finish")
        result.resolved_verifications = [metric_resolution()]
        return result

    def start_interrupted_tool(self):
        with patch("agent.controller.generate_structured", return_value=(self.first_query(), {})), \
             patch("agent.gateway.ToolGateway.execute", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit):
                diagnose.main(["--adaptive", "--start", START, "--end", END, "--question", "Original question"])

    def resume(self, responses, gateway=mock_response):
        with patch("agent.controller.generate_structured", side_effect=[(item, {}) for item in responses]), \
             patch("agent.gateway.ToolGateway.execute", side_effect=gateway):
            diagnose.main(["--resume", str(self.directory)])

    def test_inflight_tool_keeps_its_slot_and_uses_only_the_original_retry(self):
        self.start_interrupted_tool()
        before = self.read()
        self.assertEqual(before["calls"][0]["status"], "started")
        self.resume([decision(request("query_metrics", metric="request_rate")), self.finish(), review()])
        after = self.read()
        validate_checkpoint(after)
        self.assertEqual(before["run_id"], after["run_id"])
        self.assertEqual(after["question"], "Original question")
        self.assertEqual(after["window"], {"start": START, "end": END})
        self.assertEqual(after["state"]["limits"], before["state"]["limits"])
        self.assertEqual([call["attempt"] for call in after["calls"]], [1, 2])
        self.assertEqual([call["status"] for call in after["calls"]], ["interrupted", "completed"])
        self.assertEqual(after["calls"][0]["error"]["code"], "interrupted_outcome_unknown")
        self.assertEqual(len(after["state"]["decisions"]), 3)
        self.assertEqual(after["state"]["decisions"][0]["status"], "interrupted")
        self.assertEqual(len(after["state"]["resume_events"]), 1)
        self.assertEqual(after["state"]["model_usage"]["recorded_model_calls"], 5)
        self.assertEqual(len(list(self.root.iterdir())), 1)
        self.assertEqual(after["state"]["verification_checks"]["check-001"]["status"], "resolved")

    def test_repeated_interruption_does_not_grant_a_third_query_attempt(self):
        self.start_interrupted_tool()

        def interrupt(tool, arguments):
            raise KeyboardInterrupt()

        with self.assertRaises(SystemExit):
            self.resume([decision(request("query_metrics", metric="request_rate"))],
                        gateway=interrupt)
        unavailable = decision(action="finish")
        unavailable.unresolvable_verifications = [UnresolvableCheck(
            check_id="check-001", reason="Both allowed executions were interrupted; telemetry outcome remains unknown.",
        )]
        self.resume([decision(request("query_metrics", metric="request_rate")), unavailable, review()])
        after = self.read()
        self.assertEqual(len(after["calls"]), 2)
        self.assertEqual(len(after["state"]["resume_events"]), 2)
        self.assertEqual(after["state"]["decisions"][2]["status"], "rejected")
        self.assertEqual(after["state"]["verification_checks"]["check-001"]["status"], "unresolvable")
        self.assertEqual(after["report"]["report"]["assessment"], "insufficient_evidence")

    def test_interrupted_planning_slot_is_not_refunded_when_limit_changes(self):
        with patch("agent.controller.MAX_DECISIONS", 1), \
             patch("agent.controller.generate_structured", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit):
                diagnose.main(["--adaptive", "--start", START, "--end", END])
        self.resume([review()])
        state = self.read()["state"]
        self.assertEqual(state["limits"]["decisions"], 1)
        self.assertEqual(len(state["decisions"]), 1)
        self.assertEqual(state["decisions"][0]["status"], "interrupted")
        self.assertEqual(state["stop_reason"], "decision_budget")
        self.assertEqual(state["model_usage"]["recorded_model_calls"], 3)

    def test_tool_interruption_does_not_erase_completed_planner_usage(self):
        metadata = {
            "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            "attempts": [{"attempt": 1, "status": "completed"}],
        }
        with patch("agent.controller.generate_structured", return_value=(self.first_query(), metadata)), \
             patch("agent.gateway.ToolGateway.execute", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit):
                diagnose.main(["--adaptive", "--start", START, "--end", END])
        self.resume([decision(request("query_metrics", metric="request_rate")), self.finish(), review()])
        state = self.read()["state"]
        self.assertEqual(state["decisions"][0]["status"], "interrupted")
        self.assertEqual(state["decisions"][0]["model_call"], metadata)
        self.assertEqual(state["model_usage"]["available_token_usage"]["total_tokens"], 12)
        self.assertIsNone(state["model_usage"]["total_tokens"])

    def test_interrupted_review_is_spent_and_never_repeated(self):
        with patch("agent.controller.generate_structured", side_effect=[
            (decision(request("query_metrics", metric="request_rate")), {}),
            (decision(action="finish"), {}), KeyboardInterrupt,
        ]), patch("agent.gateway.ToolGateway.execute", side_effect=mock_response):
            with self.assertRaises(SystemExit):
                diagnose.main(["--adaptive", "--start", START, "--end", END])
        self.assertEqual(self.read()["state"]["phase"], "reviewing")
        self.resume([])
        after = self.read()
        self.assertEqual(len(after["state"]["reviews"]), 1)
        self.assertEqual(after["state"]["reviews"][0]["status"], "interrupted")
        self.assertEqual(after["state"]["status"], "completed")
        self.assertTrue(any(error["error"]["code"] == "interrupted_review" for error in after["collection_errors"]))
        self.assertEqual(after["state"]["model_usage"]["recorded_model_calls"], 4)

    def test_completed_review_replan_survives_a_crash_before_phase_transition(self):
        crashed = False

        def checkpoint(directory, run):
            nonlocal crashed
            write_checkpoint(directory, run)
            if not crashed and run["state"]["reviews"] and run["state"]["reviews"][-1].get("replanned"):
                crashed = True
                raise SimulatedProcessDeath()

        with patch("agent.diagnose.write_checkpoint", side_effect=checkpoint), \
             patch("agent.controller.generate_structured", side_effect=[
                 (decision(request("query_metrics", metric="request_duration_mean_seconds")), {}),
                 (decision(action="finish"), {}), (review(missing=True), {}),
             ]), patch("agent.gateway.ToolGateway.execute", side_effect=mock_response):
            with self.assertRaises(SimulatedProcessDeath):
                diagnose.main(["--adaptive", "--start", START, "--end", END])
        self.assertEqual(self.read()["state"]["phase"], "reviewing")
        self.resume([decision(request("query_metrics", metric="request_rate")), self.finish()])
        after = self.read()
        self.assertEqual(len(after["state"]["reviews"]), 1)
        self.assertEqual(len(after["state"]["decisions"]), 4)
        self.assertEqual(after["state"]["verification_checks"]["check-001"]["status"], "resolved")

    def test_interrupted_report_cannot_get_another_call(self):
        with patch("agent.controller.generate_structured", side_effect=[
            (decision(request("query_metrics", metric="request_rate")), {}),
            (decision(action="finish"), {}), (review(), {}),
        ]), patch("agent.gateway.ToolGateway.execute", side_effect=mock_response), \
             patch("agent.model.generate_structured", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit):
                diagnose.main(["--adaptive", "--start", START, "--end", END])
        with self.assertRaisesRegex(ValueError, "Report-call budget already spent"):
            load_resume(self.directory)
        with patch("agent.model.generate_structured") as reporter, self.assertRaises(SystemExit) as error:
            diagnose.main(["--resume", str(self.directory)])
        self.assertEqual(error.exception.code, 2)
        reporter.assert_not_called()
        self.assertEqual(len(self.read()["state"]["report_calls"]), 1)

    def test_interrupted_report_can_be_replayed_only_as_a_separate_run(self):
        with patch("agent.controller.generate_structured", side_effect=[
            (decision(request("query_metrics", metric="request_rate")), {}),
            (decision(action="finish"), {}), (review(), {}),
        ]), patch("agent.gateway.ToolGateway.execute", side_effect=mock_response), \
             patch("agent.model.generate_structured", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit):
                diagnose.main(["--adaptive", "--start", START, "--end", END])
        original_directory = self.directory
        original = self.read()
        with self.assertRaisesRegex(ValueError, "Report-call budget already spent"):
            load_resume(original_directory)
        with patch("agent.controller.generate_structured", side_effect=AssertionError("No planning/review")), \
             patch("agent.gateway.ToolGateway.execute", side_effect=AssertionError("No collection")), \
             patch("agent.model.generate_structured", side_effect=report_from_context) as reporter:
            diagnose.main(["--evidence-file", str(original_directory / "evidence.json")])
        replay_directory = next(path for path in self.root.iterdir() if path != original_directory)
        replay = json.loads((replay_directory / "checkpoint.json").read_text())
        self.assertEqual(reporter.call_count, 1)
        self.assertEqual(replay["state"]["status"], "completed")
        self.assertEqual(replay["state"]["mode"], "replay")
        self.assertNotEqual(replay["run_id"], original["run_id"])
        self.assertEqual(replay["state"]["source_run_id"], original["run_id"])
        self.assertEqual(replay["calls"], [])
        self.assertEqual(json.loads((original_directory / "checkpoint.json").read_text()), original)

    def test_authoritative_snapshot_not_stale_readable_views_is_resumed(self):
        self.start_interrupted_tool()
        (self.directory / "controller.json").write_text('{"status":"completed"}')
        (self.directory / "evidence.json").write_text("{}")
        self.resume([decision(request("query_metrics", metric="request_rate")), self.finish(), review()])
        run = self.read()
        for name in ("controller.json", "evidence.json"):
            self.assertEqual(self.read(name)["generation"], run["generation"])
        self.assertEqual(len(self.read("evidence.json")["calls"]), 2)

    def test_success_committed_before_view_export_is_not_repeated(self):
        crashed = False

        def exporting(path, value):
            nonlocal crashed
            if path.name == "evidence.json" and value["calls"] and value["calls"][0]["status"] == "completed" and not crashed:
                crashed = True
                raise SimulatedProcessDeath()
            return save_json(path, value)

        with patch("agent.checkpoint.save_json", side_effect=exporting), \
             patch("agent.controller.generate_structured", return_value=(self.first_query(), {})), \
             patch("agent.gateway.ToolGateway.execute", side_effect=mock_response):
            with self.assertRaises(SimulatedProcessDeath):
                diagnose.main(["--adaptive", "--start", START, "--end", END])
        self.assertEqual(self.read()["calls"][0]["status"], "completed")
        self.resume([decision(request("query_metrics", metric="request_rate")), self.finish(), review()])
        run = self.read()
        self.assertEqual(len(run["calls"]), 1)
        self.assertEqual(len(run["evidence"]), 1)
        self.assertEqual(run["state"]["decisions"][1]["status"], "rejected")

    def test_response_lost_before_checkpoint_is_unknown_not_completed(self):
        crashed = False

        def checkpoint(directory, run):
            nonlocal crashed
            if run["calls"] and run["calls"][-1]["status"] == "completed" and not crashed:
                crashed = True
                raise SimulatedProcessDeath()
            write_checkpoint(directory, run)

        with patch("agent.diagnose.write_checkpoint", side_effect=checkpoint), \
             patch("agent.controller.generate_structured", return_value=(self.first_query(), {})), \
             patch("agent.gateway.ToolGateway.execute", side_effect=mock_response):
            with self.assertRaises(SimulatedProcessDeath):
                diagnose.main(["--adaptive", "--start", START, "--end", END])
        self.assertEqual(self.read()["calls"][0]["status"], "started")
        self.assertEqual(self.read()["evidence"], [])
        self.resume([decision(request("query_metrics", metric="request_rate")), self.finish(), review()])
        self.assertEqual(len(self.read()["calls"]), 2)

    def test_unstarted_batch_requests_are_abandoned_without_a_tool_charge(self):
        crashed = False

        def checkpoint(directory, run):
            nonlocal crashed
            write_checkpoint(directory, run)
            if not crashed and len(run["calls"]) == 1 and run["calls"][0]["status"] == "completed":
                crashed = True
                raise SimulatedProcessDeath()

        batch = decision(request("query_metrics", metric="request_rate"),
                         request("query_metrics", metric="request_duration_mean_seconds"))
        with patch("agent.diagnose.write_checkpoint", side_effect=checkpoint), \
             patch("agent.controller.generate_structured", return_value=(batch, {})), \
             patch("agent.gateway.ToolGateway.execute", side_effect=mock_response):
            with self.assertRaises(SimulatedProcessDeath):
                diagnose.main(["--adaptive", "--start", START, "--end", END])
        self.assertEqual(len(self.read()["calls"]), 1)
        self.resume([decision(request("query_metrics", metric="request_duration_mean_seconds")),
                     decision(action="finish"), review()])
        run = self.read()
        self.assertEqual(len(run["calls"]), 2)
        self.assertEqual([call["attempt"] for call in run["calls"]], [1, 1])
        self.assertEqual(len(run["state"]["decisions"]), 3)
        self.assertEqual(run["state"]["decisions"][0]["status"], "interrupted")
        validate_checkpoint(run)

    def test_report_only_replay_is_separate_and_keeps_verification_history(self):
        self.start_interrupted_tool()
        self.resume([decision(request("query_metrics", metric="request_rate")), self.finish(), review()])
        original_directory = self.directory
        original = self.read()
        with patch("agent.controller.generate_structured", side_effect=AssertionError("No planning/review")), \
             patch("agent.gateway.ToolGateway.execute", side_effect=AssertionError("No collection")):
            diagnose.main(["--evidence-file", str(original_directory / "evidence.json")])
        replay_directory = next(path for path in self.root.iterdir() if path != original_directory)
        replay = json.loads((replay_directory / "checkpoint.json").read_text())
        self.assertNotEqual(replay["run_id"], original["run_id"])
        self.assertEqual(replay["state"]["mode"], "replay")
        self.assertEqual(replay["state"]["source_run_id"], original["run_id"])
        self.assertEqual(replay["state"]["verification_checks"], original["state"]["verification_checks"])
        self.assertEqual(replay["calls"], [])
        self.assertEqual(len(replay["state"]["report_calls"]), 1)
        self.assertEqual(json.loads((original_directory / "checkpoint.json").read_text()), original)

    def test_simulated_approval_keeps_authoritative_and_readable_state_consistent(self):
        self.start_interrupted_tool()
        self.resume([decision(request("query_metrics", metric="request_rate")), self.finish(), review()])
        with patch("subprocess.run", side_effect=AssertionError("No infrastructure execution")):
            action = review_action(self.directory, "approved")
        checkpoint, view = self.read(), self.read("controller.json")
        self.assertTrue(action["simulation_only"])
        self.assertEqual(checkpoint["state"]["remediation_status"], "approved")
        self.assertEqual(view["remediation_status"], "approved")
        self.assertEqual(checkpoint["generation"], view["generation"])

    def test_incompatible_completed_and_missing_budget_state_are_rejected(self):
        self.start_interrupted_tool()
        original = self.read()
        for mutate in [
            lambda run: run.update(checkpoint_version=0),
            lambda run: run["state"].pop("limits"),
            lambda run: run["state"].update(mode="fixed"),
            lambda run: run["state"].update(status="completed", phase="done"),
            lambda run: run["calls"][0].update(attempt=0),
            lambda run: run["state"].update(decisions=[]),
            lambda run: run["state"]["limits"].update(tool_calls=800),
        ]:
            candidate = copy.deepcopy(original)
            mutate(candidate)
            save_json(self.directory / "checkpoint.json", candidate)
            with self.subTest(candidate=candidate["state"].get("mode")), self.assertRaises(ValueError):
                load_resume(self.directory)
        save_json(self.directory / "checkpoint.json", original)
        self.resume([decision(request("query_metrics", metric="request_rate")), self.finish(), review()])
        with self.assertRaisesRegex(ValueError, "completed"):
            load_resume(self.directory)

    def test_resume_rejects_scope_changes_and_concurrent_processes(self):
        self.start_interrupted_tool()
        for flags in [["--adaptive"], ["--collect-only"], ["--start", START], ["--question", "New question"],
                      ["--service", "checkout"], ["--evidence-file", "unused.json"]]:
            with self.subTest(flags=flags), self.assertRaises(SystemExit) as error:
                diagnose.main(["--resume", str(self.directory), *flags])
            self.assertEqual(error.exception.code, 2)
        with run_lock(self.directory), self.assertRaisesRegex(ValueError, "already active"):
            with run_lock(self.directory):
                self.fail("Second writer must not run")
