import unittest

from pydantic import ValidationError

from agent.schemas import Finding, MetricMeasurement
from agent.summarize import summarize_evidence
from agent.validate_report import validate_report
from tests.fixtures import incident, metric, report


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.evidence = [metric('connection_wait_mean_seconds', [.8, .9, None])]
        self.measurement = {
            'evidence_id': 'connection_wait_mean_seconds',
            'metric': 'connection_wait_mean_seconds', 'labels': {'service_version': 'v2'},
            'bucket_start_utc': '2026-09-19T10:30:00Z', 'statistic': 'sample_mean',
            'value': .85, 'unit': 'seconds',
        }

    def candidate(self, **changes):
        candidate = report()
        candidate.metric_measurements = [MetricMeasurement(**{**self.measurement, **changes})]
        return candidate

    def test_bucket_statistics_match_the_model_summary(self):
        summary = summarize_evidence(self.evidence)[0]
        for statistic, value in [('minimum', .8), ('sample_mean', .85), ('maximum', .9)]:
            with self.subTest(statistic=statistic):
                column = summary['bucket_columns'].index(statistic)
                self.assertEqual(value, summary['data'][0]['buckets'][0][column])
                validate_report(self.candidate(statistic=statistic, value=value), self.evidence)

    def test_value_identity_unit_labels_time_and_statistic_must_match(self):
        for change in [dict(value=836.2), dict(metric='request_duration_mean_seconds'),
                       dict(unit='connections'), dict(labels={'service_version': 'v1'}),
                       dict(bucket_start_utc='2026-09-19T10:31:00Z'),
                       dict(statistic='maximum'), dict(evidence_id='unknown')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_report(self.candidate(**change), self.evidence)

    def test_value_from_uncited_evidence_does_not_count(self):
        other = metric('request_duration_mean_seconds', [.5])
        with self.assertRaisesRegex(ValueError, 'metric value'):
            validate_report(self.candidate(value=.5), [*self.evidence, other])

    def test_all_series_in_cited_record_are_considered(self):
        other = metric('connection_wait_mean_seconds', [.4])['data'][0]
        other['labels']['extra_label'] = 'omitted-by-summary'
        self.evidence[0]['data'].append(other)
        validate_report(self.candidate(value=.4), self.evidence)

    def test_wrong_tool_cannot_support_a_metric(self):
        item = {'evidence_id': self.measurement['evidence_id'], 'tool': 'search_logs', 'data': []}
        with self.assertRaisesRegex(ValueError, 'must reference query_metrics'):
            validate_report(self.candidate(), [item])

    def test_null_does_not_become_zero(self):
        self.evidence[0]['data'][0]['points'] = [{'timestamp': 1789813800, 'value': None}]
        with self.assertRaisesRegex(ValueError, 'metric value'):
            validate_report(self.candidate(value=0), self.evidence)

    def test_units_for_rate_utilization_and_pool(self):
        for name, unit, value in [('request_rate', 'requests/second', 4),
                                  ('pool_utilization', 'ratio', 1), ('pool_limit', 'connections', 2)]:
            with self.subTest(name=name):
                evidence = [metric(name, [value])]
                candidate = report(evidence)
                candidate.metric_measurements = [MetricMeasurement(**{
                    **self.measurement, 'evidence_id': name, 'metric': name, 'unit': unit, 'value': value})]
                validate_report(candidate, evidence)

    def test_schema_rejects_nonfinite_boolean_and_unsupported_measurements(self):
        for change in [dict(value=float('nan')), dict(value=float('inf')), dict(value=True),
                       dict(statistic='p95'), dict(unit='ms')]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.candidate(**change)

    def test_naive_bucket_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'timezone'):
            validate_report(self.candidate(bucket_start_utc='2026-09-19T10:30:00'), self.evidence)

    def test_trace_identity_name_and_measurement_are_checked_together(self):
        evidence = incident()
        for field, value in [('trace_id', 'wrong'), ('span_id', 'unknown'),
                             ('name', 'db.wrong_operation'), ('duration_ms', 999)]:
            candidate = report(evidence, incident_case=True)
            target = candidate.trace_breakdowns[0]
            if field != 'trace_id':
                target = target.spans[1]
            setattr(target, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_report(candidate, evidence)

    def test_configuration_value_and_version_are_checked_together(self):
        evidence = incident()
        for field, value in [('value', 999), ('service_version', 'unknown'), ('evidence_id', 'trace')]:
            candidate = report(evidence, incident_case=True)
            setattr(candidate.configuration_comparisons[0].observations[0], field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_report(candidate, evidence)

    def test_narrative_is_for_human_review_but_citation_ids_are_checked(self):
        candidate = self.candidate()
        candidate.supporting_findings = [Finding(
            statement='This proves a 999 ms outage.', evidence_ids=[self.measurement['evidence_id']])]
        candidate.recommended_next_steps = ['A proposed change will fix everything within 1 minute.']
        # Deliberately unsupported prose demonstrates the boundary, not a quality pass.
        validate_report(candidate, self.evidence)
        candidate.supporting_findings[0].evidence_ids = ['unknown']
        with self.assertRaisesRegex(ValueError, 'unknown evidence ID'):
            validate_report(candidate, self.evidence)
