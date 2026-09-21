import copy
import unittest

from agent.schemas import TimelineObservation
from agent.validate_report import validate_report
from tests.fixtures import TIMESTAMP, report, trace


def timed_trace(identifier, offset):
    item = trace()
    item["evidence_id"] = identifier
    item["trace_id"] = identifier.zfill(32)
    for span in item["data"]:
        span["start_time_unix_nano"] = str((TIMESTAMP + offset) * 10**9)
    return item


class IntervalRegressionTests(unittest.TestCase):
    def setUp(self):
        self.evidence = [timed_trace("1", 0), timed_trace("2", 10)]
        self.report = report(self.evidence)
        self.report.timeline_observations = [TimelineObservation(
            statement="The cited traces started in this interval.",
            start_utc="2026-09-19T10:30:00Z", end_utc="2026-09-19T10:30:10Z",
            precision="source_timestamp", evidence_ids=["1", "2"])]

    def test_interval_can_span_different_cited_traces(self):
        validate_report(self.report, self.evidence)

    def test_endpoint_in_uncited_trace_does_not_count(self):
        self.report.timeline_observations[0].evidence_ids = ["1"]
        with self.assertRaisesRegex(ValueError, "source timestamp is absent"):
            validate_report(self.report, self.evidence)

    def test_invented_endpoint_between_real_endpoints_is_rejected(self):
        self.report.timeline_observations[0].end_utc = "2026-09-19T10:30:09Z"
        with self.assertRaisesRegex(ValueError, "source timestamp is absent"):
            validate_report(self.report, self.evidence)

    def test_irrelevant_citation_outside_interval_is_rejected(self):
        self.evidence.append(timed_trace("3", 60))
        self.report.trace_breakdowns = report(self.evidence).trace_breakdowns
        self.report.timeline_observations[0].evidence_ids.append("3")
        with self.assertRaisesRegex(ValueError, "outside the claimed interval"):
            validate_report(self.report, self.evidence)

    def test_reversed_interval_remains_rejected(self):
        self.report.timeline_observations[0].start_utc = "2026-09-19T10:30:20Z"
        with self.assertRaisesRegex(ValueError, "reversed"):
            validate_report(self.report, self.evidence)

    def test_distinct_log_sources_can_support_range(self):
        evidence = [{"evidence_id": str(index), "tool": "search_logs", "data": [
            {"timestamp_unix_nano": str((TIMESTAMP + offset) * 10**9), "message": "event"}]
        } for index, offset in [(1, 0), (2, 10)]]
        candidate = copy.deepcopy(self.report)
        candidate.trace_breakdowns = []
        validate_report(candidate, evidence)
        evidence[1]["data"][0]["timestamp_unix_nano"] = str(TIMESTAMP * 10**9)
        with self.assertRaisesRegex(ValueError, "distinct log events"):
            validate_report(candidate, evidence)
