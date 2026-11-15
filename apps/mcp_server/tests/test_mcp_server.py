from typing import Any

import anyio
from mcp import Client

from ementa.agents.legal_research import Citation, ResearchAnswer
from ementa_kit.rag import Hit
from ementa_mcp.server import build_server


def call(name: str, arguments: dict[str, Any]) -> Any:
    async def scenario() -> Any:
        async with Client(build_server()) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(scenario)


def test_server_lists_the_f1_tools() -> None:
    async def scenario() -> list[str]:
        async with Client(build_server()) as client:
            tools = await client.list_tools()
            return sorted(tool.name for tool in tools.tools)

    assert anyio.run(scenario) == [
        "answer_legal_question",
        "calculate_deadline",
        "get_statute_article",
        "search_legal_corpus",
    ]


def test_calculate_deadline_returns_structured_output() -> None:
    result = call("calculate_deadline", {"start_date": "2026-12-03", "business_days": 15})

    assert not result.is_error
    data = result.structured_content
    assert data["due_date"] == "2027-01-26"
    assert data["data_class"] == "public"
    assert any("art. 220" in p for p in data["premises"])


def test_calculate_deadline_rejects_invalid_days() -> None:
    result = call("calculate_deadline", {"start_date": "2026-12-03", "business_days": 0})

    assert result.is_error


def test_calculate_deadline_is_marked_read_only() -> None:
    async def scenario() -> Any:
        async with Client(build_server()) as client:
            tools = (await client.list_tools()).tools
            return next(t for t in tools if t.name == "calculate_deadline").annotations

    annotations = anyio.run(scenario)
    assert annotations.read_only_hint is True
    assert annotations.open_world_hint is False


def test_get_statute_article_returns_the_official_text() -> None:
    result = call("get_statute_article", {"source_id": "cdc:art49:pu"})

    assert not result.is_error
    data = result.structured_content
    assert data["article_id"] == "cdc:art49"
    assert data["provision_text"].startswith(
        "Se o consumidor exercitar o direito de arrependimento"
    )
    assert data["text"].startswith("Art. 49. O consumidor pode desistir")
    assert data["url"].startswith("https://www.planalto.gov.br/")


def test_get_statute_article_reports_unknown_ids() -> None:
    assert call("get_statute_article", {"source_id": "cdc:art999"}).is_error
    assert call("get_statute_article", {"source_id": "stj:sumula:385"}).is_error


HIT = Hit(
    "cdc:art49",
    "cdc",
    "Código de Defesa do Consumidor, art. 49",
    ["Título I"],
    "Art. 49. O consumidor pode desistir do contrato, no prazo de 7 dias.",
    ["cdc:art49"],
    "https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
    score=0.032,
)


class FakeKnowledge:
    def search(self, queries: list[str], limit: int = 5) -> list[Hit]:
        assert queries == ["arrependimento"]
        return [HIT][:limit]


class FakeAgent:
    def ask(self, question: str) -> ResearchAnswer:
        return ResearchAnswer(
            question=question,
            answer="Pode desistir em 7 dias [cdc:art49].",
            citations=[Citation("cdc:art49", HIT.title, HIT.url)],
            removed=[],
            sources=["cdc:art49"],
            queries=[question],
            attempts=1,
            prompt="legal_research.answer@1",
            model="fake",
        )


def call_with_fakes(name: str, arguments: dict[str, Any]) -> Any:
    async def scenario() -> Any:
        server = build_server(knowledge=FakeKnowledge, agent=FakeAgent)
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(scenario)


def test_search_legal_corpus_returns_hits_with_source_ids() -> None:
    result = call_with_fakes("search_legal_corpus", {"query": "arrependimento", "k": 3})

    assert not result.is_error
    [hit] = result.structured_content["hits"]
    assert hit["source_id"] == "cdc:art49"
    assert hit["url"].startswith("https://www.planalto.gov.br/")


def test_answer_legal_question_returns_verified_citations_and_notice() -> None:
    result = call_with_fakes("answer_legal_question", {"question": "Posso desistir da compra?"})

    assert not result.is_error
    data = result.structured_content
    assert data["grounded"] is True
    assert data["citations"][0]["source_id"] == "cdc:art49"
    assert data["notice"].startswith("Rascunho")
    assert data["data_class"] == "public"


def test_answer_legal_question_validates_the_question() -> None:
    assert call_with_fakes("answer_legal_question", {"question": "oi"}).is_error
