import copy
import json
import unittest

from agent.context import MAX_LOOKUP_BYTES, ContextLimit, EvidenceLookup, build_context, encode, lookup_evidence
from agent.controller import initialize_state
from tests import test_controller
from tests.fixtures import BASE, TIMESTAMP, metric
from tests.test_controller import decision, request


class ContextTests(unittest.TestCase):
    def context(self, evidence, max_bytes=80_000):
        state = {"decisions": []}
        initialize_state(state)
        return build_context(
            question="Assess competing explanations.", base=BASE, evidence=evidence,
            collection_errors=[], calls=[], state=state, max_bytes=max_bytes,
        )

    def test_metric_summarization_preserves_missing_and_contradictory_extremes(self):
        item = metric("request_duration_mean_seconds", [None, .2, 4, .3] * 90)
        item.update(source="fixture://metrics", query_parameters={"service": "checkout"},
                    retrieved_at=BASE["end"], possibly_truncated=True)
        original = copy.deepcopy(item)
        content, manifest = self.context([item])
        compact = json.loads(content)["evidence"][0]
        self.assertEqual(compact["data"][0]["missing_samples"], 90)
        self.assertEqual(min(bucket[3] for bucket in compact["data"][0]["buckets"]), .2)
        self.assertEqual(max(bucket[5] for bucket in compact["data"][0]["buckets"]), 4)
        self.assertEqual(compact["source"], item["source"])
        self.assertTrue(compact["possibly_truncated"])
        self.assertIn(item["evidence_id"], manifest["summarized_evidence_ids"])
        self.assertEqual(item, original)
        self.assertLess(len(content.encode()), len(encode(item).encode()))

    def test_large_context_keeps_all_evidence_ids_and_explicit_omissions(self):
        evidence = [
            {"evidence_id": identifier, "tool": "search_logs", "source": "fixture://logs",
             "query_parameters": {"contains": ""}, "possibly_truncated": True,
             "data": [{"message": message * 10_000, "timestamp_unix_nano": str(TIMESTAMP * 10**9)}]}
            for identifier, message in [("supports", "slow"), ("contradicts", "normal")]
        ]
        original = copy.deepcopy(evidence)
        content, manifest = self.context(evidence, max_bytes=1_950)
        payload = json.loads(content)
        self.assertLessEqual(len(content.encode("utf-8")), 1_950)
        self.assertEqual({item["evidence_id"] for item in payload["evidence"]}, {"supports", "contradicts"})
        self.assertTrue(manifest["omitted_data_evidence_ids"])
        for item in payload["evidence"]:
            self.assertEqual(item["source"], "fixture://logs")
            self.assertTrue(item["possibly_truncated"])
            self.assertIn("detail_notice", item)
            if item.get("data_omitted"):
                self.assertTrue(item["has_usable_data"])
                self.assertNotIn("data", item)
        self.assertEqual(evidence, original)

    def test_hard_limit_still_stops_instead_of_dropping_required_metadata(self):
        with self.assertRaises(ContextLimit) as failure:
            self.context([metric("request_rate", [1])], max_bytes=100)
        self.assertGreater(failure.exception.manifest["payload_bytes"], 100)
        self.assertEqual(failure.exception.manifest["omitted_data_evidence_ids"], ["request_rate"])

    def test_compaction_selects_both_extremes_not_only_first_or_recent_samples(self):
        item = metric("request_duration_mean_seconds", [1] * 360)
        item["data"][0]["points"][90]["value"] = 12
        item["data"][0]["points"][180]["value"] = .01
        content, manifest = self.context([item], max_bytes=2_400)
        payload = json.loads(content)["evidence"][0]
        self.assertTrue(manifest["compacted_evidence_ids"])
        if payload.get("data_omitted"):
            self.assertEqual(payload["overview"]["minimum_across_series"], .01)
            self.assertEqual(payload["overview"]["maximum_across_series"], 12)
        else:
            self.assertEqual(min(bucket[3] for bucket in payload["data"][0]["buckets"]), .01)
            self.assertEqual(max(bucket[5] for bucket in payload["data"][0]["buckets"]), 12)
            self.assertGreater(payload["data"][0]["omitted_bucket_count"], 0)

    def test_local_lookup_is_byte_bounded_paginated_and_sanitized(self):
        item = {"evidence_id": "logs", "tool": "search_logs", "source": "fixture://logs",
                "data": [{"message": "secret=do-not-send " + "x" * 2000,
                          "timestamp_unix_nano": str(TIMESTAMP * 10**9)} for _ in range(10)]}
        result = lookup_evidence([item], EvidenceLookup(evidence_id="logs", offset=0, limit=10))
        self.assertLessEqual(len(encode(result).encode("utf-8")), MAX_LOOKUP_BYTES)
        self.assertLess(result["next_offset"], 10)
        self.assertNotIn("do-not-send", encode(result))
        self.assertIn("TRUNCATED", result["data"][0]["message"])
        with self.assertRaisesRegex(ValueError, "unknown ID"):
            lookup_evidence([item], EvidenceLookup(evidence_id="unknown", offset=0, limit=1))
        with self.assertRaisesRegex(ValueError, "offset"):
            lookup_evidence([item], EvidenceLookup(evidence_id="logs", offset=10, limit=1))

    def test_lookup_budget_is_separate_but_every_lookup_spends_a_planning_step(self):
        def lookup(identifier):
            result = decision(action="lookup")
            result.evidence_lookup = EvidenceLookup(evidence_id=identifier, offset=0, limit=1)
            return result

        state, calls, _, contexts = test_controller.ControllerTests.run_decisions(self, [
            decision(request("query_metrics", metric="request_rate")),
            lookup("request_rate"), lookup("unknown"), lookup("request_rate"), decision(action="finish"),
        ])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(state["lookups"]), 2)
        self.assertEqual(len(state["decisions"]), 5)
        self.assertEqual(state["decisions"][3]["error"], "Local evidence lookup budget exhausted")
        self.assertEqual(contexts[2]["evidence_lookup_results"][0]["evidence_id"], "request_rate")
        self.assertEqual(contexts[-1]["remaining_evidence_lookups"], 0)
