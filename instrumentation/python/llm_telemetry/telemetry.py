"""OpenTelemetry setup: traces, metrics and logs over OTLP/HTTP.

Configured with standard OTel env vars:
  OTEL_EXPORTER_OTLP_ENDPOINT  (default http://localhost:4318)
  OTEL_SERVICE_NAME            (default llm-app)
and one project setting:
  PROMPT_LOGGING=on|off        (default off) - send prompt/response text as logs
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

LATENCY_BUCKETS = [0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 60, 120]

_initialized = False
_providers = []


def prompt_logging_enabled() -> bool:
    return os.getenv("PROMPT_LOGGING", "off").strip().lower() in ("on", "true", "1", "yes")


def setup(service_name: str | None = None) -> None:
    """Initialise OTel once per process. Safe to call multiple times."""
    global _initialized
    if _initialized:
        return
    resource = Resource.create(
        {SERVICE_NAME: service_name or os.getenv("OTEL_SERVICE_NAME", "llm-app")}
    )

    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tp)

    mp = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10_000)
        ],
        views=[
            View(
                instrument_name="llm_*_seconds",
                aggregation=ExplicitBucketHistogramAggregation(LATENCY_BUCKETS),
            )
        ],
    )
    metrics.set_meter_provider(mp)

    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(lp)
    otel_logger = logging.getLogger("llm_telemetry.events")
    otel_logger.setLevel(logging.INFO)
    otel_logger.propagate = False
    otel_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=lp))

    _providers.extend([tp, mp, lp])
    _initialized = True


def shutdown() -> None:
    """Flush everything before the process exits."""
    for p in _providers:
        p.shutdown()
