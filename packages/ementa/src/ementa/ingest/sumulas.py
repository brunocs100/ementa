"""Parsers for súmulas: the STJ list in SCON and the STF pages of the súmulas vinculantes.

Each súmula becomes one `Article` with a single provision of kind `sumula`, so the index, the
lookup tool and the agent treat it like an article: id `stj:sumula:385` or `stf:sv:13`, the
official text, the branch and topic as path, and the court's own page as URL.

- STJ (SCON, `toc.jsp`): one `div.gridSumula` per súmula, with the number (`numeroSumula`) and,
  in `div.blocoVerbete`, the branch and topic (`ramoSumula`, "DIREITO DO CONSUMIDOR - PLANO DE
  SAÚDE"), the text and the reference "(SEGUNDA SEÇÃO, julgado em ..., DJe ...)", kept as a note.
  Most entries wrap the text in `span.clsVerbete`; newer ones (675) do not, so the text is what
  lies between the branch and the reference, checked against `clsVerbete` when there is one. The
  "em vigor" filter of SCON only leaves out cancelled súmulas; one marked "SÚMULA REVOGADA" after
  the reference (357) is left out here.
- STF (`sumariosumulas.asp`): one page per súmula vinculante; the text is the first `div.parCOM`
  after the title "Súmula Vinculante N". A cancelled súmula, marked "(cancelada)" in the title,
  and a number still pending publication are left out.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser

from ementa.ingest.planalto import Article, Provision

STJ_URL = "https://processo.stj.jus.br/SCON/sumstj/toc.jsp?livre=%27{number}%27.num.&O=JT"

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
_REFERENCE = re.compile(r"\([^()]*julgado em[^()]*\)")
_NOT_IN_FORCE = re.compile(r"\bS[ÚU]MULA (?:REVOGADA|CANCELADA)\b", re.IGNORECASE)
_SV_TITLE = re.compile(r"^Súmula Vinculante (\d+)\b\s*(.*)$")
_PUBLISHED = re.compile(r"Data de publicação do enunciado: DJE? de [\d-]+")


class SumulaParseError(ValueError):
    pass


@dataclass
class _Open:
    name: str
    tag: str
    depth: int = 1
    parts: list[str] = field(default_factory=list)


class _ClassText(HTMLParser):
    """Text of the elements that carry one of `watched` classes, in the order they close."""

    def __init__(self, watched: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.watched = watched
        self.found: list[tuple[str, str]] = []
        self._open: list[_Open] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            return
        for element in self._open:
            if element.tag == tag:
                element.depth += 1
        classes = (dict(attrs).get("class") or "").split()
        name = next((c for c in classes if c in self.watched), None)
        if name:
            self._open.append(_Open(name, tag))

    def handle_endtag(self, tag: str) -> None:
        for element in list(self._open):
            if element.tag == tag:
                element.depth -= 1
                if element.depth == 0:
                    self._open.remove(element)
                    self.found.append((element.name, _clean("".join(element.parts))))

    def handle_data(self, data: str) -> None:
        for element in self._open:
            element.parts.append(data)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\xa0", " ").replace("​", ""))
    return re.sub(r"\s+", " ", text).strip()


def _sumula(sid: str, number: str, label: str, title: str, text: str, url: str) -> Article:
    provision = Provision(sid, "sumula", label, text)
    return Article(sid, number, [], [provision], title=title, url=url)


def parse_stj_sumulas(html: str, prefix: str = "stj") -> list[Article]:
    """Súmulas of a SCON result page in number order, with ids such as `stj:sumula:385`."""
    parser = _ClassText({"gridSumula", "numeroSumula", "ramoSumula", "clsVerbete", "blocoVerbete"})
    parser.feed(html)
    sumulas: list[Article] = []
    fields: dict[str, str] = {}
    for name, text in parser.found:
        if name != "gridSumula":
            fields[name] = text
            continue
        if (sumula := _stj_sumula(fields, prefix)) is not None:
            sumulas.append(sumula)
        fields = {}
    return sorted(sumulas, key=lambda s: int(s.number))


def _stj_sumula(fields: dict[str, str], prefix: str) -> Article | None:
    missing = {"numeroSumula", "ramoSumula", "blocoVerbete"} - fields.keys()
    if missing:
        raise SumulaParseError(f"súmula without {sorted(missing)}: {fields}")
    number, branch, block = fields["numeroSumula"], fields["ramoSumula"], fields["blocoVerbete"]
    if not block.startswith(branch):
        raise SumulaParseError(f"súmula {number}: the block does not start with its branch")
    body = block[len(branch) :].strip()
    references = list(_REFERENCE.finditer(body))
    if not references:
        raise SumulaParseError(f"súmula {number}: no '(..., julgado em ...)' reference")
    reference = references[-1]
    text, tail = body[: reference.start()].strip(), body[reference.end() :].strip()
    if "clsVerbete" in fields and fields["clsVerbete"] != text:
        raise SumulaParseError(f"súmula {number}: text differs from its clsVerbete span")
    if _NOT_IN_FORCE.search(tail):
        return None
    sid = f"{prefix}:sumula:{number}"
    title = f"Súmula {number} do STJ"
    sumula = _sumula(sid, number, title, title, text, STJ_URL.format(number=number))
    sumula.path = [part.strip() for part in branch.split(" - ", 1)]
    sumula.provisions[0].notes.extend(n for n in (reference.group(0), tail) if n)
    return sumula


def parse_stf_sumula(html: str, url: str, prefix: str = "stf") -> Article | None:
    """The súmula vinculante of one STF page, or None when it is cancelled or not published."""
    parser = _ClassText({"titulo", "parCOM"})
    parser.feed(html)
    titles = [text for name, text in parser.found if name == "titulo"]
    bodies = [text for name, text in parser.found if name == "parCOM"]
    match = _SV_TITLE.match(titles[0]) if titles else None
    if match is None or not bodies:
        raise SumulaParseError(f"{url}: no 'Súmula Vinculante N' title followed by its text")
    number, marker = match.groups()
    text = bodies[0]
    if "cancelada" in marker.lower() or "pendente de publicação" in text:
        return None
    sid = f"{prefix}:sv:{number}"
    sumula = _sumula(
        sid, number, f"Súmula Vinculante {number}", f"Súmula Vinculante {number} do STF", text, url
    )
    published = next((m.group(0) for b in bodies[1:] if (m := _PUBLISHED.search(b))), None)
    if published:
        sumula.provisions[0].notes.append(published)
    return sumula


def parse_stf_sumulas(pages: Iterable[tuple[str, str]], prefix: str = "stf") -> list[Article]:
    """Súmulas vinculantes of `(html, url)` pages, in number order, without the cancelled."""
    parsed = (parse_stf_sumula(html, url, prefix) for html, url in pages)
    return sorted((s for s in parsed if s is not None), key=lambda s: int(s.number))
