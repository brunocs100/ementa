"""Versioned corpus snapshots: each source has its raw files and a `source.json` with URL and hash.

A source is one statute page of the Planalto (`format: planalto`), one SCON result page with
súmulas of the STJ (`stj-sumulas`), or the STF pages of the súmulas vinculantes, one file per
súmula, each listed in `files` with its own URL and hash (`stf-sumulas`). The hash of a source
with several files is the hash of their bytes joined in the order they are listed.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ementa.ingest.planalto import Article, parse_statute
from ementa.ingest.sumulas import parse_stf_sumulas, parse_stj_sumulas

DEFAULT_CORPUS_DIR = Path(__file__).resolve().parents[5] / "data" / "corpus"
SOURCE_IDS = ("cdc", "lgpd", "cpc", "stj", "stf")


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceFile:
    file: str
    url: str
    sha256: str


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    statute: str
    url: str
    encoding: str
    retrieved_at: str
    sha256: str
    file: str = ""
    license: str = ""
    format: str = "planalto"
    files: tuple[SourceFile, ...] = ()


@dataclass(frozen=True)
class Statute:
    """A source and its citable units: articles of a law, or súmulas (one provision each)."""

    source: Source
    articles: tuple[Article, ...]


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(folder: Path, source: Source) -> list[bytes]:
    if not source.files:
        raw = (folder / source.file).read_bytes()
        if (digest := _sha256(raw)) != source.sha256:
            raise SnapshotError(f"{source.id}: sha256 {digest} does not match source.json")
        return [raw]
    raws = []
    for item in source.files:
        raw = (folder / item.file).read_bytes()
        if (digest := _sha256(raw)) != item.sha256:
            raise SnapshotError(f"{source.id}/{item.file}: sha256 {digest} does not match")
        raws.append(raw)
    if (digest := _sha256(b"".join(raws))) != source.sha256:
        raise SnapshotError(f"{source.id}: sha256 {digest} of the joined files does not match")
    return raws


def load_statute(source_id: str, corpus_dir: Path = DEFAULT_CORPUS_DIR) -> Statute:
    """Parse a snapshot after checking its hashes against `source.json`."""
    folder = corpus_dir / "raw" / source_id
    meta = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    files = tuple(SourceFile(**item) for item in meta.pop("files", []))
    source = Source(files=files, **{k: meta[k] for k in Source.__dataclass_fields__ if k in meta})
    texts = [raw.decode(source.encoding) for raw in _read(folder, source)]
    if source.format == "planalto":
        articles = parse_statute(texts[0], source.id)
    elif source.format == "stj-sumulas":
        articles = parse_stj_sumulas(texts[0], source.id)
    elif source.format == "stf-sumulas":
        pages = zip(texts, (item.url for item in source.files), strict=True)
        articles = parse_stf_sumulas(pages, source.id)
    else:
        raise SnapshotError(f"{source_id}: unknown format {source.format!r}")
    return Statute(source, tuple(articles))
