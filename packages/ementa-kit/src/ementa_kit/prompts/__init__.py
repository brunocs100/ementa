"""Prompts as versioned files: `id` and `version` in a front matter, recorded on every span."""

from ementa_kit.prompts.loader import Prompt, load_prompt
from ementa_kit.prompts.registry import PromptRegistry

__all__ = ["Prompt", "PromptRegistry", "load_prompt"]
