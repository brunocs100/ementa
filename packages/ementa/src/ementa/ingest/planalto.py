"""Parser for consolidated statutes published by the Planalto (planalto.gov.br/ccivil_03).

Each `<p>` holds one provision: article, paragraph, inciso or alínea. Struck text is old wording
and is dropped. Amendment notes such as "(Incluído pela Lei nº 14.181, de 2021)" leave the text
and go to `notes`. Text of another law quoted by an amending article stays inside that article.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser

HEADING_LEVELS = {"TÍTULO": 0, "CAPÍTULO": 1, "SEÇÃO": 2, "Seção": 2, "Subseção": 3}

_ARTICLE = re.compile(r"^Art\.\s*(\d+)\s*[º°o]?\s*(?:-\s*([A-Z]))?\s*[.\-–]?\s*(.*)$")
_PARAGRAPH = re.compile(r"^§\s*(\d+)\s*\.?\s*[º°o]?\s*\.?\s*[-–]?\s*(.*)$")
_SOLE_PARAGRAPH = re.compile(r"^Parágrafo\s+único\s*\.?\s*[-–]?\s*(.*)$", re.IGNORECASE)
_INCISO = re.compile(r"^([IVXLC]+)\s*[-–]\s*(.*)$")
_ALINEA = re.compile(r"^([a-z])\)\s*(.*)$")
_HEADING = re.compile(r"^(TÍTULO|CAPÍTULO|SEÇÃO|Seção|Subseção)\s+([IVXLC]+(?:-[A-Z])?)\b\s*(.*)$")
_NOTE = re.compile(
    r"\s*\((?:Redação dada|Incluíd[oa]|Acrescid[oa]|Revogad[oa]|Vide|Renumerad[oa]|Vigência)"
    r"[^)]*\)"
)
_LINK_WORD = re.compile(r"\s+Vigência$")  # leftover text of a "Vigência" link
_VETOED = re.compile(r"^\(?\s*vetad[oa]\s*\)?\s*[.;]?$", re.IGNORECASE)
_END = re.compile(r"^Brasília,\s")
_OPEN_QUOTE = ('"', "“")
_CLOSE_QUOTE = ('"', "”")


@dataclass
class Provision:
    id: str
    kind: str  # article, paragraph, inciso, alinea
    label: str
    text: str
    notes: list[str] = field(default_factory=list)
    vetoed: bool = False


@dataclass
class Article:
    id: str
    number: str
    path: list[str]
    provisions: list[Provision] = field(default_factory=list)

    @property
    def vetoed(self) -> bool:
        return self.provisions[0].vetoed

    def render(self) -> str:
        """Full text of the article, one provision per line, in the statute's own style."""
        return "\n".join(f"{_lead(p, self.number)} {p.text}".strip() for p in self.provisions)


def _lead(provision: Provision, number: str) -> str:
    if provision.kind == "article":
        ordinal = number.isdigit() and int(number) < 10
        return f"Art. {number}º" if ordinal else f"Art. {number}."
    if provision.label == "Parágrafo único":
        return "Parágrafo único."
    return provision.label


class _Paragraphs(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._current: list[str] | None = None
        self._struck = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p":
            self._current = []
        elif tag in ("strike", "s", "del"):
            self._struck += 1
        elif tag == "br" and self._current is not None:
            self._current.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("strike", "s", "del"):
            self._struck = max(0, self._struck - 1)
        elif tag == "p" and self._current is not None:
            text = _clean("".join(self._current))
            if text:
                self.paragraphs.append(text)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None and not self._struck:
            self._current.append(data)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\xa0", " "))
    return re.sub(r"\s+", " ", text).strip()


def _split_notes(text: str) -> tuple[str, list[str]]:
    notes = [n.strip() for n in _NOTE.findall(text)]
    return _LINK_WORD.sub("", _clean(_NOTE.sub("", text))), notes


def _closes_quote(text: str) -> bool:
    return text.rstrip(" .;:").endswith(_CLOSE_QUOTE)


def paragraphs(html: str) -> list[str]:
    parser = _Paragraphs()
    parser.feed(html)
    return parser.paragraphs


def parse_statute(html: str, prefix: str) -> list[Article]:
    """Articles of a Planalto statute page, with canonical ids such as `cdc:art49:pu`."""
    articles: list[Article] = []
    headings: list[tuple[int, str]] = []
    heading_open = False
    article: Article | None = None
    parent_of_inciso: str | None = None
    last_inciso: str | None = None
    quote_open = False

    for raw in paragraphs(html):
        if _END.match(raw):
            break
        text, notes = _split_notes(raw)
        if not text:
            if article and article.provisions:
                article.provisions[-1].notes.extend(notes)
            continue

        if quote_open and article:
            # Text of another law quoted by an amending article (CDC arts. 110 to 117).
            article.provisions[-1].text += f" {text}"
            quote_open = not _closes_quote(text)
            continue

        if m := _HEADING.match(text):
            level = HEADING_LEVELS[m.group(1)]
            headings = [h for h in headings if h[0] < level]
            headings.append((level, f"{m.group(1).capitalize()} {m.group(2)} {m.group(3)}".strip()))
            heading_open = not m.group(3)
            continue

        if m := _ARTICLE.match(text):
            number = m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")
            art_id = f"{prefix}:art{number}"
            article = Article(id=art_id, number=number, path=[h[1] for h in headings])
            articles.append(article)
            body = m.group(3)
            article.provisions.append(
                Provision(
                    art_id, "article", f"Art. {number}", body, notes, bool(_VETOED.match(body))
                )
            )
            parent_of_inciso, last_inciso, heading_open = art_id, None, False
            continue

        if heading_open and headings:
            # Heading title on its own line, such as "DA PREVENÇÃO E DO TRATAMENTO ...".
            level, title = headings[-1]
            headings[-1] = (level, f"{title} {text}")
            heading_open = False
            continue

        if article is None:
            continue  # preamble

        provision: Provision | None = None
        if m := _SOLE_PARAGRAPH.match(text):
            pid = f"{article.id}:pu"
            provision = Provision(pid, "paragraph", "Parágrafo único", m.group(1), notes)
            parent_of_inciso, last_inciso = pid, None
        elif m := _PARAGRAPH.match(text):
            pid = f"{article.id}:p{m.group(1)}"
            provision = Provision(pid, "paragraph", f"§ {m.group(1)}º", m.group(2), notes)
            parent_of_inciso, last_inciso = pid, None
        elif (m := _INCISO.match(text)) and parent_of_inciso:
            pid = f"{parent_of_inciso}:{m.group(1)}"
            provision = Provision(pid, "inciso", f"{m.group(1)} -", m.group(2), notes)
            last_inciso = pid
        elif (m := _ALINEA.match(text)) and last_inciso:
            pid = f"{last_inciso}:{m.group(1)}"
            provision = Provision(pid, "alinea", f"{m.group(1)})", m.group(2), notes)

        if provision is None:
            # Continuation of the previous provision: penalties, quoted text of amended laws.
            last = article.provisions[-1]
            last.text = f"{last.text} {text}".strip()
            last.notes.extend(notes)
            if text.startswith(_OPEN_QUOTE):
                quote_open = not _closes_quote(text)
            continue

        provision.vetoed = bool(_VETOED.match(provision.text))
        article.provisions.append(provision)

    return articles
