import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from agent.model import ReportValidationError, generate_report
from agent.schemas import IncidentReport
from agent.summarize import summarize_evidence
from agent.validate_report import validate_report
from tests.fixtures import TIMESTAMP, incident, metric, report


class ReportTests(unittest.TestCase):
    def test_known_pool_incident(self):
        evidence = incident()
        expected = report(evidence, incident_case=True)
        with patch("agent.model.generate_structured", return_value=(expected, {})) as model:
            result, metadata = generate_report("Explain latency", evidence, [])
        self.assertEqual(result.confidence, "medium")
        payload = json.loads(model.call_args.kwargs["content"])
        self.assertEqual(model.call_args.kwargs["max_tokens"], 8192)
        self.assertEqual(payload["evidence"][0]["data"][1]["observed_values"], [2])
        self.assertEqual(result.trace_breakdowns[0].spans[1].duration_ms, 833.456)
        self.assertEqual(metadata["prompt_version"], "structured-observations-v1")

    def test_readable_source_timestamps_preserve_precision_and_original_evidence(self):
        evidence = [
            {"tool": "get_trace", "data": [{"start_time_unix_nano": "1789815010075893123",
                                            "duration_ms": 1042.268}]},
            {"tool": "search_logs", "data": [{"timestamp_unix_nano": "1789815010908621000"}]},
            {"tool": "find_traces", "data": [{"start_time_unix_nano": None}]},
        ]
        original = copy.deepcopy(evidence)
        summarized = summarize_evidence(evidence)
        self.assertEqual(summarized[0]["data"][0]["source_timestamp_utc"],
                         "2026-09-19T10:50:10.075893123Z")
        self.assertEqual(summarized[1]["data"][0]["source_timestamp_utc"],
                         "2026-09-19T10:50:10.908621000Z")
        self.assertNotIn("source_timestamp_utc", summarized[2]["data"][0])
        self.assertEqual(evidence, original)
        self.assertEqual(summarized[0]["data"][0]["duration_ms"], 1042.268)

    def test_insufficient_evidence_with_high_confidence_still_fails(self):
        candidate = report()
        candidate.confidence = "high"
        with self.assertRaisesRegex(ValueError, "insufficient evidence requires low confidence"):
            validate_report(candidate, [])

    def test_normal_ambiguous_empty_and_failed_collections(self):
        cases = [([metric("request_duration_mean_seconds", [.2, .201, .2])], []),
                 ([metric("pool_utilization", [1, 1])], []),
                 ([metric("request_rate", [None, None])], []),
                 ([], [{"tool": "query_metrics", "error": {"code": "backend_error"}}])]
        for evidence, errors in cases:
            with self.subTest(evidence=evidence):
                with patch("agent.model.generate_structured", return_value=(report(evidence), {})) as model:
                    result, _ = generate_report("Explain latency", evidence, errors)
                self.assertEqual(result.assessment, "insufficient_evidence")
                self.assertEqual(result.confidence, "low")
                self.assertEqual(json.loads(model.call_args.kwargs["content"])["collection_errors"], errors)

    def test_configuration_span_and_citation_checks_remain_strict(self):
        evidence = incident()
        valid = report(evidence, incident_case=True).model_dump()
        changes = [
            ("configuration_comparisons", []),
            ("trace_breakdowns", []),
            ("leading_hypothesis", {"statement": "claim", "evidence_ids": ["invented"]}),
        ]
        for field, value in changes:
            candidate = {**valid, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_report(IncidentReport.model_validate(candidate), evidence)
        candidate = copy.deepcopy(valid)
        candidate["trace_breakdowns"][0]["spans"][1]["duration_ms"] = 840
        with self.assertRaisesRegex(ValueError, "span duration"):
            validate_report(IncidentReport.model_validate(candidate), evidence)

    def test_validation_failure_retains_metadata_and_candidate(self):
        evidence = incident()
        candidate = report(evidence, incident_case=True)
        candidate.trace_breakdowns = []
        with patch("agent.model.generate_structured", return_value=(candidate, {"usage": None})):
            with self.assertRaisesRegex(ReportValidationError, "retrieved trace was omitted") as error:
                generate_report("Explain latency", evidence, [])
        self.assertEqual(error.exception.metadata["usage"], None)
        self.assertIs(error.exception.report, candidate)

    def test_missing_failure_disclosure_is_rejected(self):
        candidate = report()
        candidate.missing_information = []
        with patch("agent.model.generate_structured", return_value=(candidate, {})):
            with self.assertRaisesRegex(ReportValidationError, "collection limitations"):
                generate_report("Explain latency", [], [{"error": "backend_error"}])

    def test_metric_buckets_and_log_timestamps(self):
        evidence = [metric("request_rate", [1, 2])]
        candidate = report()
        observation = {"statement": "Observed samples in this minute",
                       "start_utc": "2026-09-19T10:30:00Z", "end_utc": "2026-09-19T10:31:00Z",
                       "precision": "metric_bucket", "evidence_ids": ["request_rate"]}
        candidate = IncidentReport.model_validate({**candidate.model_dump(), "timeline_observations": [observation]})
        validate_report(candidate, evidence)
        candidate.timeline_observations[0].start_utc = "2026-09-19T10:29:00Z"
        candidate.timeline_observations[0].end_utc = "2026-09-19T10:30:00Z"
        with self.assertRaisesRegex(ValueError, "bucket is absent"):
            validate_report(candidate, evidence)
        logs = [{"tool": "search_logs", "evidence_id": "logs", "data": [{
            "timestamp_unix_nano": str(TIMESTAMP * 10**9),
            "message": "event at 10:30:00; observed at 10:30:01"}]}]
        observation.update(precision="source_timestamp", evidence_ids=["logs"],
                           end_utc="2026-09-19T10:30:01Z")
        candidate = IncidentReport.model_validate({**report().model_dump(), "timeline_observations": [observation]})
        with self.assertRaisesRegex(ValueError, "instants, not durations"):
            validate_report(candidate, logs)
        candidate.timeline_observations[0].end_utc = observation["start_utc"]
        validate_report(candidate, logs)

    def test_configuration_summary_preserves_intermediate_values_and_missing(self):
        evidence = [metric("pool_limit", [10, 7, 2]), metric("request_rate", [None, None])]
        summarized = summarize_evidence(evidence)
        self.assertEqual(summarized[0]["data"][0]["observed_values"], [2, 7, 10])
        self.assertEqual(summarized[1]["data"][0]["buckets"][0][1:], [0, 2, None, None, None])

    def test_log_ranges_require_distinct_source_events_at_both_endpoints(self):
        logs = [{"tool": "search_logs", "evidence_id": "logs", "data": [
            {"timestamp_unix_nano": str(TIMESTAMP * 10**9), "message": "first event"},
            {"timestamp_unix_nano": str((TIMESTAMP + 1) * 10**9), "message": "second event"},
        ]}]
        observation = {"statement": "Two events were recorded in this interval",
                       "start_utc": "2026-09-19T10:30:00Z", "end_utc": "2026-09-19T10:30:01Z",
                       "precision": "source_timestamp", "evidence_ids": ["logs"]}
        candidate = IncidentReport.model_validate({**report().model_dump(), "timeline_observations": [observation]})
        validate_report(candidate, logs)
        candidate.timeline_observations[0].end_utc = "2026-09-19T10:30:02Z"
        with self.assertRaisesRegex(ValueError, "distinct log events"):
            validate_report(candidate, logs)
        candidate.timeline_observations[0].end_utc = observation["end_utc"]
        logs[0]["data"][1] = dict(logs[0]["data"][0])
        with self.assertRaisesRegex(ValueError, "distinct log events"):
            validate_report(candidate, logs)

    def test_saved_failed_run_supports_honest_partial_report(self):
        saved = Path(__file__).parent / "fixtures/partial_collection.json"
        payload = json.loads(saved.read_text())
        candidate = report(payload["evidence"])
        with patch("agent.model.generate_structured", return_value=(candidate, {})) as model:
            generate_report(payload["question"], payload["evidence"], payload["collection_errors"])
        context = json.loads(model.call_args.kwargs["content"])
        self.assertEqual(context["configuration_evidence_ids"], [])
        self.assertEqual(context["retrieved_trace_evidence_ids"], [])

    def test_captured_live_report_contract_errors(self):
        captured = json.loads((Path(__file__).parent / "fixtures/rejected_report.json").read_text())
        captured["report"]["metric_measurements"] = []
        # The live model called utilization a setting; the schema now rules it out.
        self.assertEqual(captured["report"]["configuration_comparisons"][0]["metric"], "pool_utilization")
        with self.assertRaises(ValidationError) as error:
            IncidentReport.model_validate(captured["report"])
        self.assertIn(("configuration_comparisons", 0, "metric"),
                      [item["loc"] for item in error.exception.errors()])
        # Its search results also cannot stand in for retrieved trace spans.
        candidate = IncidentReport.model_validate({
            **report().model_dump(), "trace_breakdowns": captured["report"]["trace_breakdowns"]})
        with self.assertRaisesRegex(ValueError, "must reference get_trace"):
            validate_report(candidate, captured["evidence"])

    def test_high_confidence_needs_more_than_metrics_and_trace_search(self):
        evidence = [metric("pool_utilization", [1])]
        candidate = report()
        candidate.assessment = "likely_cause_identified"
        candidate.leading_hypothesis.evidence_ids = ["pool_utilization"]
        candidate.confidence = "high"
        with self.assertRaisesRegex(ValueError, "multiple collected signal types"):
            validate_report(candidate, evidence)
