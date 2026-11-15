"""Versioned corpus snapshots: each source has a raw file and a `source.json` with URL and hash."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ementa.ingest.planalto import Article, parse_statute

DEFAULT_CORPUS_DIR = Path(__file__).resolve().parents[5] / "data" / "corpus"


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    statute: str
    url: str
    file: str
    encoding: str
    retrieved_at: str
    sha256: str


@dataclass(frozen=True)
class Statute:
    source: Source
    articles: tuple[Article, ...]


def load_statute(source_id: str, corpus_dir: Path = DEFAULT_CORPUS_DIR) -> Statute:
    """Parse a snapshot after checking its hash against `source.json`."""
    folder = corpus_dir / "raw" / source_id
    meta = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    source = Source(**{k: meta[k] for k in Source.__dataclass_fields__})
    raw = (folder / source.file).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != source.sha256:
        raise SnapshotError(f"{source_id}: sha256 {digest} does not match source.json")
    return Statute(source, tuple(parse_statute(raw.decode(source.encoding), source.id)))
