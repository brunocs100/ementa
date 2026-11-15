"""Versioned prompts of the legal agents. Changing one means running the evaluation."""

from functools import lru_cache
from pathlib import Path

from ementa_kit.prompts import Prompt, PromptRegistry

PROMPTS_DIR = Path(__file__).parent


@lru_cache
def prompt_registry() -> PromptRegistry:
    """Every prompt file under prompts/, loaded and checked once."""
    return PromptRegistry.from_dir(PROMPTS_DIR)


def get_prompt(prompt_id: str, version: int) -> Prompt:
    """`legal_research.answer`, 1 -> the prompt in prompts/legal_research/answer.v1.md"""
    return prompt_registry().get(prompt_id, version)
