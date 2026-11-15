import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ementa_kit.config import Settings
from ementa_kit.telemetry import configure_logging, get_logger, setup


def test_setup_exports_spans_with_service_name() -> None:
    exporter = InMemorySpanExporter()
    settings = Settings(_env_file=None, service_name="ementa-test", otel_enabled=False)  # type: ignore[call-arg]

    provider = setup(settings, exporter=exporter)
    with provider.get_tracer("test").start_as_current_span("smoke"):
        pass
    provider.force_flush()

    [span] = exporter.get_finished_spans()
    assert span.name == "smoke"
    assert span.resource.attributes["service.name"] == "ementa-test"


def test_logs_are_json_on_stderr_with_trace_id(capsys: pytest.CaptureFixture[str]) -> None:
    exporter = InMemorySpanExporter()
    settings = Settings(_env_file=None, otel_enabled=False)  # type: ignore[call-arg]
    provider = setup(settings, exporter=exporter)
    configure_logging(settings)

    with provider.get_tracer("test").start_as_current_span("with-log") as span:
        get_logger("test").info("hello", answer=42)
        trace_id = format(span.get_span_context().trace_id, "032x")

    out, err = capsys.readouterr()
    assert out == ""
    line = json.loads(err.strip().splitlines()[-1])
    assert line["event"] == "hello"
    assert line["answer"] == 42
    assert line["trace_id"] == trace_id
