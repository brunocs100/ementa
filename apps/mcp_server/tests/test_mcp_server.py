import re
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


def test_server_lists_the_tools() -> None:
    async def scenario() -> list[str]:
        async with Client(build_server()) as client:
            tools = await client.list_tools()
            return sorted(tool.name for tool in tools.tools)

    assert anyio.run(scenario) == [
        "answer_legal_question",
        "calculate_deadline",
        "get_statute_article",
        "list_skills",
        "load_skill",
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


def test_list_skills_shows_only_name_and_description() -> None:
    result = call("list_skills", {})

    assert not result.is_error
    data = result.structured_content
    assert [s["name"] for s in data["skills"]] == ["deadline-calculation", "legal-research"]
    assert all(set(s) == {"name", "description"} for s in data["skills"])
    assert data["data_class"] == "public"


def test_load_skill_returns_the_instructions() -> None:
    result = call("load_skill", {"name": "deadline-calculation"})

    assert not result.is_error
    data = result.structured_content
    assert data["name"] == "deadline-calculation"
    assert data["instructions"].startswith("# Cálculo de prazo")
    assert "calculate_deadline" in data["instructions"]


def test_load_skill_reports_unknown_skills_and_rejects_bad_names() -> None:
    unknown = call("load_skill", {"name": "contract-review"})

    assert unknown.is_error
    assert (
        "Skill desconhecida: contract-review. Disponíveis: deadline-calculation, legal-research."
        in unknown.content[0].text
    )
    assert call("load_skill", {"name": "../legal-research"}).is_error


def schema_fields(schema: Any) -> set[str]:
    """Every property name in a JSON schema, nested ones included."""
    if isinstance(schema, dict):
        return set(schema.get("properties", {})).union(*map(schema_fields, schema.values()))
    if isinstance(schema, list):
        return set().union(*map(schema_fields, schema))
    return set()


def test_skills_mention_only_tools_and_fields_that_exist() -> None:
    async def scenario() -> tuple[set[str], list[str]]:
        async with Client(build_server()) as client:
            tools = (await client.list_tools()).tools
            known = {t.name for t in tools} | schema_fields(
                [[t.input_schema, t.output_schema] for t in tools]
            )
            texts = []
            for skill in (await client.call_tool("list_skills", {})).structured_content["skills"]:
                loaded = await client.call_tool("load_skill", {"name": skill["name"]})
                texts += [
                    loaded.structured_content["description"],
                    loaded.structured_content["instructions"],
                ]
            return known, texts

    known, texts = anyio.run(scenario)
    mentioned = {m for text in texts for m in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text)}

    assert {"answer_legal_question", "calculate_deadline", "start_date"} <= mentioned
    assert mentioned <= known, f"not a tool or field of the server: {sorted(mentioned - known)}"
