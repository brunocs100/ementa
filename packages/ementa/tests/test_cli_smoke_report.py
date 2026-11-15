from typing import Any

from ementa.cli import _smoke_markdown, _smoke_summary
from ementa_kit.config import Settings
from ementa_kit.telemetry import CostMeter


def row(case: str, rank: int | None, cited: bool) -> dict[str, Any]:
    return {
        "id": case,
        "rank": rank,
        "top5": rank is not None,
        "cited": cited,
        "removed": 0,
        "seconds": 1.0,
        "sources": ["cdc:art49", "cdc:art26"],
        "citations": [],
    }


def test_summary_and_markdown_show_the_metrics_and_only_the_misses() -> None:
    rows = [row("cdc-01", 1, True), row("cpc-01", 3, True), row("lgpd-01", None, False)]
    meter = CostMeter()
    meter.total_usd, meter.calls = 0.0123, 6

    summary = _smoke_summary(rows, 30, False, Settings(_env_file=None), meter, stopped=True)  # type: ignore[call-arg]
    markdown = _smoke_markdown(summary, rows)

    assert (summary["top1"], summary["top5"], summary["recall_at_5"]) == (1, 2, 0.667)
    assert summary["mrr_at_5"] == round((1 + 1 / 3) / 3, 3)
    assert summary["cited"] == 2
    assert "parou no teto de custo depois de 3 de 30" in summary["line"]
    assert "| 3 de 30 | 1 | 2 (0.67) | 0.44 | 2 | 0 | US$ 0.0123 |" in markdown
    assert "lgpd-01" in markdown
    assert "cdc-01" not in markdown
    assert "Parou no teto de custo" in markdown
