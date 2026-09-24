import re
from pathlib import Path

import pytest
from vcr.request import Request

from tests.contract.recording import CASSETTES, missing_cassette, record_provider_hosts_only

SECRET = re.compile(r"sk-ant|authorization|x-api-key|ya29\.|AKIA|ASIA|eyJ|AIza", re.IGNORECASE)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.anthropic.com/v1/messages",
        "https://bedrock-mantle.eu-west-1.api.aws/anthropic/v1/messages",
        "https://bedrock-runtime.eu-west-1.amazonaws.com/model/x/invoke",
        "https://aiplatform.googleapis.com/v1/projects/p",
        "https://us-east5-aiplatform.googleapis.com/v1/projects/p",
        "https://aiplatform.eu.rep.googleapis.com/v1/projects/p",
        "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
        "https://api.openai.com/v1/chat/completions",
        "https://specster-dev.openai.azure.com/openai/deployments/d/chat/completions",
    ],
)
def test_provider_api_requests_are_recorded(url: str) -> None:
    request = Request("POST", url, b"{}", {})
    assert record_provider_hosts_only(request) is request


@pytest.mark.parametrize(
    "url",
    [
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/x:generateAccessToken",
        "https://oauth2.googleapis.com/token",
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts",
        "http://169.254.169.254/latest/api/token",
        "https://sts.amazonaws.com/",
        "https://sts.eu-west-1.amazonaws.com/",
        "https://login.microsoftonline.com/t/oauth2/v2.0/token",
        "https://s3.eu-west-1.amazonaws.com/bucket",
        "https://api.anthropic.com.attacker.example/v1/messages",
        "https://openai.azure.com.attacker.example/",
    ],
)
def test_everything_else_is_never_recorded(url: str) -> None:
    assert record_provider_hosts_only(Request("POST", url, b"{}", {})) is None


def test_committed_cassettes_hold_no_secrets() -> None:
    leaks = [
        p.name
        for p in sorted(Path(CASSETTES).glob("*.yaml"))
        if SECRET.search(p.read_text(encoding="utf-8"))
    ]
    assert leaks == [], f"cassettes contain credentials: {leaks}"


def test_a_missing_cassette_skips_unless_cassettes_are_required() -> None:
    with pytest.raises(pytest.skip.Exception, match="no cassette"):
        missing_cassette("no cassette", {})
    with pytest.raises(pytest.skip.Exception):
        missing_cassette("no cassette", {"SPECSTER_REQUIRE_CASSETTES": "0"})
    with pytest.raises(pytest.fail.Exception, match="no cassette"):
        missing_cassette("no cassette", {"SPECSTER_REQUIRE_CASSETTES": "1"})
