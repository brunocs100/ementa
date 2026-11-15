"""Adds up what the model calls of a run cost, from the GenAI attributes of the `chat` spans."""

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from ementa_kit.llm.pricing import cost_usd


class CostMeter(SpanProcessor):
    """Register on the tracer provider; read `total_usd` to enforce a budget between steps."""

    def __init__(self) -> None:
        self.total_usd = 0.0
        self.calls = 0
        self.unpriced: set[str] = set()

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        attrs = span.attributes or {}
        if "gen_ai.usage.input_tokens" not in attrs:
            return
        model = str(attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model") or "")
        cost = cost_usd(
            model,
            int(attrs.get("gen_ai.usage.input_tokens", 0)),  # type: ignore[arg-type]
            int(attrs.get("gen_ai.usage.output_tokens", 0)),  # type: ignore[arg-type]
            int(attrs.get("gen_ai.usage.cache_read.input_tokens", 0)),  # type: ignore[arg-type]
        )
        self.calls += 1
        if cost is None:
            self.unpriced.add(model)
        else:
            self.total_usd += cost

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
