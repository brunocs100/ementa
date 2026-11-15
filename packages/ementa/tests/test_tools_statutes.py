import pytest
from pydantic import ValidationError

from ementa.tools.statutes import (
    GetStatuteArticleInput,
    UnknownSourceError,
    get_statute_article,
)


@pytest.mark.parametrize(
    "source_id",
    [
        "cdc:art49",
        "cdc:art54-A",
        "cpc:art246:p1-A:I",
        "lgpd:art65:I-A",
        "stj:sumula:385",
        "stf:sv:13",
    ],
)
def test_source_id_schema_accepts_articles_and_sumulas(source_id: str) -> None:
    assert GetStatuteArticleInput(source_id=source_id).source_id == source_id


@pytest.mark.parametrize(
    "source_id",
    ["cdc:49", "stj:sumula:", "stj:sumula:385:pu", "stf:sv:x", "CDC:art49", "cdc:art49 "],
)
def test_source_id_schema_refuses_malformed_ids(source_id: str) -> None:
    with pytest.raises(ValidationError):
        GetStatuteArticleInput(source_id=source_id)


def test_sumula_lookup_returns_its_text_and_own_url() -> None:
    out = get_statute_article(GetStatuteArticleInput(source_id="stj:sumula:479"))

    assert out.article_id == "stj:sumula:479"
    assert out.provision_text.startswith("As instituições financeiras respondem objetivamente")
    assert out.path == ["DIREITO DO CONSUMIDOR", "RESPONSABILIDADE CIVIL DO FORNECEDOR"]
    assert out.url.endswith("livre=%27479%27.num.&O=JT")
    assert out.statute == "Súmulas do Superior Tribunal de Justiça"


def test_unknown_source_and_unknown_sumula_are_reported() -> None:
    with pytest.raises(UnknownSourceError, match="fonte desconhecida"):
        get_statute_article(GetStatuteArticleInput(source_id="cc:art186"))
    with pytest.raises(UnknownSourceError, match="súmula não encontrada"):
        get_statute_article(GetStatuteArticleInput(source_id="stj:sumula:1"))
