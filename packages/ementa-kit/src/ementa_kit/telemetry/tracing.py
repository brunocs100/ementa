from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from ementa_kit.config import Settings
from ementa_kit.telemetry.logging import configure_logging


def setup(settings: Settings, exporter: SpanExporter | None = None) -> TracerProvider:
    """Configure logs and the global tracer provider.

    Spans go to the OTLP/HTTP endpoint in `settings.otel_endpoint` (the otel-lgtm container in
    dev). Pass `exporter` to send them elsewhere, as the tests do.
    """
    configure_logging(settings)
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "deployment.environment.name": settings.env.value,
        }
    )
    provider = TracerProvider(resource=resource)
    if exporter is None and settings.otel_enabled:
        exporter = OTLPSpanExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/traces")
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def instrument_openai() -> None:
    """`chat` spans with GenAI attributes (model, tokens) for every OpenAI SDK call."""
    from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

    instrumentor = OpenAIInstrumentor()  # type: ignore[no-untyped-call]  # untyped package
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument()


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)
