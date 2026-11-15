from dataclasses import dataclass
from pathlib import Path
from string import Template


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    changelog: str
    text: str

    def render(self, **values: str) -> str:
        """Fill `$name` placeholders. Raises KeyError if one is missing."""
        return Template(self.text).substitute(values)


def load_prompt(path: Path) -> Prompt:
    """Read a prompt file: `---`, lines `key: value` (id, version, changelog), `---`, body."""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"{path}: missing front matter")
    header, _, body = raw[4:].partition("\n---\n")
    meta: dict[str, str] = {}
    for line in header.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    missing = {"id", "version", "changelog"} - meta.keys()
    if missing:
        raise ValueError(f"{path}: front matter lacks {sorted(missing)}")
    return Prompt(meta["id"], int(meta["version"]), meta["changelog"], body.strip() + "\n")
