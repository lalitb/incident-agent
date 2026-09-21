import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http._log_exporter import (
    OTLPLogExporter,
)


def setup_telemetry():
    endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://localhost:4318",
    ).rstrip("/")

    resource = Resource.create({
        "service.name": "checkout",
        "service.version": os.getenv("SERVICE_VERSION", "v1"),
        "deployment.environment.name": "local",
    })

    # Traces: completed spans are exported in batches.
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"),
            schedule_delay_millis=1000,
        )
    )
    trace.set_tracer_provider(trace_provider)

    # Metrics: collect and export every five seconds.
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
        export_interval_millis=5000,
    )
    metric_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
    )
    metrics.set_meter_provider(metric_provider)

    # Logs: bridge Python logging into OpenTelemetry.
    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"),
            schedule_delay_millis=1000,
        )
    )

    logger = logging.getLogger("checkout")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    otel_handler = LoggingHandler(
        level=logging.INFO,
        logger_provider=log_provider,
    )
    logger.addHandler(otel_handler)
    logger.addHandler(logging.StreamHandler())

    def shutdown():
        # Export remaining data when the server stops normally.
        metric_provider.shutdown()
        trace_provider.shutdown()
        logger.removeHandler(otel_handler)
        log_provider.shutdown()

    return (
        trace.get_tracer("checkout"),
        metrics.get_meter("checkout"),
        logger,
        shutdown,
    )