import pytest

from ementa_kit.config import Environment, Settings


def test_defaults_point_to_local_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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
