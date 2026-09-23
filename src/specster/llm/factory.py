from collections.abc import Mapping

import anthropic

from specster.config import ModelConfig
from specster.llm.anthropic_chat import AnthropicChat
from specster.llm.base import ChatModel

REPLAY_KEY = "replay-placeholder"


class ProviderConfigError(Exception):
    pass


def _need(value: str | None, what: str, provider: str) -> str:
    if not value:
        raise ProviderConfigError(f"models.*.{what} is required for provider {provider}")
    return value


def _key(env: Mapping[str, str], name: str, provider: str, replay: bool) -> str:
    if replay:
        return REPLAY_KEY
    value = env.get(name)
    if not value:
        raise ProviderConfigError(f"environment variable {name} is required for {provider}")
    return value


def build_chat_model(cfg: ModelConfig, env: Mapping[str, str], replay: bool = False) -> ChatModel:
    p = cfg.provider
    client: anthropic.Anthropic | anthropic.AnthropicBedrockMantle | anthropic.AnthropicVertex
    if p == "anthropic":
        key = _key(env, cfg.api_key_env or "ANTHROPIC_API_KEY", p, replay)
        client = anthropic.Anthropic(
            api_key=key, base_url=cfg.base_url, max_retries=cfg.max_retries
        )
    elif p == "bedrock":
        region = _need(cfg.region, "region", p)
        # Replay must not reach for AWS credentials or botocore's SigV4 signer.
        client = anthropic.AnthropicBedrockMantle(
            aws_region=region, max_retries=cfg.max_retries, skip_auth=replay
        )
    elif p == "vertex-anthropic":
        region, project = _need(cfg.region, "region", p), _need(cfg.project, "project", p)
        client = anthropic.AnthropicVertex(
            project_id=project,
            region=region,
            max_retries=cfg.max_retries,
            access_token="replay" if replay else None,
        )
    else:
        raise ProviderConfigError(f"provider {p} is not wired yet")
    return AnthropicChat(client, p, cfg.model, cfg.max_tokens, cfg.effort)
