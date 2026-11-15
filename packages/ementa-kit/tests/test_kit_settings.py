import pytest

from ementa_kit.config import Environment, Settings


def test_defaults_point_to_local_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EMENTA_ENV", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.env is Environment.DEV
    assert settings.otel_endpoint == "http://localhost:4318"
    assert settings.openai_api_key is None


def test_reads_prefixed_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMENTA_ENV", "ci")
    monkeypatch.setenv("EMENTA_LOG_LEVEL", "DEBUG")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.env is Environment.CI
    assert settings.log_level == "DEBUG"
    assert not settings.is_prod


def test_openai_key_accepts_the_provider_name_and_stays_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "sk-test-123"
    assert "sk-test-123" not in repr(settings)


def test_rejects_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMENTA_ENV", "staging")

    with pytest.raises(ValueError, match="env"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_no_skill_script_runs_by_default_and_the_allowlist_is_a_json_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMENTA_SKILL_SCRIPTS_ALLOWED", raising=False)
    assert Settings(_env_file=None).skill_scripts_allowed == []  # type: ignore[call-arg]

    monkeypatch.setenv("EMENTA_SKILL_SCRIPTS_ALLOWED", '["calc/show.py"]')
    monkeypatch.setenv("EMENTA_SKILL_SCRIPT_TIMEOUT_S", "2.5")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.skill_scripts_allowed == ["calc/show.py"]
    assert settings.skill_script_timeout_s == 2.5


def test_rerank_is_on_by_default_and_switchable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ENABLED", "MODEL", "CANDIDATES", "TOP_K"):
        monkeypatch.delenv(f"EMENTA_RERANK_{name}", raising=False)
    default = Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setenv("EMENTA_RERANK_ENABLED", "false")
    monkeypatch.setenv("EMENTA_RERANK_MODEL", "org/other-reranker")
    monkeypatch.setenv("EMENTA_RERANK_CANDIDATES", "20")
    monkeypatch.setenv("EMENTA_RERANK_TOP_K", "6")
    changed = Settings(_env_file=None)  # type: ignore[call-arg]

    assert default.rerank_enabled is True
    assert default.rerank_model == "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    assert (default.rerank_candidates, default.rerank_top_k) == (30, 8)
    assert changed.rerank_enabled is False
    assert changed.rerank_model == "org/other-reranker"
    assert (changed.rerank_candidates, changed.rerank_top_k) == (20, 6)


def test_rerank_counts_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMENTA_RERANK_TOP_K", "0")

    with pytest.raises(ValueError, match="rerank_top_k"):
        Settings(_env_file=None)  # type: ignore[call-arg]
