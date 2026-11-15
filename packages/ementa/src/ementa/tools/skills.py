"""Schemas of `list_skills` and `load_skill`: the runtime skills, one level of detail at a time."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from ementa_kit.policy import DataClass
from ementa_kit.skills import MAX_NAME, NAME_PATTERN, Skill, SkillSummary

DATA_CLASS = DataClass.PUBLIC

LIST_DESCRIPTION = (
    "Lista as skills da Ementa só com nome e descrição. Uma skill ensina a fazer uma tarefa "
    "jurídica com as ferramentas deste servidor, como pesquisar com citação ou calcular prazo. "
    "Quando a descrição cobrir o pedido, leia as instruções com load_skill antes de começar."
)

LOAD_DESCRIPTION = (
    "Devolve as instruções completas de uma skill pelo nome que list_skills mostra, como "
    "legal-research ou deadline-calculation. Siga as instruções ao fazer a tarefa."
)


class SkillSummaryOut(BaseModel):
    name: str
    description: str


class ListSkillsOutput(BaseModel):
    skills: list[SkillSummaryOut]
    data_class: DataClass = DATA_CLASS

    @classmethod
    def from_index(cls, index: list[SkillSummary]) -> Self:
        return cls(skills=[SkillSummaryOut(name=s.name, description=s.description) for s in index])


class LoadSkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=MAX_NAME, pattern=NAME_PATTERN)


class LoadSkillOutput(BaseModel):
    name: str
    description: str
    instructions: str
    data_class: DataClass = DATA_CLASS

    @classmethod
    def from_skill(cls, skill: Skill) -> Self:
        return cls(name=skill.name, description=skill.description, instructions=skill.body)
