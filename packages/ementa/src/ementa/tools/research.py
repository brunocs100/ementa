"""Schemas of `search_legal_corpus` and `answer_legal_question`."""

from pydantic import BaseModel, ConfigDict, Field

from ementa.agents.legal_research import NOTICE, ResearchAnswer
from ementa_kit.policy import DataClass
from ementa_kit.rag import Hit

DATA_CLASS = DataClass.PUBLIC

SEARCH_DESCRIPTION = (
    "Busca híbrida (vetorial e textual, fundida por RRF) no corpus jurídico público da Ementa, "
    "hoje o Código de Defesa do Consumidor. Devolve os artigos mais relevantes com id de "
    "fonte, texto oficial e URL. Use para achar o artigo; para a resposta citada, use "
    "answer_legal_question."
)

ANSWER_DESCRIPTION = (
    "Responde uma pergunta de Direito do Consumidor com base no CDC. O agente legal_research busca "
    "as fontes, escreve a resposta e passa pelo verificador: toda citação mostrada existe no "
    "corpus, e o que não foi verificado é removido e listado em `removed`. Não calcula prazo em "
    "dias úteis (use calculate_deadline). A saída é rascunho para o advogado conferir."
)


class SearchLegalCorpusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=500)
    k: int = Field(default=5, ge=1, le=10)


class SearchHit(BaseModel):
    source_id: str
    title: str
    path: list[str]
    text: str
    url: str
    score: float

    @classmethod
    def from_hit(cls, hit: Hit) -> "SearchHit":
        return cls(
            source_id=hit.id,
            title=hit.title,
            path=hit.path,
            text=hit.text,
            url=hit.url,
            score=round(hit.score, 6),
        )


class SearchLegalCorpusOutput(BaseModel):
    query: str
    hits: list[SearchHit]
    data_class: DataClass = DATA_CLASS


class AnswerLegalQuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=5, max_length=2000)


class CitationOut(BaseModel):
    source_id: str
    title: str
    url: str


class RemovedOut(BaseModel):
    reference: str
    reason: str


class AnswerLegalQuestionOutput(BaseModel):
    answer: str
    citations: list[CitationOut]
    removed: list[RemovedOut]
    sources: list[str]
    grounded: bool
    prompt: str
    model: str
    notice: str = NOTICE
    data_class: DataClass = DATA_CLASS

    @classmethod
    def from_answer(cls, answer: ResearchAnswer) -> "AnswerLegalQuestionOutput":
        return cls(
            answer=answer.answer,
            citations=[CitationOut(**c.__dict__) for c in answer.citations],
            removed=[RemovedOut(reference=r.reference, reason=r.reason) for r in answer.removed],
            sources=answer.sources,
            grounded=answer.grounded,
            prompt=answer.prompt,
            model=answer.model,
        )
