"""The report the lawyer approves, in Markdown, and the corpus texts it quotes as its basis."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache

from ementa.agents.contract_review.schemas import ClauseOut, Redline, RiskRow
from ementa.domain.contracts import Severity
from ementa.tools.research import RemovedOut

CORPUS_SOURCES = ("cdc", "stj")  # what the playbook may cite
_SHORT = {"cdc": "CDC", "lgpd": "LGPD", "cpc": "CPC"}
_SOURCE = {"rule": "regra", "model": "modelo"}


@dataclass(frozen=True)
class SourceText:
    id: str
    label: str  # "CDC, art. 51, IV" or "Súmula 302 do STJ"
    text: str  # literal; an inciso or an alínea comes after the lead-in it completes


def source_label(source_id: str, title: str = "") -> str:
    """The label of `cdc:art52:p1` is "CDC, art. 52, § 1º"; a súmula goes by its `title`."""
    prefix, _, rest = source_id.partition(":")
    if prefix not in _SHORT:
        return title or source_id
    article, *parts = rest.split(":")
    number = article.removeprefix("art")
    ordinal = "º" if number.isdigit() and int(number) < 10 else ""
    labels = [f"{_SHORT[prefix]}, art. {number}{ordinal}"]
    for part in parts:
        if part == "pu":
            labels.append("parágrafo único")
        elif part[:1] == "p" and part[1:].isdigit():
            n = int(part[1:])
            labels.append(f"§ {n}{'º' if n < 10 else ''}")
        else:
            labels.append(part)
    return ", ".join(labels)


@cache
def corpus_texts(sources: tuple[str, ...] = CORPUS_SOURCES) -> dict[str, SourceText]:
    """Every citable provision of `sources` in the corpus snapshot, by source id; vetoed and
    revoked ones are left out, as there is nothing in them to cite. Parsed once per process."""
    from ementa.ingest.corpus import load_statute

    texts: dict[str, SourceText] = {}
    for source in sources:
        for article in load_statute(source).articles:
            by_id = {p.id: p for p in article.provisions}
            for provision in article.provisions:
                if not provision.text or provision.vetoed:
                    continue
                parent = by_id.get(provision.id.rpartition(":")[0])
                text = provision.text
                if provision.kind in ("inciso", "alinea") and parent is not None and parent.text:
                    text = f"{parent.text} {provision.label} {provision.text}"
                label = source_label(provision.id, article.title)
                texts[provision.id] = SourceText(provision.id, label, text)
    return texts


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.split("\n"))


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def worst_by_clause(findings: Iterable[tuple[str, Severity]]) -> dict[str, Severity]:
    """The most serious finding of each clause, from (clause, severity) pairs."""
    worst: dict[str, Severity] = {}
    for clause, severity in findings:
        current = worst.get(clause)
        if current is None or severity.rank < current.rank:
            worst[clause] = severity
    return worst


def severity_counts(worst: Mapping[str, Severity]) -> str:
    """For example "gravidade: 3 alta, 1 média", each clause by its most serious finding."""
    parts = [
        f"{n} {severity.label}"
        for severity in Severity
        if (n := sum(s is severity for s in worst.values()))
    ]
    return f"gravidade: {', '.join(parts)}"


def render_report(
    *,
    title: str,
    filename: str,
    playbook: str,
    prompt: str | None,
    model: str | None,
    criteria: Sequence[str],
    clauses: Sequence[ClauseOut],
    matrix: Sequence[RiskRow],
    redlines: Sequence[Redline],
    removed: Sequence[RemovedOut],
    unreviewed: Sequence[str],
    texts: Mapping[str, SourceText],
    notice: str,
) -> str:
    """The draft as the lawyer reads it: summary, warnings, risk matrix, redlines and the
    literal text of every provision cited. Without `prompt`, only the rules ran."""
    lines = [f"# Revisão de contrato: {title}", "", notice, ""]
    worst = worst_by_clause((row.clause, row.severity) for row in matrix)
    flagged = f"{len(worst)} com risco ({severity_counts(worst)})" if worst else "nenhuma com risco"
    analyzed = _plural(len(clauses), "analisada", "analisadas")
    lines += [
        f"- Arquivo: `{filename}`",
        f"- Playbook: `{playbook}`",
        f"- Cláusulas: {analyzed}, {flagged}",
    ]
    if prompt and model:
        lines.append(f"- Critérios do modelo: `{prompt}` no `{model}`, com o texto pseudonimizado")
    lines.append("")

    warnings: list[str] = []
    if not prompt and criteria:
        warnings.append(
            "Só as regras do playbook rodaram. Os critérios que pedem o modelo não foram "
            f"avaliados: {'; '.join(criteria)}."
        )
    if unreviewed:
        which = "a cláusula" if len(unreviewed) == 1 else "as cláusulas"
        where = "nela" if len(unreviewed) == 1 else "nelas"
        warnings.append(
            f"O modelo não avaliou {which} {', '.join(unreviewed)}: {where} só as regras do "
            "playbook foram aplicadas."
        )
    if removed:
        warnings.append(
            f"O verificador tirou {_plural(len(removed), 'item', 'itens')} sem fonte conferida"
            " no corpus; a lista está no fim."
        )
    if warnings:
        lines += ["## Atenção", "", *(f"- {w}" for w in warnings), ""]

    lines += ["## Matriz de risco", ""]
    if matrix:
        lines += [
            "| Cláusula | Problema | Gravidade | Fundamento | Origem |",
            "|---|---|---|---|---|",
        ]
        for row in matrix:
            clause = f"{row.clause} \u2013 {row.clause_title}" if row.clause_title else row.clause
            basis = "; ".join(f"{texts[i].label if i in texts else i} [{i}]" for i in row.basis)
            lines.append(
                f"| {_cell(clause)} | {_cell(row.rule_title)} | {row.severity.label} "
                f"| {_cell(basis)} | {_SOURCE[row.source]} |"
            )
    else:
        lines.append("Nenhuma cláusula violou o playbook.")
    lines.append("")

    if redlines:
        lines += ["## Redlines", ""]
        rows_by_clause: dict[str, list[RiskRow]] = {}
        for row in matrix:
            rows_by_clause.setdefault(row.clause, []).append(row)
        for redline in redlines:
            name = f"Cláusula {redline.clause}"
            if redline.clause_title:
                name = f"{name} \u2013 {redline.clause_title}"
            lines.append(f"### {name}")
            lines.append("")
            for row in rows_by_clause.get(redline.clause, []):
                lines.append(f"- **{row.rule_title}** ({row.severity.label}). {row.why}")
                if row.note:
                    lines.append(f"  Motivo apontado pelo modelo: {row.note}")
            lines.append("")
            for change in redline.changes:
                if change.before and change.after:
                    lines += ["Trecho atual:", "", _quote(change.before), ""]
                    lines += ["Redação sugerida:", "", _quote(change.after), ""]
                elif change.before:
                    lines += ["Trecho a suprimir:", "", _quote(change.before), ""]
                else:
                    lines += ["Texto a acrescentar:", "", _quote(change.after), ""]

    cited = list(dict.fromkeys(i for row in matrix for i in row.basis))
    if cited:
        lines += ["## Fundamentos", ""]
        for source_id in cited:
            text = texts.get(source_id)
            if text is not None:
                lines.append(f"- [{source_id}] {text.label}: {text.text}")
        lines.append("")
    if removed:
        lines += ["## Removido pelo verificador", ""]
        lines += [f"- `{r.reference}`: {r.reason}" for r in removed]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
