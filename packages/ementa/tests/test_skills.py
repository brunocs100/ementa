from ementa.services import SKILLS_DIR
from ementa_kit.context import approx_tokens
from ementa_kit.skills import SkillLibrary


def test_runtime_skills_follow_the_agent_skills_format() -> None:
    library = SkillLibrary(SKILLS_DIR)  # fails on any malformed SKILL.md

    assert [s.name for s in library.index()] == [
        "contract-review-services",
        "deadline-calculation",
        "initial-petition-consumer",
        "legal-research",
    ]
    for summary in library.index():
        assert len(library.load_skill(summary.name).body.splitlines()) < 500


def test_skill_index_fits_its_context_budget() -> None:
    """docs/ARQUITETURA.md gives the skill index 300 tokens."""
    assert approx_tokens(SkillLibrary(SKILLS_DIR).render_index()) <= 300
