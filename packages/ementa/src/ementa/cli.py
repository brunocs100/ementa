"""`uv run ementa`: corpus ingestion, search and the legal research agent from the terminal."""

import argparse
import io
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ementa.ingest.corpus import SOURCE_IDS
from ementa_kit.config import Settings, get_settings
from ementa_kit.rag import FastEmbedder, PgHybridStore
from ementa_kit.telemetry import setup

if TYPE_CHECKING:
    from ementa_kit.telemetry import CostMeter


def _store(settings: Settings, embedder: FastEmbedder) -> PgHybridStore:
    return PgHybridStore(settings.database_url, embedder.dim)


def _ingest(args: argparse.Namespace, settings: Settings) -> int:
    from ementa.ingest.index import ingest_statute

    embedder = FastEmbedder(settings.embedding_model)
    store = _store(settings, embedder)
    sources = SOURCE_IDS if args.source == "all" else (args.source,)
    for n, source in enumerate(sources):
        written = ingest_statute(source, store, embedder, reset=args.reset and n == 0)
        print(f"{source}: {written} chunks ({embedder.model_name}, dim {embedder.dim})")
    return 0


def _search(args: argparse.Namespace, settings: Settings) -> int:
    from ementa.services import knowledge_base

    for rank, hit in enumerate(knowledge_base().search([args.query], limit=args.k), start=1):
        first_line = hit.text.splitlines()[0][:110]
        print(
            f"{rank}. {hit.id:<14} {hit.score:.4f} "
            f"v={hit.vector_rank} t={hit.text_rank}  {first_line}"
        )
    return 0


def _ask(args: argparse.Namespace, settings: Settings) -> int:
    from ementa.services import legal_research_agent

    answer = legal_research_agent().ask(args.question)
    print(answer.answer)
    print()
    for c in answer.citations:
        print(f"[{c.source_id}] {c.title} {c.url}")
    for r in answer.removed:
        print(f"removido: {r.reference} ({r.reason})")
    print()
    print(f"{answer.notice} {answer.prompt}, {answer.model}, tentativas {answer.attempts}")
    return 0


SMOKE_FILE = Path(__file__).resolve().parents[4] / "evals" / "smoke_cdc.jsonl"


STOPPED_AT_BUDGET = 2


def _smoke(args: argparse.Namespace, settings: Settings) -> int:
    """Golden questions: expected source in the top 5 and, with the agent, in the citations.

    Exit 0 when recall@5 reaches `--min-recall` (default: every question), 1 when it does not,
    2 when the run stopped at `--max-cost-usd` before the last question.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    from ementa.knowledge import article_of
    from ementa.services import knowledge_base, legal_research_agent
    from ementa_kit.telemetry import CostMeter

    meter = CostMeter()
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(meter)

    cases = [
        json.loads(line) for line in args.file.read_text(encoding="utf-8").splitlines() if line
    ]
    rows: list[dict[str, Any]] = []
    stopped = False
    for case in cases:
        if args.max_cost_usd is not None and meter.total_usd >= args.max_cost_usd:
            stopped = True
            break
        started = time.perf_counter()
        expected = set(case["expected"])
        if args.retrieval_only:
            sources = [h.id for h in knowledge_base().search([case["question"]], limit=5)]
            cited: list[str] = []
            removed = 0
        else:
            answer = legal_research_agent().ask(case["question"])
            sources = answer.sources[:5]
            cited = [c.source_id for c in answer.citations]
            removed = len(answer.removed)
        rank = next((n for n, s in enumerate(sources, start=1) if s in expected), None)
        row = {
            "id": case["id"],
            "rank": rank,
            "top5": rank is not None,
            "cited": bool(expected & {article_of(c) for c in cited}),
            "removed": removed,
            "seconds": round(time.perf_counter() - started, 1),
            "sources": sources,
            "citations": cited,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))

    summary = _smoke_summary(rows, len(cases), args.retrieval_only, settings, meter, stopped)
    print(summary["line"])
    if args.report:
        report = {"file": str(args.file), "summary": summary, "rows": rows}
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(_smoke_markdown(summary, rows), encoding="utf-8")
    if stopped:
        return STOPPED_AT_BUDGET
    target = args.min_recall if args.min_recall is not None else 1.0
    return 0 if summary["recall_at_5"] >= target else 1


def _smoke_summary(
    rows: list[dict[str, Any]],
    planned: int,
    retrieval_only: bool,
    settings: Settings,
    meter: "CostMeter",
    stopped: bool,
) -> dict[str, Any]:
    n = len(rows) or 1
    top1 = sum(r["rank"] == 1 for r in rows)
    top5 = sum(r["top5"] for r in rows)
    mrr = sum(1 / r["rank"] for r in rows if r["rank"]) / n
    summary: dict[str, Any] = {
        "mode": "retrieval" if retrieval_only else "agent",
        "questions": len(rows),
        "planned": planned,
        "top1": top1,
        "top5": top5,
        "recall_at_5": round(top5 / n, 3),
        "mrr_at_5": round(mrr, 3),
        "cost_usd": round(meter.total_usd, 4),
        "model_calls": meter.calls,
        "unpriced_models": sorted(meter.unpriced),
        "stopped_at_budget": stopped,
        "embedding_model": settings.embedding_model,
        "rerank": settings.rerank_model if settings.rerank_enabled else "off",
    }
    line = (
        f"top1 {top1}/{len(rows)}, top5 {top5}/{len(rows)} (recall@5 {summary['recall_at_5']:.2f}),"
        f" MRR@5 {summary['mrr_at_5']:.2f}"
    )
    if not retrieval_only:
        summary["cited"] = sum(r["cited"] for r in rows)
        summary["removed"] = sum(r["removed"] for r in rows)
        line += f", citada {summary['cited']}/{len(rows)}, removidas {summary['removed']}"
        line += f", US$ {meter.total_usd:.4f} em {meter.calls} chamadas"
    if stopped:
        line += f"; parou no teto de custo depois de {len(rows)} de {planned} perguntas"
    summary["line"] = f"{line} ({settings.embedding_model}, rerank {summary['rerank']})"
    return summary


def _smoke_markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    title = "Busca (sem modelo)" if summary["mode"] == "retrieval" else "Agente (modelo real)"
    lines = [
        f"### {title}",
        "",
        "| Perguntas | Top 1 | Top 5 (recall@5) | MRR@5 | Citada | Removidas | Custo |",
        "|---|---|---|---|---|---|---|",
        f"| {summary['questions']} de {summary['planned']} | {summary['top1']} "
        f"| {summary['top5']} ({summary['recall_at_5']:.2f}) | {summary['mrr_at_5']:.2f} "
        f"| {summary.get('cited', '-')} | {summary.get('removed', '-')} "
        f"| US$ {summary['cost_usd']:.4f} |",
        "",
    ]
    if summary["stopped_at_budget"]:
        lines += ["Parou no teto de custo antes da última pergunta.", ""]
    misses = [r for r in rows if not r["top5"] or (summary["mode"] == "agent" and not r["cited"])]
    if misses:
        lines += ["| Errou | Posição | Fontes |", "|---|---|---|"]
        lines += [
            f"| {r['id']} | {r['rank'] or '-'} | {', '.join(r['sources'][:5])} |" for r in misses
        ]
        lines.append("")
    lines.append(f"Embedding `{summary['embedding_model']}`, rerank `{summary['rerank']}`.")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ementa")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="load a corpus snapshot into the knowledge base")
    ingest.add_argument("source", choices=[*SOURCE_IDS, "all"])
    ingest.add_argument("--reset", action="store_true", help="drop the knowledge schema first")

    search = sub.add_parser("search", help="hybrid search over the corpus")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=5)

    ask = sub.add_parser("ask", help="ask the legal_research agent")
    ask.add_argument("question")

    smoke = sub.add_parser("smoke", help="run the questions of an evals/*.jsonl file")
    smoke.add_argument("--retrieval-only", action="store_true", help="search only, no model calls")
    smoke.add_argument(
        "--file", type=Path, default=SMOKE_FILE, help="question file (default: smoke_cdc.jsonl)"
    )
    smoke.add_argument(
        "--min-recall", type=float, help="pass when recall@5 reaches this (default: all)"
    )
    smoke.add_argument(
        "--max-cost-usd", type=float, help="stop before the next question past this model cost"
    )
    smoke.add_argument("--report", type=Path, help="write the rows and the summary as JSON")
    smoke.add_argument("--markdown", type=Path, help="write a Markdown table for a PR comment")

    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = get_settings()
    setup(settings)
    handlers = {"ingest": _ingest, "search": _search, "ask": _ask, "smoke": _smoke}
    return handlers[args.command](args, settings)


if __name__ == "__main__":
    sys.exit(main())
