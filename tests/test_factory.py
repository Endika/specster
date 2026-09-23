import pytest

from specster.config import ModelConfig
from specster.llm.factory import ProviderConfigError, build_chat_model


@pytest.mark.parametrize(
    ("cfg", "missing"),
    [
        (ModelConfig(provider="anthropic"), "ANTHROPIC_API_KEY"),
        (ModelConfig(provider="anthropic", api_key_env="MY_KEY"), "MY_KEY"),
        (ModelConfig(provider="bedrock", model="anthropic.claude-haiku-4-5"), "region"),
        (ModelConfig(provider="vertex-anthropic", project="p"), "region"),
        (ModelConfig(provider="vertex-anthropic", region="global"), "project"),
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
    ):
        model = build_chat_model(cfg, {}, replay=True)
        assert (model.provider, model.model) == (cfg.provider, cfg.model)
