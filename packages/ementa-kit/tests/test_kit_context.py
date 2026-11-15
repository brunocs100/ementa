from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ementa_kit.config import Settings
from ementa_kit.context import (
    SIZES_EVENT,
    Conversation,
    Section,
    approx_tokens,
    build_context,
    build_user_turn,
)
from ementa_kit.llm import ModelRegistry, ModelRole, ModelUnavailableError
from ementa_kit.prompts import PromptRegistry, load_prompt


def test_user_turn_puts_sections_first_and_question_last() -> None:
    turn = build_user_turn([Section("fontes", "[x:art1] texto")], "E agora?")

    assert turn.text == "<fontes>\n[x:art1] texto\n</fontes>\n\n<pergunta>\nE agora?\n</pergunta>"


def test_build_context_records_the_size_of_each_part_on_the_current_span() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sections = [Section("fontes", "[x:art1] texto")]

    with provider.get_tracer("test").start_as_current_span("invoke_agent"):
        messages = build_context("regras", sections, "E agora?")

    assert messages == [SystemMessage("regras"), build_user_turn(sections, "E agora?")]
    [span] = exporter.get_finished_spans()
    [event] = span.events
    assert event.name == SIZES_EVENT == "ementa.context.section_tokens"
    assert dict(event.attributes or {}) == {
        "system.chars": 6,
        "system.tokens": 2,
        "fontes.chars": 33,  # "<fontes>\n[x:art1] texto\n</fontes>", what the model sees
        "fontes.tokens": 9,
        "pergunta.chars": 31,
        "pergunta.tokens": 8,
    }
    assert [approx_tokens(t) for t in ("", "abcd", "abcde")] == [0, 1, 2]


def test_conversation_only_grows() -> None:
    conversation = Conversation("sistema")
    conversation.append(HumanMessage("oi"))
    before = conversation.messages
    conversation.append(AIMessage("olá"))

    assert conversation.messages[: len(before)] == before
    assert conversation.sizes() == [7, 2, 3]
    with pytest.raises(ValueError, match="frozen"):
        conversation.append(SystemMessage("outro sistema"))


def test_load_prompt_reads_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "p.v2.md"
    path.write_text("---\nid: a.b\nversion: 2\nchangelog: v2 test\n---\nOlá, $nome.\n", "utf-8")

    prompt = load_prompt(path)

    assert (prompt.id, prompt.version) == ("a.b", 2)
    assert prompt.render(nome="Ana") == "Olá, Ana.\n"


@pytest.mark.parametrize(
    ("content", "error"),
    [("sem front matter", "missing front matter"), ("---\nid: a\n---\ntexto", "lacks")],
)
def test_load_prompt_rejects_bad_files(tmp_path: Path, content: str, error: str) -> None:
    path = tmp_path / "p.md"
    path.write_text(content, "utf-8")

    with pytest.raises(ValueError, match=error):
        load_prompt(path)


def write_prompt(root: Path, relative: str, prompt_id: str, version: int, changelog: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    log = f"changelog: {changelog}\n" if changelog != "-" else ""
    path.write_text(f"---\nid: {prompt_id}\nversion: {version}\n{log}---\nTexto.\n", "utf-8")


def test_prompt_registry_lists_every_prompt_by_id_and_version(tmp_path: Path) -> None:
    write_prompt(tmp_path, "b/x.v1.md", "b.x", 1, "v1 primeira")
    write_prompt(tmp_path, "a/y.v2.md", "a.y", 2, "v2 cita a LGPD; v1 primeira")
    write_prompt(tmp_path, "a/y.v1.md", "a.y", 1, "v1 primeira")

    registry = PromptRegistry.from_dir(tmp_path)

    assert [(p.id, p.version, p.changelog) for p in registry.prompts()] == [
        ("a.y", 1, "v1 primeira"),
        ("a.y", 2, "v2 cita a LGPD; v1 primeira"),
        ("b.x", 1, "v1 primeira"),
    ]
    assert registry.get("a.y", 2).text == "Texto.\n"
    with pytest.raises(LookupError, match=r"unknown prompt a\.y v3"):
        registry.get("a.y", 3)


@pytest.mark.parametrize(
    ("files", "error"),
    [
        (
            [("a/x.v1.md", "a.x", 1, "v1 um"), ("a/copy.md", "a.x", 1, "v1 cópia")],
            "a/x.v1.md: a.x v1 is already in a/copy.md",
        ),
        ([("a/x.v2.md", "a.x", 2, "v1 primeira versão")], "a/x.v2.md: .* no entry for v2"),
        ([("a/x.v1.md", "a.x", 1, "")], "no entry for v1"),
        ([("a/x.v1.md", "a.x", 1, "v10 outra")], "no entry for v1"),
        ([("a/x.v1.md", "a.x", 1, "-")], r"lacks \['changelog'\]"),
    ],
)
def test_prompt_registry_fails_fast_on_a_duplicate_or_a_missing_changelog(
    tmp_path: Path, files: list[tuple[str, str, int, str]], error: str
) -> None:
    for relative, prompt_id, version, changelog in files:
        write_prompt(tmp_path, relative, prompt_id, version, changelog)

    with pytest.raises(ValueError, match=error):
        PromptRegistry.from_dir(tmp_path)


def _settings(**values: str) -> Settings:
    return Settings(_env_file=None, openai_api_key=None, **values)  # type: ignore[call-arg,arg-type]


def test_registry_maps_roles_to_models_and_caches() -> None:
    built: list[str] = []

    def factory(spec):  # type: ignore[no-untyped-def]
        built.append(spec.model)
        return object()

    registry = ModelRegistry(_settings(model_fast="tiny"), factory=factory)

    assert registry.spec(ModelRole.FAST).model == "tiny"
    assert registry.spec(ModelRole.REASONING).reasoning_effort == "high"
    assert registry.chat(ModelRole.FAST) is registry.chat(ModelRole.FAST)
    assert built == ["tiny"]


def test_registry_without_key_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EMENTA_OPENAI_API_KEY", raising=False)

    with pytest.raises(ModelUnavailableError, match="OPENAI_API_KEY"):
        ModelRegistry(_settings()).chat(ModelRole.DEFAULT)
