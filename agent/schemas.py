from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Finding(StrictModel):
    statement: str
    evidence_ids: list[str]


class Hypothesis(Finding):
    verification_needed: list[str] = Field(default_factory=list)


class MetricMeasurement(StrictModel):
    evidence_id: str
    metric: str
    labels: dict[str, str]
    bucket_start_utc: str
    statistic: Literal["minimum", "sample_mean", "maximum"]
    value: float = Field(strict=True)
    unit: Literal["seconds", "requests/second", "connections", "ratio"]


class ConfigurationObservation(StrictModel):
    service_version: str
    value: float
    evidence_id: str


class ConfigurationComparison(StrictModel):
    # Must match a collected query_metrics metric identifier.
    metric: Literal["pool_limit"]

    # These are observed values, not necessarily intended settings.
    observations: list[ConfigurationObservation]


class SpanMeasurement(StrictModel):
    span_id: str
    name: str
    duration_ms: float


class TraceBreakdown(StrictModel):
    trace_id: str
    evidence_id: str
    spans: list[SpanMeasurement]


class TimelineObservation(StrictModel):
    statement: str

    # Both fields must be timezone-aware ISO timestamps.
    # For an instant observation, set them equal.
    start_utc: str
    end_utc: str

    precision: Literal[
        "source_timestamp",
        "sample_timestamp",
        "metric_bucket",
    ]

    evidence_ids: list[str]


class IncidentReport(StrictModel):
    assessment: Literal[
        "likely_cause_identified",
        "insufficient_evidence",
    ] = Field(description="A supported plausible cause need not be proven. If no cause is supported, use insufficient_evidence with low confidence.")

    metric_measurements: list[MetricMeasurement] = Field(
        description="Selected measurements copied from supplied metric buckets, with their labels, units and evidence IDs."
    )
    configuration_comparisons: list[ConfigurationComparison] = Field(
        description="Only supplied pool_limit evidence. Empty when none was collected."
    )
    trace_breakdowns: list[TraceBreakdown] = Field(
        description="Only nonempty get_trace evidence, never find_traces results. Empty if none."
    )
    timeline_observations: list[TimelineObservation]

    leading_hypothesis: Hypothesis
    supporting_findings: list[Finding]
    contradicting_findings: list[Finding]

    confidence: Literal["low", "medium", "high"] = Field(
        description="Confidence in the causal assessment. Must be low when assessment is insufficient_evidence."
    )
    missing_information: list[str]
    recommended_next_steps: list[str]
