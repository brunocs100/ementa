from pathlib import Path

from ementa.prompts import PROMPTS_DIR, prompt_registry
from ementa_kit.prompts import load_prompt


def test_every_prompt_file_is_in_the_registry() -> None:
    files = sorted(PROMPTS_DIR.rglob("*.md"))

    prompts = prompt_registry().prompts()  # fails on a duplicate or a missing changelog

    assert files
    assert len(prompts) == len(files)


def test_prompt_files_are_named_after_their_id_and_version() -> None:
    for path in PROMPTS_DIR.rglob("*.md"):
        prompt = load_prompt(path)
        agent, name = prompt.id.split(".", 1)
        assert path.relative_to(PROMPTS_DIR) == Path(agent, f"{name}.v{prompt.version}.md")
