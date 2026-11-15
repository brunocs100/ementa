"""Versioned prompts of the legal agents. Changing one means running the evaluation."""

from pathlib import Path

from ementa_kit.prompts import Prompt, load_prompt

PROMPTS_DIR = Path(__file__).parent


def get_prompt(prompt_id: str, version: int) -> Prompt:
    """`legal_research.answer`, 1 -> prompts/legal_research/answer.v1.md"""
    agent, name = prompt_id.split(".", 1)
    prompt = load_prompt(PROMPTS_DIR / agent / f"{name}.v{version}.md")
    if (prompt.id, prompt.version) != (prompt_id, version):
        raise ValueError(f"{prompt_id} v{version}: front matter says {prompt.id} v{prompt.version}")
    return prompt
