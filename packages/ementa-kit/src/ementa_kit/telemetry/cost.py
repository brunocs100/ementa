"""Adds up what the model calls of a run cost, from the GenAI attributes of the `chat` spans."""

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from ementa_kit.llm.pricing import usage_from_attributes


class CostMeter(SpanProcessor):
    """Register on the tracer provider; read `total_usd` to enforce a budget between steps."""

    def __init__(self) -> None:
        self.total_usd = 0.0
        self.calls = 0
        self.unpriced: set[str] = set()

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        usage = usage_from_attributes(span.attributes or {})
        if usage is None:
            return
        cost = usage.cost_usd
        self.calls += 1
        if cost is None:
            self.unpriced.add(usage.model)
        else:
            self.total_usd += cost

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
