from collections.abc import Mapping

import anthropic
import openai

from specster.config import ModelConfig
from specster.llm.anthropic_chat import AnthropicChat
from specster.llm.base import ChatModel
from specster.llm.gemini_chat import GeminiChat
from specster.llm.openai_chat import OpenAIChat

REPLAY_KEY = "replay-placeholder"
_NO_BASE_URL = frozenset({"bedrock", "vertex-anthropic", "gemini", "vertex-gemini"})


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


def _anthropic(cfg: ModelConfig, env: Mapping[str, str], replay: bool) -> ChatModel:
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
    else:
        region, project = _need(cfg.region, "region", p), _need(cfg.project, "project", p)
        client = anthropic.AnthropicVertex(
            project_id=project,
            region=region,
            max_retries=cfg.max_retries,
            access_token="replay" if replay else None,
        )
    return AnthropicChat(client, p, cfg.model, cfg.max_tokens, cfg.effort)


def _openai(cfg: ModelConfig, env: Mapping[str, str], replay: bool) -> ChatModel:
    p = cfg.provider
    default_param = "max_tokens" if p == "openai-compatible" else "max_completion_tokens"
    param = cfg.token_param or default_param
    client: openai.OpenAI
    if p == "azure-openai":
        endpoint = _need(cfg.base_url, "base_url", p)
        version = _need(cfg.api_version, "api_version", p)
        if cfg.api_key_env:
            key: str | None = _key(env, cfg.api_key_env, p, replay)
        else:
            key = REPLAY_KEY if replay else env.get("AZURE_OPENAI_API_KEY")
        if key:
            client = openai.AzureOpenAI(
                azure_endpoint=endpoint,
                api_version=version,
                api_key=key,
                max_retries=cfg.max_retries,
            )
        else:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            # Without a key, the OIDC login from azure/login in the workflow supplies the token.
            token = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
            client = openai.AzureOpenAI(
                azure_endpoint=endpoint,
                api_version=version,
                azure_ad_token_provider=token,
                max_retries=cfg.max_retries,
            )
    elif p == "openai":
        key = _key(env, cfg.api_key_env or "OPENAI_API_KEY", p, replay)
        client = openai.OpenAI(api_key=key, base_url=cfg.base_url, max_retries=cfg.max_retries)
    else:
        base_url = _need(cfg.base_url, "base_url", p)
        # Local servers take no key; the SDK still refuses to start without one.
        key = _key(env, cfg.api_key_env, p, replay) if cfg.api_key_env else "not-needed"
        client = openai.OpenAI(api_key=key, base_url=base_url, max_retries=cfg.max_retries)
    return OpenAIChat(client, p, cfg.model, cfg.max_tokens, param)


def _gemini(cfg: ModelConfig, env: Mapping[str, str], replay: bool) -> ChatModel:
    from google import genai

    p = cfg.provider
    if p == "gemini":
        key = _key(env, cfg.api_key_env or "GEMINI_API_KEY", p, replay)
        client = genai.Client(api_key=key)
    else:
        project, region = _need(cfg.project, "project", p), _need(cfg.region, "region", p)
        if replay:
            from google.oauth2.credentials import Credentials

            client = genai.Client(
                vertexai=True,
                project=project,
                location=region,
                # google-auth leaves Credentials.__init__ unannotated.
                credentials=Credentials(token="replay"),  # type: ignore[no-untyped-call]
            )
        else:
            client = genai.Client(vertexai=True, project=project, location=region)
    return GeminiChat(client, p, cfg.model, cfg.max_tokens, cfg.max_retries)


def build_chat_model(cfg: ModelConfig, env: Mapping[str, str], replay: bool = False) -> ChatModel:
    p = cfg.provider
    if cfg.base_url and p in _NO_BASE_URL:
        raise ProviderConfigError(f"models.*.base_url is not supported for provider {p}")
    if p in ("anthropic", "bedrock", "vertex-anthropic"):
        return _anthropic(cfg, env, replay)
    if p in ("openai", "openai-compatible", "azure-openai"):
        return _openai(cfg, env, replay)
    if p in ("gemini", "vertex-gemini"):
        return _gemini(cfg, env, replay)
    raise ProviderConfigError(f"provider {p} is not wired yet")
