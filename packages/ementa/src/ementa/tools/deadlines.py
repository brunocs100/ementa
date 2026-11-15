from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ementa.domain import deadlines
from ementa_kit.policy import DataClass

DATA_CLASS = DataClass.PUBLIC

DESCRIPTION = (
    "Calcula o último dia de um prazo processual em dias úteis (CPC, arts. 219, 220 e 224), "
    "com feriados nacionais, Sexta-feira Santa, feriados do estado e a suspensão de 20/12 a 20/01. "
    "Devolve o vencimento, os dias contados, os dias pulados com o motivo e as premissas. "
    "Use sempre esta ferramenta para prazos: nunca calcule de cabeça."
)


class CalculateDeadlineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date = Field(
        description="Dia do começo do prazo (por exemplo, a intimação), AAAA-MM-DD"
    )
    business_days: int = Field(
        ge=1, le=deadlines.MAX_BUSINESS_DAYS, description="Prazo em dias úteis"
    )
    state: Literal["RS"] = Field(default="RS", description="UF do tribunal")
    extra_closed_days: list[date] = Field(
        default_factory=list,
        max_length=60,
        description="Dias sem expediente no tribunal, além dos feriados (municipais, forenses)",
    )


class SkippedDay(BaseModel):
    day: date
    reason: str


class CalculateDeadlineOutput(BaseModel):
    due_date: date
    start_date: date
    business_days: int
    counted_days: list[date]
    skipped_days: list[SkippedDay]
    premises: list[str]
    data_class: DataClass = DATA_CLASS
    notice: str = "Rascunho para conferência do advogado responsável."


def calculate_deadline(request: CalculateDeadlineInput) -> CalculateDeadlineOutput:
    result = deadlines.calculate_deadline(
        request.start_date,
        request.business_days,
        state=request.state,
        extra_closed_days=request.extra_closed_days,
    )
    return CalculateDeadlineOutput(
        due_date=result.due_date,
        start_date=result.start_date,
        business_days=result.business_days,
        counted_days=list(result.counted_days),
        skipped_days=[SkippedDay(day=d, reason=r) for d, r in result.skipped_days],
        premises=list(result.premises),
    )
