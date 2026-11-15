"""The firm's approved brief templates and legal opinions (pareceres), read from `data/firm/`.

A template is `templates/<id>.md`: YAML front matter (title, description, drafting guidance, the
requests every brief of the kind makes, who approved it and when), then the brief's body with
`{{placeholders}}` the agent fills. An opinion is `opinions/<id>.md`: front matter, then the text
the planner and the drafter may read. Only the body of an opinion reaches a model; the names in
the front matter stay here. A malformed file fails the whole library, naming the file.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Self

import yaml

# What the agent fills in every template: the court, the parties, the three sections the model
# writes and the place and date of the closing.
FIELDS = ("juizo", "autor", "reu", "fatos", "fundamentos", "pedidos", "local_data")

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_TEMPLATE_KEYS = {
    "id",
    "title",
    "description",
    "area",
    "approved_by",
    "approved_at",
    "fictitious",
    "guidance",
    "fixed_requests",
}
_OPINION_KEYS = {"id", "title", "area", "approved_by", "approved_at", "fictitious"}


class LibraryError(ValueError):
    """A template or an opinion outside the format."""


class UnknownTemplateError(LookupError):
    pass


@dataclass(frozen=True)
class BriefTemplate:
    id: str
    title: str
    description: str
    guidance: str
    fixed_requests: tuple[str, ...]
    body: str
    approved_by: str
    approved_at: date

    def render(self, values: Mapping[str, str]) -> str:
        """The body with each placeholder replaced, in one pass: text that comes in with a value
        is never read as a placeholder."""
        return _PLACEHOLDER.sub(lambda m: values[m.group(1)], self.body)


@dataclass(frozen=True)
class Opinion:
    id: str
    title: str
    body: str
    approved_by: str
    approved_at: date


def _read(path: Path, keys: set[str]) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    head, sep, body = raw.removeprefix("---\n").partition("\n---\n")
    if not raw.startswith("---\n") or not sep:
        raise LibraryError(f"{path}: missing YAML front matter between --- lines")
    try:
        meta = yaml.safe_load(head)
    except yaml.YAMLError as error:
        raise LibraryError(f"{path}: front matter is not valid YAML: {error}") from None
    if not isinstance(meta, dict):
        raise LibraryError(f"{path}: front matter must be a mapping")
    missing = sorted(keys - {"fixed_requests", "fictitious"} - meta.keys())
    unknown = sorted(str(k) for k in meta.keys() - keys)
    if missing or unknown:
        raise LibraryError(f"{path}: missing fields {missing}, unexpected fields {unknown}")
    if meta["id"] != path.stem or not _ID.fullmatch(str(meta["id"])):
        raise LibraryError(f"{path}: id must be the file name, in lowercase words and hyphens")
    if not isinstance(meta["approved_at"], date):
        raise LibraryError(f"{path}: approved_at must be a date (YYYY-MM-DD)")
    for key in keys - {"approved_at", "fixed_requests", "fictitious"}:
        if not isinstance(meta[key], str) or not meta[key].strip():
            raise LibraryError(f"{path}: {key} must be a non-empty text")
    if not body.strip():
        raise LibraryError(f"{path}: the body is empty")
    return meta, body.strip() + "\n"


def read_template(path: Path) -> BriefTemplate:
    meta, body = _read(path, _TEMPLATE_KEYS)
    found = _PLACEHOLDER.findall(body)
    unknown = sorted(set(found) - set(FIELDS))
    absent = [f for f in FIELDS if f not in found]
    if unknown or absent:
        raise LibraryError(f"{path}: unknown placeholders {unknown}, missing placeholders {absent}")
    requests = meta.get("fixed_requests", [])
    if not isinstance(requests, list) or not all(isinstance(r, str) and r for r in requests):
        raise LibraryError(f"{path}: fixed_requests must be a list of texts")
    return BriefTemplate(
        id=meta["id"],
        title=meta["title"].strip(),
        description=meta["description"].strip(),
        guidance=meta["guidance"].strip(),
        fixed_requests=tuple(r.strip() for r in requests),
        body=body,
        approved_by=meta["approved_by"],
        approved_at=meta["approved_at"],
    )


def read_opinion(path: Path) -> Opinion:
    meta, body = _read(path, _OPINION_KEYS)
    return Opinion(
        id=meta["id"],
        title=meta["title"].strip(),
        body=body,
        approved_by=meta["approved_by"],
        approved_at=meta["approved_at"],
    )


class BriefLibrary:
    """Templates and opinions by id, in file name order."""

    def __init__(self, templates: Iterable[BriefTemplate], opinions: Iterable[Opinion]) -> None:
        self._templates = {t.id: t for t in templates}
        self._opinions = {o.id: o for o in opinions}

    @classmethod
    def from_dir(cls, root: Path) -> Self:
        """`root/templates/*.md` and `root/opinions/*.md`. Fails on any malformed file."""
        return cls(
            [read_template(p) for p in sorted((root / "templates").glob("*.md"))],
            [read_opinion(p) for p in sorted((root / "opinions").glob("*.md"))],
        )

    def templates(self) -> list[BriefTemplate]:
        return list(self._templates.values())

    def template(self, template_id: str) -> BriefTemplate:
        try:
            return self._templates[template_id]
        except KeyError:
            raise UnknownTemplateError(template_id) from None

    def opinions(self) -> list[Opinion]:
        return list(self._opinions.values())

    def opinion(self, opinion_id: str | None) -> Opinion | None:
        return self._opinions.get(opinion_id) if opinion_id else None
