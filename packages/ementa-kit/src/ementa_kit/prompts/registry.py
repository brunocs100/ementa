import re
from pathlib import Path
from typing import Self

from ementa_kit.prompts.loader import Prompt, load_prompt


class PromptRegistry:
    """Every prompt of an app by id and version, checked as it is built.

    It fails fast on two prompts with the same id and version, and on a prompt whose changelog
    has no entry for its own version (`v2 ...` in the changelog of version 2).
    """

    def __init__(self) -> None:
        self._prompts: dict[tuple[str, int], Prompt] = {}
        self._sources: dict[tuple[str, int], str] = {}

    @classmethod
    def from_dir(cls, root: Path) -> Self:
        """Load every `*.md` under `root` as a prompt."""
        registry = cls()
        for path in sorted(root.rglob("*.md")):
            registry.add(load_prompt(path), source=path.relative_to(root).as_posix())
        return registry

    def add(self, prompt: Prompt, source: str = "") -> None:
        key = (prompt.id, prompt.version)
        where = source or f"{prompt.id} v{prompt.version}"
        if key in self._prompts:
            raise ValueError(
                f"{where}: {prompt.id} v{prompt.version} is already in {self._sources[key]}"
            )
        if not re.search(rf"\bv{prompt.version}\b", prompt.changelog):
            raise ValueError(f"{where}: the changelog has no entry for v{prompt.version}")
        self._prompts[key] = prompt
        self._sources[key] = where

    def get(self, prompt_id: str, version: int) -> Prompt:
        try:
            return self._prompts[(prompt_id, version)]
        except KeyError:
            raise LookupError(f"unknown prompt {prompt_id} v{version}") from None

    def prompts(self) -> list[Prompt]:
        """Every prompt, by id and version: what the registry lists."""
        return [self._prompts[key] for key in sorted(self._prompts)]
