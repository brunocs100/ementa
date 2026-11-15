"""`uv run ementa`: corpus ingestion, search and the legal research agent from the terminal."""

import argparse
import io
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from ementa_kit.config import Settings, get_settings
from ementa_kit.rag import FastEmbedder, PgHybridStore
from ementa_kit.telemetry import setup


def _store(settings: Settings, embedder: FastEmbedder) -> PgHybridStore:
    return PgHybridStore(settings.database_url, embedder.dim)


def _ingest(args: argparse.Namespace, settings: Settings) -> int:
    from ementa.ingest.index import ingest_statute

    embedder = FastEmbedder(settings.embedding_model)
    written = ingest_statute(args.source, _store(settings, embedder), embedder, reset=args.reset)
    print(f"{args.source}: {written} chunks ({embedder.model_name}, dim {embedder.dim})")
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


def _smoke(args: argparse.Namespace, settings: Settings) -> int:
    """Smoke questions: expected article in the top 5 and, with the agent, in the citations."""
    from ementa.knowledge import article_of
    from ementa.services import knowledge_base, legal_research_agent

    cases = [
        json.loads(line) for line in SMOKE_FILE.read_text(encoding="utf-8").splitlines() if line
    ]
    rows = []
    for case in cases:
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
    top5 = sum(r["top5"] for r in rows)
    mrr = sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows)
    top1 = sum(r["rank"] == 1 for r in rows)
    summary = f"top1 {top1}/{len(rows)}, top5 {top5}/{len(rows)}, MRR@5 {mrr:.2f}"
    if not args.retrieval_only:
        summary += f", citada {sum(r['cited'] for r in rows)}/{len(rows)}"
        summary += f", removidas {sum(r['removed'] for r in rows)}"
    rerank = settings.rerank_model if settings.rerank_enabled else "off"
    print(f"{summary} ({settings.embedding_model}, rerank {rerank})")
    return 0 if top5 == len(rows) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ementa")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="load a corpus snapshot into the knowledge base")
    ingest.add_argument("source", choices=["cdc"])
    ingest.add_argument("--reset", action="store_true", help="drop the knowledge schema first")

    search = sub.add_parser("search", help="hybrid search over the corpus")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=5)

    ask = sub.add_parser("ask", help="ask the legal_research agent")
    ask.add_argument("question")

    smoke = sub.add_parser("smoke", help="run the smoke questions in evals/smoke_cdc.jsonl")
    smoke.add_argument("--retrieval-only", action="store_true", help="search only, no model calls")

    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = get_settings()
    setup(settings)
    handlers = {"ingest": _ingest, "search": _search, "ask": _ask, "smoke": _smoke}
    return handlers[args.command](args, settings)


if __name__ == "__main__":
    sys.exit(main())
