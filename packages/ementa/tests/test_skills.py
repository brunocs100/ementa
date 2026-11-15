from ementa.services import SKILLS_DIR
from ementa_kit.skills import SkillLibrary


def test_runtime_skills_follow_the_agent_skills_format() -> None:
    library = SkillLibrary(SKILLS_DIR)  # fails on any malformed SKILL.md

    assert [s.name for s in library.index()] == ["deadline-calculation", "legal-research"]
    for summary in library.index():
        assert len(library.load_skill(summary.name).body.splitlines()) < 500
