from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ementa_kit.config import Settings
from ementa_kit.context import Conversation, Section, build_user_turn
from ementa_kit.llm import ModelRegistry, ModelRole, ModelUnavailableError
from ementa_kit.prompts import load_prompt


def test_user_turn_puts_sections_first_and_question_last() -> None:
    turn = build_user_turn([Section("fontes", "[x:art1] texto")], "E agora?")

    assert turn.text == "<fontes>\n[x:art1] texto\n</fontes>\n\n<pergunta>\nE agora?\n</pergunta>"


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
