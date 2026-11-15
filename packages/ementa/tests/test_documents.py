"""Firm documents without a database: what is accepted, how text is extracted and split."""

import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ementa.documents import (
    MAX_BYTES,
    DocumentRejectedError,
    MediaType,
    Section,
    Upload,
    decode_text,
    extract_text,
    media_type_of,
    split_text,
)

# Fictitious contract, written for the tests.
CONTRACT = """# Contrato de prestação de serviços

Contrato fictício, só para testes.

## Cláusula 1 - Objeto ##

A contratada presta serviços de consultoria
jurídica ao contratante.

## Cláusula 2 - Pagamento

O pagamento é mensal.

```text
# isto não é um título
```

### Multa

Multa de 2% sobre o valor em atraso.

# Anexo

Lista de serviços.
"""


def tiny_pdf(text: str | None) -> bytes:
    """A one-page PDF with `text` in Helvetica, built by hand; None gives a page with no text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1") if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%b\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%b\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


@pytest.mark.parametrize(
    ("filename", "declared", "expected"),
    [
        ("contrato.md", "application/octet-stream", MediaType.MARKDOWN),
        ("NOTAS.TXT", None, MediaType.TEXT),
        ("parecer.markdown", "text/plain", MediaType.MARKDOWN),
        ("peticao.pdf", "application/octet-stream", MediaType.PDF),
        ("sem-extensao", "text/markdown; charset=utf-8", MediaType.MARKDOWN),
        ("sem-extensao", "text/plain", MediaType.TEXT),
        ("planilha.xlsx", "application/vnd.ms-excel", None),
        ("contrato.docx", None, None),
    ],
)
def test_media_type_comes_from_the_extension_then_the_declared_type(
    filename: str, declared: str | None, expected: MediaType | None
) -> None:
    assert media_type_of(filename, declared) is expected


def test_accepted_upload_keeps_the_base_name_and_hashes_the_bytes() -> None:
    content = CONTRACT.encode()
    upload = Upload.accept("C:\\Users\\ana\\contratos\\contrato.md", None, content)

    assert upload.filename == "contrato.md"
    assert upload.media_type is MediaType.MARKDOWN
    assert upload.sha256 == hashlib.sha256(content).hexdigest()
    assert Upload.accept(None, "text/plain", b"texto").filename == "documento"
    assert Upload.accept("peticao.pdf", None, tiny_pdf("Peticao")).media_type is MediaType.PDF


@pytest.mark.parametrize(
    ("filename", "content", "reason"),
    [
        ("vazio.txt", b"", "empty"),
        ("brancos.md", b" \n\n\t\n", "empty"),
        ("grande.txt", b"a" * (MAX_BYTES + 1), "too_large"),
        ("contrato.docx", b"PK\x03\x04", "unsupported"),
        ("latin1.txt", "contratação".encode("latin-1"), "unreadable"),
        ("binario.txt", b"abc\x00def", "unreadable"),
        ("falso.pdf", b"isto nao e um pdf", "unreadable"),
    ],
)
def test_rejected_uploads_say_why_without_quoting_the_file(
    filename: str, content: bytes, reason: str
) -> None:
    with pytest.raises(DocumentRejectedError) as raised:
        Upload.accept(filename, None, content)

    assert raised.value.reason == reason
    if content.strip():
        assert content.decode("latin-1")[:20] not in str(raised.value)


def test_text_is_utf8_without_bom_and_with_lf_line_ends() -> None:
    assert decode_text("\ufeffLinha 1\r\nLinha 2\rFim".encode()) == "Linha 1\nLinha 2\nFim"


def test_pdf_text_comes_from_its_text_layer() -> None:
    assert extract_text(MediaType.PDF, tiny_pdf("Contrato de teste")) == "Contrato de teste"


def test_pdf_without_text_or_damaged_is_refused() -> None:
    with pytest.raises(DocumentRejectedError) as scan:
        extract_text(MediaType.PDF, tiny_pdf(None))
    with pytest.raises(DocumentRejectedError) as damaged:
        extract_text(MediaType.PDF, b"%PDF-1.4\nlixo")

    assert scan.value.reason == "empty"
    assert "OCR" in str(scan.value)
    assert damaged.value.reason == "unreadable"


def test_markdown_splits_under_its_headings_and_ignores_hashes_in_code() -> None:
    sections = split_text(CONTRACT, markdown=True)

    title = "Contrato de prestação de serviços"
    assert sections == [
        Section([title], "Contrato fictício, só para testes."),
        Section(
            [title, "Cláusula 1 - Objeto"],
            "A contratada presta serviços de consultoria\njurídica ao contratante.",
        ),
        Section(
            [title, "Cláusula 2 - Pagamento"],
            "O pagamento é mensal.\n\n```text\n# isto não é um título\n```",
        ),
        Section([title, "Cláusula 2 - Pagamento", "Multa"], "Multa de 2% sobre o valor em atraso."),
        Section(["Anexo"], "Lista de serviços."),
    ]


def test_plain_text_has_no_headings_and_packs_short_paragraphs() -> None:
    text = "# não é título em texto puro\n\nPrimeiro parágrafo.\n\nSegundo parágrafo."

    assert split_text(text, markdown=False) == [
        Section([], "# não é título em texto puro\n\nPrimeiro parágrafo.\n\nSegundo parágrafo.")
    ]


def test_a_long_paragraph_is_cut_after_whole_sentences() -> None:
    paragraph = " ".join(f"Frase número {n} do contrato." for n in range(1, 40))

    sections = split_text(paragraph, markdown=False, max_chars=100)

    assert len(sections) > 1
    assert all(len(s.text) <= 100 and s.text.endswith(".") for s in sections)
    assert " ".join(s.text for s in sections) == paragraph


_word = st.text(alphabet="abcdefghijçã", min_size=1, max_size=12)
_paragraph = st.lists(st.tuples(_word, st.sampled_from([" ", "\n"])), min_size=1, max_size=40).map(
    lambda pairs: "".join(word + sep for word, sep in pairs).strip()
)
_text = st.lists(_paragraph, min_size=1, max_size=8).map("\n\n".join)


@given(text=_text, max_chars=st.integers(min_value=30, max_value=300))
def test_split_keeps_every_word_in_order_and_each_piece_within_the_limit(
    text: str, max_chars: int
) -> None:
    sections = split_text(text, markdown=False, max_chars=max_chars)

    assert all(0 < len(s.text) <= max_chars for s in sections)
    assert " ".join(s.text for s in sections).split() == text.split()
