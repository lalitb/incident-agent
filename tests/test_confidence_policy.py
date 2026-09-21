import contextlib
import io
import unittest
from unittest.mock import patch
from agent.model import generate_report
from agent.validate_report import cap_confidence, validate_report
from tests.fixtures import incident, report


class ConfidencePolicyTests(unittest.TestCase):
    def test_high_with_gap_is_capped_and_logged(self):
        evidence = incident()
        candidate = report(evidence, incident_case=True)
        candidate.confidence = 'high'
        candidate.leading_hypothesis.verification_needed = ['Verify the mechanism under representative load.']
        output = io.StringIO()
        with patch('agent.model.generate_structured', return_value=(candidate, {})), contextlib.redirect_stdout(output):
            result, metadata = generate_report('Explain latency', evidence, [])
        self.assertEqual(result.confidence, 'medium')
        self.assertEqual(metadata['confidence_override']['from'], 'high')
        self.assertIn('Confidence override', output.getvalue())

    def test_low_and_medium_are_not_raised(self):
        for confidence in ['low','medium']:
            candidate = report()
            candidate.confidence = confidence
            candidate.leading_hypothesis.verification_needed = ['Verify the mechanism.']
            self.assertIsNone(cap_confidence(candidate))
            self.assertEqual(candidate.confidence, confidence)

    def test_high_without_gap_is_not_overridden(self):
        candidate = report()
        candidate.confidence = 'high'
        self.assertIsNone(cap_confidence(candidate))

    def test_insufficient_evidence_keeps_stronger_low_cap(self):
        candidate = report()
        candidate.confidence = 'high'
        candidate.leading_hypothesis.verification_needed = ['Verify the mechanism.']
        self.assertEqual(cap_confidence(candidate)['to'], 'low')
        validate_report(candidate, [])

    def test_direct_validator_rejects_bypassed_cap(self):
        candidate = report(incident(), incident_case=True)
        candidate.confidence = 'high'
        candidate.leading_hypothesis.verification_needed = ['Verify the mechanism.']
        with self.assertRaisesRegex(ValueError, 'unverified hypothesis'):
            validate_report(candidate, incident())

    def test_controller_gaps_cannot_be_dropped_by_report_model(self):
        evidence = incident()
        candidate = report(evidence, incident_case=True)
        candidate.confidence = 'high'
        with patch('agent.model.generate_structured', return_value=(candidate, {})):
            result, metadata = generate_report('Explain latency', evidence, [], verification_needed=['Verify the query execution mechanism.'])
        self.assertEqual(result.confidence, 'medium')
        self.assertEqual(result.leading_hypothesis.verification_needed, ['Verify the query execution mechanism.'])
