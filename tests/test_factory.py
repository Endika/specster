import pytest

from specster.config import ModelConfig
from specster.llm.factory import ProviderConfigError, build_chat_model
from specster.llm.openai_chat import OpenAIChat


@pytest.mark.parametrize(
    ("cfg", "missing"),
    [
        (ModelConfig(provider="anthropic"), "ANTHROPIC_API_KEY"),
        (ModelConfig(provider="anthropic", api_key_env="MY_KEY"), "MY_KEY"),
        (ModelConfig(provider="bedrock", model="anthropic.claude-haiku-4-5"), "region"),
        (ModelConfig(provider="vertex-anthropic", project="p"), "region"),
        (ModelConfig(provider="vertex-anthropic", region="global"), "project"),
        (ModelConfig(provider="openai", model="gpt-5-mini"), "OPENAI_API_KEY"),
        (ModelConfig(provider="openai-compatible", model="m"), "base_url"),
        (
            ModelConfig(
                provider="openai-compatible", base_url="http://h/v1", api_key_env="COMPAT_KEY"
            ),
            "COMPAT_KEY",
        ),
        (ModelConfig(provider="azure-openai", api_version="2024-10-21"), "base_url"),
        (
            ModelConfig(provider="azure-openai", base_url="https://a.openai.azure.com"),
            "api_version",
        ),
        (ModelConfig(provider="gemini", model="gemini-flash-latest"), "GEMINI_API_KEY"),
        (ModelConfig(provider="vertex-gemini", region="global"), "project"),
        (ModelConfig(provider="vertex-gemini", project="p"), "region"),
    ],
)
def test_missing_required_settings_name_the_key(cfg: ModelConfig, missing: str) -> None:
    with pytest.raises(ProviderConfigError, match=missing):
        build_chat_model(cfg, {})


def test_replay_needs_no_credentials() -> None:
    for cfg in (
        ModelConfig(provider="anthropic"),
        ModelConfig(provider="bedrock", model="anthropic.claude-haiku-4-5", region="eu-west-1"),
        ModelConfig(provider="vertex-anthropic", region="global", project="p"),
        ModelConfig(provider="openai", model="gpt-5-mini"),
        ModelConfig(provider="openai-compatible", model="m", base_url="http://h/v1"),
        ModelConfig(
            provider="azure-openai",
            model="d",
            base_url="https://a.openai.azure.com",
            api_version="2024-10-21",
        ),
        ModelConfig(provider="gemini", model="gemini-flash-latest"),
        ModelConfig(provider="vertex-gemini", model="g", region="global", project="p"),
    ):
        model = build_chat_model(cfg, {}, replay=True)
        assert (model.provider, model.model) == (cfg.provider, cfg.model)


def test_openai_compatible_needs_no_key() -> None:
    cfg = ModelConfig(provider="openai-compatible", model="m", base_url="http://h/v1")
    assert build_chat_model(cfg, {}).provider == "openai-compatible"


@pytest.mark.parametrize(
    ("cfg", "param"),
    [
        (ModelConfig(provider="openai", model="m"), "max_completion_tokens"),
        (ModelConfig(provider="openai-compatible", base_url="http://h/v1"), "max_tokens"),
        (
            ModelConfig(provider="openai", model="m", token_param="max_tokens"),
            "max_tokens",
        ),
    ],
)
def test_openai_token_parameter_defaults_per_provider(cfg: ModelConfig, param: str) -> None:
    model = build_chat_model(cfg, {}, replay=True)
    assert isinstance(model, OpenAIChat)
    assert model.token_param == param
