import unittest
from unittest.mock import patch
from agent.controller import VerificationNeed
from agent.verification import VerificationResolution
from tests import test_controller
from tests.test_controller import decision, request
from tests.fixtures import TRACE_ID, trace, report


CLAIM = 'Verify connection acquisition and query execution separately.'


def finish_with_gap():
    result = decision(action='finish')
    result.verification_needed = [VerificationNeed(claim=CLAIM, tool='get_trace', metric=None)]
    return result


def resolved_finish():
    result = decision(action='finish')
    result.resolved_verifications = [VerificationResolution(
        check_id="check-001", evidence_ids=["trace"],
        explanation="The selected trace separately measures acquisition and query execution; not population proof.",
        trace_breakdowns=report([trace()]).trace_breakdowns,
    )]
    return result


class GapPolicyTests(unittest.TestCase):
    run_decisions = test_controller.ControllerTests.run_decisions

    def test_finish_forces_search_then_trace_before_resolution(self):
        state, calls, snapshots, contexts = self.run_decisions([
            finish_with_gap(), decision(request('find_traces', limit=1, min_duration_ms=800)),
            decision(request('get_trace', trace_id=TRACE_ID)), resolved_finish()])
        self.assertEqual(state['decisions'][0]['status'], 'rejected')
        self.assertEqual([c['tool'] for c in calls], ['find_traces','get_trace'])
        self.assertEqual(state['stop_reason'], 'model_finished')
        self.assertEqual(state['verification_checks']['check-001']['status'], 'resolved')
        self.assertEqual(contexts[1]['verification_checks']['check-001']['claim'], CLAIM)

    def test_omitting_a_previously_flagged_gap_does_not_clear_it(self):
        state, calls, _, _ = self.run_decisions([finish_with_gap()] + [decision(action='finish')]*5)
        self.assertEqual(state['stop_reason'], 'decision_budget')
        self.assertEqual(state['verification_checks']['check-001']['claim'], CLAIM)
        self.assertEqual(state['verification_checks']['check-001']['status'], 'pending')
        self.assertEqual(calls, [])

    def test_unrelated_batch_is_rejected_atomically(self):
        state, calls, _, _ = self.run_decisions([finish_with_gap(),
            decision(request('query_metrics', metric='request_rate')),
            decision(request('find_traces', limit=1, min_duration_ms=800)),
            decision(request('get_trace', trace_id=TRACE_ID)), resolved_finish()])
        self.assertEqual(state['decisions'][1]['status'], 'rejected')
        self.assertNotIn('query_metrics', [c['tool'] for c in calls])

    def test_search_only_cannot_resolve_span_gap(self):
        state, calls, _, _ = self.run_decisions([finish_with_gap(),
            decision(request('find_traces', limit=1, min_duration_ms=800)), resolved_finish(),
            decision(request('get_trace', trace_id=TRACE_ID)), resolved_finish()])
        self.assertEqual(state['decisions'][2]['status'], 'rejected')
        self.assertEqual(state['stop_reason'], 'model_finished')

    def test_failed_targeted_query_leaves_gap_and_consumes_tool_budget(self):
        result = decision(action='finish')
        result.verification_needed = [VerificationNeed(claim='Verify latency.',tool='query_metrics',metric='request_duration_mean_seconds')]
        with patch('agent.controller.MAX_TOOL_CALLS', 1):
            state, calls, _, _ = self.run_decisions([result, decision(request('query_metrics',metric='request_duration_mean_seconds'))], failed_tools=True)
        self.assertEqual(state['stop_reason'], 'tool_budget')
        self.assertEqual(state['verification_checks']['check-001']['claim'], 'Verify latency.')
        self.assertEqual(len(calls), 1)

    def test_last_decision_does_not_bypass_planning_budget(self):
        with patch('agent.controller.MAX_DECISIONS', 1):
            state, calls, _, contexts = self.run_decisions([finish_with_gap()])
        self.assertEqual(state['stop_reason'], 'decision_budget')
        self.assertEqual(len(contexts), 1)
        self.assertEqual(calls, [])

    def test_model_cannot_claim_resolution_without_targeted_evidence(self):
        state, calls, _, _ = self.run_decisions([finish_with_gap(), resolved_finish(),
            decision(request('find_traces',limit=1,min_duration_ms=800)),
            decision(request('get_trace',trace_id=TRACE_ID)), resolved_finish()])
        self.assertEqual(state['decisions'][1]['status'], 'rejected')
        self.assertEqual(state['stop_reason'], 'model_finished')

    def test_discovery_check_allows_retrieving_its_discovered_trace(self):
        discovery = decision(request('find_traces', limit=1, min_duration_ms=800))
        discovery.verification_needed = [VerificationNeed(
            claim='Find a slow checkout trace for further inspection.', tool='find_traces', metric=None,
        )]
        finish = decision(action='finish')
        finish.resolved_verifications = [VerificationResolution(
            check_id='check-001', evidence_ids=['search'],
            explanation='The search returned one matching trace ID; this resolves discovery, not its mechanism.',
        )]
        state, calls, _, _ = self.run_decisions([
            discovery, decision(request('get_trace', trace_id=TRACE_ID)), finish,
        ])
        self.assertEqual([call['tool'] for call in calls], ['find_traces', 'get_trace'])
        self.assertEqual(state['decisions'][1]['status'], 'completed')
        self.assertEqual(state['stop_reason'], 'model_finished')

    def test_discovery_followup_does_not_resolve_the_check_implicitly(self):
        discovery = decision(request('find_traces', limit=1, min_duration_ms=800))
        discovery.verification_needed = [VerificationNeed(
            claim='Discover a slow trace.', tool='find_traces', metric=None,
        )]
        with patch('agent.controller.MAX_DECISIONS', 3):
            state, calls, _, _ = self.run_decisions([
                discovery, decision(request('get_trace', trace_id=TRACE_ID)), decision(action='finish'),
            ])
        self.assertEqual(len(calls), 2)
        self.assertEqual(state['verification_checks']['check-001']['status'], 'pending')
        self.assertEqual(state['decisions'][2]['status'], 'rejected')

    def test_discovery_followup_still_rejects_undiscovered_trace_ids(self):
        discovery = decision(request('find_traces', limit=1, min_duration_ms=800))
        discovery.verification_needed = [VerificationNeed(
            claim='Discover a slow trace.', tool='find_traces', metric=None,
        )]
        with patch('agent.controller.MAX_DECISIONS', 2):
            state, calls, _, _ = self.run_decisions([
                discovery, decision(request('get_trace', trace_id='00000000000000000000000000000002')),
            ])
        self.assertEqual(len(calls), 1)
        self.assertEqual(state['decisions'][1]['error'], 'Trace ID was not returned by a trace search')

    def test_retrieval_cannot_substitute_for_a_discovery_resolution(self):
        discovery = decision(request('find_traces', limit=1, min_duration_ms=800))
        discovery.verification_needed = [VerificationNeed(
            claim='Discover a slow trace.', tool='find_traces', metric=None,
        )]
        with patch('agent.controller.MAX_DECISIONS', 3):
            state, calls, _, _ = self.run_decisions([
                discovery, decision(request('get_trace', trace_id=TRACE_ID)), resolved_finish(),
            ])
        self.assertEqual(len(calls), 2)
        self.assertEqual(state['decisions'][2]['error'], 'Resolution evidence does not match the check target')
        self.assertEqual(state['verification_checks']['check-001']['status'], 'pending')
