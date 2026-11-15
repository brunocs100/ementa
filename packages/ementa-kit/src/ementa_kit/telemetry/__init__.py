"""OpenTelemetry tracing and structured logging for every Ementa process."""

from ementa_kit.telemetry.cost import CostMeter
from ementa_kit.telemetry.logging import configure_logging, get_logger
from ementa_kit.telemetry.tracing import get_tracer, instrument_openai, setup

__all__ = [
    "CostMeter",
    "configure_logging",
    "get_logger",
    "get_tracer",
    "instrument_openai",
    "setup",
]
