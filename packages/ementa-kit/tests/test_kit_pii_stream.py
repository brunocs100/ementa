"""Rehydration of a streamed reply. The CPF, name and phone here are fictitious."""

from hypothesis import given
from hypothesis import strategies as st

from ementa_kit.guardrails.pii import MarkerStream, rehydrate
from ementa_kit.guardrails.pii.stream import MAX_MARKER

MAPPING = {
    "[CPF_1]": "123.456.789-09",
    "[PESSOA_1]": "Maria Souza",
    "[TELEFONE_12]": "(51) 99999-8888",
}


def test_a_marker_split_across_pieces_is_shown_whole() -> None:
    stream = MarkerStream(MAPPING)

    shown = [stream.feed("Seu CPF é [CP"), stream.feed("F_"), stream.feed("1]."), stream.flush()]

    assert shown == ["Seu CPF é ", "", "123.456.789-09.", ""]


def test_a_bare_marker_waits_for_the_next_character() -> None:
    stream = MarkerStream(MAPPING)

    assert stream.feed("Fale com PESSOA") == "Fale com "
    assert stream.feed("_1") == ""  # it could still become PESSOA_10
    assert stream.feed(" hoje.") == "Maria Souza hoje."


def test_a_source_id_goes_out_once_its_bracket_closes() -> None:
    stream = MarkerStream(MAPPING)

    assert stream.feed("Pode desistir [cdc:ar") == "Pode desistir "
    assert stream.feed("t49] em 7 dias") == "[cdc:art49] em 7 "
    assert stream.flush() == "dias"  # the last word waits for what comes after it


def test_markdown_escaped_markers_keep_their_backslash_with_them() -> None:
    stream = MarkerStream(MAPPING)

    pieces = ["Ligue no \\", "[TELEFONE\\", "_12\\", "]"]
    shown = "".join(stream.feed(p) for p in pieces) + stream.flush()

    assert shown == "Ligue no (51) 99999-8888"


def test_a_bracket_left_open_is_released_after_the_longest_marker() -> None:
    stream = MarkerStream(MAPPING)
    note = "[nota " + "longa " * (MAX_MARKER // 6)

    assert len(note) > MAX_MARKER
    assert stream.feed(note) == note


def test_a_marker_glued_to_a_word_stays_a_marker_name() -> None:
    """`rehydrate` leaves "éCPF_1" alone; a cut between "é" and "CPF_1" must not change that."""
    stream = MarkerStream(MAPPING)

    shown = stream.feed("Código é") + stream.feed("CPF_1 ") + stream.flush()

    assert shown == "Código éCPF_1 " == rehydrate("Código éCPF_1 ", MAPPING)


def test_without_markers_in_the_thread_nothing_is_held_back() -> None:
    stream = MarkerStream({})

    assert stream.feed("Seu CPF é [CP") == "Seu CPF é [CP"


def test_flush_releases_an_unfinished_marker_as_it_is() -> None:
    stream = MarkerStream(MAPPING)

    assert stream.feed("termina em [CPF_") == "termina em "
    assert stream.flush() == "[CPF_"


PIECES = st.sampled_from(
    [
        "[CPF_1]",
        "[ CPF_1 ]",
        "[cpf_1]",
        "\\[CPF\\_1\\]",
        "[CPF 1]",
        "CPF_1",
        "CPF\\_1",
        "CPF_12",
        "[CPF_9]",
        "[PESSOA_1]",
        "Pessoa_1",
        "[TELEFONE_12]",
        "[cdc:art49]",
        "CPF",
        "CP",
        "texto",
        "é",
        "1",
        "_",
        "[",
        "]",
        "\\",
        " ",
        "\n",
        ".",
    ]
)


@given(
    st.lists(PIECES, max_size=30).map("".join),
    st.lists(st.integers(min_value=0, max_value=400), max_size=12),
)
def test_the_pieces_shown_join_into_the_rehydrated_reply(text: str, cuts: list[int]) -> None:
    """However the reply is split, the user ends up with what `rehydrate` gives for the whole.

    At most 30 pieces keep every marker, padding included, under `MAX_MARKER` characters."""
    bounds = sorted({min(cut, len(text)) for cut in cuts})
    pieces = [text[a:b] for a, b in zip([0, *bounds], [*bounds, len(text)], strict=True)]
    stream = MarkerStream(MAPPING)

    shown = "".join(stream.feed(piece) for piece in pieces) + stream.flush()

    assert shown == rehydrate(text, MAPPING)
