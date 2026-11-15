import re
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field

from ementa.ingest.corpus import Statute, load_statute
from ementa_kit.policy import DataClass

DATA_CLASS = DataClass.PUBLIC

DESCRIPTION = (
    "Devolve o texto oficial de um artigo de lei pelo id de fonte, por exemplo `cdc:art49`, "
    "`cdc:art42:pu` (parágrafo único), `cdc:art26:p2` (§ 2º) ou `cdc:art26:II` (inciso). "
    "Traz o artigo inteiro, o dispositivo pedido, a localização na lei, a URL e a data do snapshot."
)

SOURCE_ID = re.compile(r"^([a-z]+):art(\d+(?:-[A-Z])?)((?::[A-Za-z0-9]+)*)$")


class GetStatuteArticleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=SOURCE_ID.pattern, description="Id de fonte, como cdc:art49")


class StatuteArticleOutput(BaseModel):
    source_id: str
    article_id: str
    statute: str
    title: str
    path: list[str]
    text: str
    provision_text: str
    vetoed: bool
    notes: list[str]
    url: str
    retrieved_at: str
    data_class: DataClass = DATA_CLASS


class UnknownSourceError(ValueError):
    pass


@lru_cache
def _statute(source: str) -> Statute:
    return load_statute(source)


def get_statute_article(request: GetStatuteArticleInput) -> StatuteArticleOutput:
    match = SOURCE_ID.match(request.source_id)
    if match is None:  # the schema already checks; this keeps mypy and direct callers honest
        raise UnknownSourceError(f"id de fonte inválido: {request.source_id}")
    source, number = match.group(1), match.group(2)
    try:
        statute = _statute(source)
    except FileNotFoundError:
        raise UnknownSourceError(f"fonte desconhecida: {source}") from None

    article_id = f"{source}:art{number}"
    article = next((a for a in statute.articles if a.id == article_id), None)
    if article is None:
        raise UnknownSourceError(f"artigo não encontrado: {article_id}")
    provision = next((p for p in article.provisions if p.id == request.source_id), None)
    if provision is None:
        raise UnknownSourceError(f"dispositivo não encontrado: {request.source_id}")

    return StatuteArticleOutput(
        source_id=request.source_id,
        article_id=article_id,
        statute=statute.source.statute,
        title=statute.source.title,
        path=article.path,
        text=article.render(),
        provision_text=provision.text,
        vetoed=provision.vetoed,
        notes=provision.notes,
        url=statute.source.url,
        retrieved_at=statute.source.retrieved_at,
    )
