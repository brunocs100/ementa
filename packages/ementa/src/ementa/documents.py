"""Firm documents sent through the API: kept as restricted data and indexed by the worker.

`DocumentIntake.submit` (POST /v1/documents) stores the bytes in `firm.documents`, one row per
uploader and sha256, so sending the same file again adds nothing; then it submits the
`ingest_document` job (`ai.jobs`) and puts its id on the arq queue. The worker runs
`DocumentIngestor.ingest`: extract the text, split it under its headings, embed it with the local
model and write the chunks, marked restricted, to `firm_knowledge`, a schema of their own. The
public corpus in `knowledge` is untouched: until row-level security lands (F6), nothing that
searches it can reach firm data, and `owner_id`, `acl` and `secrecy_level` are ready for the
policies.
"""

import asyncio
import hashlib
import io
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ementa.ingest.index import embed_passages, passage
from ementa_kit.jobs import Job, JobQueue, JobStatus, PgJobStore
from ementa_kit.policy import DataClass
from ementa_kit.rag import Chunk, Embedder, PgHybridStore
from ementa_kit.telemetry import get_tracer

INGEST_JOB = "ingest_document"
FIRM_KNOWLEDGE = "firm_knowledge"  # schema of the chunks of firm documents
MAX_BYTES = 2 * 1024 * 1024
CHUNK_CHARS = 1500

tracer = get_tracer(__name__)


class MediaType(StrEnum):
    TEXT = "text/plain"
    MARKDOWN = "text/markdown"
    PDF = "application/pdf"


_BY_EXTENSION = {
    ".txt": MediaType.TEXT,
    ".text": MediaType.TEXT,
    ".md": MediaType.MARKDOWN,
    ".markdown": MediaType.MARKDOWN,
    ".pdf": MediaType.PDF,
}
_BY_DECLARED = {
    "text/plain": MediaType.TEXT,
    "text/markdown": MediaType.MARKDOWN,
    "text/x-markdown": MediaType.MARKDOWN,
    "application/pdf": MediaType.PDF,
}

Reason = Literal["empty", "too_large", "unsupported", "unreadable", "missing"]


class DocumentRejectedError(ValueError):
    """The file cannot become a document. `reason` says why; the message never quotes it."""

    def __init__(self, reason: Reason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def media_type_of(filename: str, declared: str | None) -> MediaType | None:
    """By extension first (browsers often send a .md as application/octet-stream), then by the
    declared type."""
    suffix = filename[filename.rfind(".") :].lower() if "." in filename else ""
    if suffix in _BY_EXTENSION:
        return _BY_EXTENSION[suffix]
    return _BY_DECLARED.get((declared or "").split(";")[0].strip().lower())


def _clean_filename(filename: str | None) -> str:
    """The base name only, without control characters, at most 255 characters."""
    base = re.split(r"[\\/]", filename or "")[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[:255] or "documento"


def decode_text(content: bytes) -> str:
    """UTF-8 text (a BOM is dropped) with LF line ends."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DocumentRejectedError(
            "unreadable", "The file is not UTF-8 text; save it as UTF-8 and send it again."
        ) from error
    if "\x00" in text:
        raise DocumentRejectedError("unreadable", "The file has binary content, not text.")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def extract_text(media_type: MediaType, content: bytes) -> str:
    """The text of a stored document. A PDF needs a text layer: a scan has none."""
    if media_type is not MediaType.PDF:
        return decode_text(content)
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            reader.decrypt("")  # many PDFs are encrypted only to set permissions
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as error:  # pypdf raises many kinds of error on a damaged file
        raise DocumentRejectedError(
            "unreadable", "The PDF could not be read: it is damaged or protected."
        ) from error
    text = "\n\n".join(page.strip() for page in pages if page.strip()).replace("\x00", "")
    if not text:
        raise DocumentRejectedError(
            "empty", "The PDF has no text layer (a scan?); OCR is not supported."
        )
    return text


@dataclass(frozen=True)
class Upload:
    filename: str
    media_type: MediaType
    content: bytes

    @classmethod
    def accept(cls, filename: str | None, declared_type: str | None, content: bytes) -> "Upload":
        """Check a file before it is stored: size, type and, for text, that it decodes."""
        name = _clean_filename(filename)
        if len(content) > MAX_BYTES:
            raise DocumentRejectedError(
                "too_large", f"The file has {len(content)} bytes; the limit is {MAX_BYTES}."
            )
        if not content:
            raise DocumentRejectedError("empty", "The file is empty.")
        media_type = media_type_of(name, declared_type)
        if media_type is None:
            raise DocumentRejectedError(
                "unsupported", "Send a text (.txt), Markdown (.md) or PDF (.pdf) file."
            )
        if media_type is MediaType.PDF:
            if not content.startswith(b"%PDF-"):
                raise DocumentRejectedError("unreadable", "The file is not a PDF.")
        elif not decode_text(content).strip():
            raise DocumentRejectedError("empty", "The file has no text.")
        return cls(name, media_type, content)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class Section:
    path: list[str]  # the Markdown headings above the text
    text: str


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")


def _cut(paragraph: str, max_chars: int) -> list[str]:
    """A paragraph longer than `max_chars`, cut after a sentence, else between two words."""
    pieces: list[str] = []
    rest = paragraph
    while len(rest) > max_chars:
        window = rest[: max_chars + 1]
        cut = max((m.end() for m in _SENTENCE_END.finditer(window)), default=0)
        if cut < max_chars // 2:  # no sentence ends in the second half: cut at a space
            cut = max(window.rfind(" "), window.rfind("\n"))
        if cut <= 0:
            cut = max_chars
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


def _pack(paragraphs: list[str], max_chars: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for part in _cut(paragraph, max_chars):
            if current and len(current) + 2 + len(part) > max_chars:
                pieces.append(current)
                current = part
            else:
                current = f"{current}\n\n{part}" if current else part
    if current:
        pieces.append(current)
    return pieces


def split_text(text: str, markdown: bool, max_chars: int = CHUNK_CHARS) -> list[Section]:
    """Pieces of at most `max_chars`, whole paragraphs when they fit, each with the Markdown
    headings above it. A heading starts a new piece; a `#` inside a code fence is not one."""
    sections: list[Section] = []
    path: list[str] = []
    paragraphs: list[str] = []
    block: list[str] = []
    in_fence = False

    def end_paragraph() -> None:
        if block:
            paragraphs.append("\n".join(block).strip())
            block.clear()

    def flush() -> None:
        end_paragraph()
        sections.extend(Section(list(path), piece) for piece in _pack(paragraphs, max_chars))
        paragraphs.clear()

    for line in text.split("\n"):
        if markdown and _FENCE.match(line):
            in_fence = not in_fence
            block.append(line.rstrip())
            continue
        heading = _HEADING.match(line) if markdown and not in_fence else None
        if heading:
            flush()
            path = [*path[: len(heading.group(1)) - 1], heading.group(2)]
        elif line.strip() or in_fence:
            block.append(line.rstrip())
        else:
            end_paragraph()
    flush()
    return [s for s in sections if s.text]


@dataclass(frozen=True)
class Document:
    id: str
    uploaded_by: str
    owner_id: str  # first reader once row-level security lands (F6)
    acl: list[str]  # who may read it; starts with the owner
    secrecy_level: DataClass
    sha256: str
    filename: str
    media_type: MediaType
    size_bytes: int
    chunks: int | None  # None until the worker indexes it
    created_at: datetime
    indexed_at: datetime | None


_SCHEMA = """
create schema if not exists {schema};
create table if not exists {schema}.documents (
    id uuid primary key,
    uploaded_by text not null,
    owner_id text not null,
    acl text[] not null,
    secrecy_level text not null default 'restricted'
        check (secrecy_level in ('public', 'restricted', 'secret')),
    sha256 text not null,
    filename text not null,
    media_type text not null,
    size_bytes integer not null,
    content bytea not null,
    chunks integer,
    created_at timestamptz not null default now(),
    indexed_at timestamptz,
    unique (uploaded_by, sha256)
);
"""

_COLUMNS = (
    "id, uploaded_by, owner_id, acl, secrecy_level, sha256, filename, media_type, size_bytes, "
    "chunks, created_at, indexed_at"
)


def _document(row: Mapping[str, Any]) -> Document:
    return Document(
        **{
            **row,
            "id": str(row["id"]),
            "secrecy_level": DataClass(row["secrecy_level"]),
            "media_type": MediaType(row["media_type"]),
        }
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class PgDocumentStore:
    """`firm.documents` on Postgres: the bytes, the uploader and the access columns."""

    def __init__(self, database_url: str, schema: str = "firm") -> None:
        self.database_url = database_url
        self.schema = schema

    def _sql(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(schema=sql.Identifier(self.schema))

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(
            self.database_url, autocommit=True, row_factory=dict_row, connect_timeout=5
        )

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(self._sql(_SCHEMA))

    def register(self, upload: Upload, user_id: str) -> tuple[Document, bool]:
        """The document of `user_id` with this content, and whether this call stored it."""
        with self._connect() as conn, conn.transaction():
            row = conn.execute(
                self._sql(
                    "insert into {schema}.documents (id, uploaded_by, owner_id, acl, sha256, "
                    "filename, media_type, size_bytes, content) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"on conflict (uploaded_by, sha256) do nothing returning {_COLUMNS}"
                ),
                (
                    uuid.uuid4(),
                    user_id,
                    user_id,
                    [user_id],
                    upload.sha256,
                    upload.filename,
                    str(upload.media_type),
                    len(upload.content),
                    upload.content,
                ),
            ).fetchone()
            if row is not None:
                return _document(row), True
            row = conn.execute(
                self._sql(
                    f"select {_COLUMNS} from {{schema}}.documents "
                    "where uploaded_by = %s and sha256 = %s"
                ),
                (user_id, upload.sha256),
            ).fetchone()
        assert row is not None  # the insert conflicted with this row
        return _document(row), False

    def get(self, document_id: str) -> Document | None:
        if not _is_uuid(document_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                self._sql(f"select {_COLUMNS} from {{schema}}.documents where id = %s"),
                (document_id,),
            ).fetchone()
        return _document(row) if row else None

    def content(self, document_id: str) -> bytes | None:
        if not _is_uuid(document_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                self._sql("select content from {schema}.documents where id = %s"), (document_id,)
            ).fetchone()
        return bytes(row["content"]) if row else None

    def mark_indexed(self, document_id: str, chunks: int) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    "update {schema}.documents set chunks = %s, indexed_at = now() where id = %s"
                ),
                (chunks, document_id),
            )


def document_chunks(document: Document, sections: list[Section]) -> list[Chunk]:
    """One restricted chunk per section, readable by the document's ACL."""
    source_id = f"doc:{document.id}"
    return [
        Chunk(
            id=f"{source_id}:{n}",
            source_id=source_id,
            title=document.filename,
            path=section.path,
            text=section.text,
            url=f"ementa://documents/{document.id}",
            doc_sha256=document.sha256,
            secrecy_level=document.secrecy_level,
            acl=list(document.acl),
        )
        for n, section in enumerate(sections, start=1)
    ]


class DocumentIngestor:
    """Indexes one stored document: text, pieces under their headings, embeddings, chunks."""

    def __init__(
        self,
        documents: PgDocumentStore,
        chunks: PgHybridStore,
        embedder: Embedder,
        max_chars: int = CHUNK_CHARS,
    ) -> None:
        self.documents = documents
        self.chunks = chunks
        self.embedder = embedder
        self.max_chars = max_chars

    def ingest(self, document_id: str) -> int:
        """Index the document and return how many chunks it has; already indexed, a no-op.

        Running it again after a crash halfway is safe: the chunks of the document are replaced
        in one transaction, never added twice.
        """
        document = self.documents.get(document_id)
        if document is None:
            raise DocumentRejectedError("missing", "The document is not stored.")
        if document.chunks is not None:
            return document.chunks
        text = extract_text(document.media_type, self.documents.content(document_id) or b"")
        markdown = document.media_type is MediaType.MARKDOWN
        sections = split_text(text, markdown=markdown, max_chars=self.max_chars)
        if not sections:
            raise DocumentRejectedError("empty", "The document has no text to index.")
        chunks = document_chunks(document, sections)
        with tracer.start_as_current_span(
            f"embeddings {self.embedder.model_name}",
            attributes={
                "gen_ai.operation.name": "embeddings",
                "gen_ai.request.model": self.embedder.model_name,
                "ementa.embedding.passages": len(chunks),
            },
        ):
            embeddings = embed_passages(self.embedder, [passage(c) for c in chunks])
        first = chunks[0]
        self.chunks.upsert_source(
            first.source_id,
            document.filename,
            first.url,
            document.created_at.date().isoformat(),
            document.sha256,
            None,
        )
        written = self.chunks.replace_chunks(
            first.source_id, chunks, embeddings, self.embedder.model_name
        )
        self.documents.mark_indexed(document.id, written)
        return written


@dataclass(frozen=True)
class Submission:
    job: Job
    document: Document
    duplicate: bool  # the uploader had sent this file before


class DocumentIntake:
    """What POST /v1/documents runs: store the upload, submit its job, put the job on the queue."""

    def __init__(self, documents: PgDocumentStore, jobs: PgJobStore, queue: JobQueue) -> None:
        self.documents = documents
        self.jobs = jobs
        self.queue = queue

    def _register(self, upload: Upload, user_id: str) -> tuple[Document, bool, Job]:
        document, created = self.documents.register(upload, user_id)
        return document, created, self.jobs.submit(INGEST_JOB, document.id, user_id)

    async def submit(self, upload: Upload, user_id: str) -> Submission:
        """Store and queue the file; a file sent again by the same user adds nothing.

        The job goes to the queue unless it already succeeded: arq ignores an id it holds, and a
        queue that lost the job (Redis restarted) gets it back.
        """
        document, created, job = await asyncio.to_thread(self._register, upload, user_id)
        if job.status is not JobStatus.SUCCEEDED:
            await self.queue.enqueue(INGEST_JOB, job.id, document_id=document.id)
        return Submission(job, document, duplicate=not created)

    def job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)
