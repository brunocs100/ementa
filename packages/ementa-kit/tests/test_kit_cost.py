import pytest
from opentelemetry.sdk.trace import TracerProvider

from ementa_kit.llm import cost_usd, price_for
from ementa_kit.llm.pricing import load_prices
from ementa_kit.telemetry import CostMeter


def test_every_model_role_has_a_price() -> None:
    assert set(load_prices()) >= {"gpt-6.1-terra", "gpt-6-luna", "gpt-6.1-sol"}


def test_cost_counts_cached_input_at_the_cached_price() -> None:
    # 2,188 input (188 cached) and 82 output tokens on Terra: 2000*1 + 188*0.05 + 82*5 per 1M.
    assert cost_usd("gpt-6.1-terra", 2188, 82, cached_input_tokens=188) == pytest.approx(
        (2000 * 1.00 + 188 * 0.05 + 82 * 5.00) / 1_000_000
    )


def test_dated_snapshot_uses_the_base_price_and_unknown_model_has_none() -> None:
    assert price_for("gpt-6-luna-2026-10-01") == price_for("gpt-6-luna")
    assert cost_usd("modelo-desconhecido", 10, 10) is None


def test_meter_adds_up_the_chat_spans_and_lists_unpriced_models() -> None:
    meter = CostMeter()
    provider = TracerProvider()
    provider.add_span_processor(meter)
    tracer = provider.get_tracer("t")
    usage = {"gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 100}
    with tracer.start_as_current_span("invoke_agent x"):
        with tracer.start_as_current_span(
            "chat", attributes={"gen_ai.request.model": "gpt-6-luna", **usage}
        ):
            pass
        with tracer.start_as_current_span(
            "chat", attributes={"gen_ai.request.model": "outro", **usage}
        ):
            pass

    assert meter.calls == 2
    assert meter.total_usd == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1_000_000)
    assert meter.unpriced == {"outro"}
