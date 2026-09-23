from pathlib import Path
from typing import Any

import pytest

CASSETTES = Path(__file__).parent / "cassettes"


def _drop_response_headers(response: dict[str, Any]) -> dict[str, Any]:
    response["headers"] = {
        k: v for k, v in response["headers"].items() if k.lower() == "content-type"
    }
    return response


def _skip_auth_endpoints(request: Any) -> Any:
    host = request.host or ""
    if any(h in host for h in ("oauth2.googleapis.com", "sts.", "login.microsoftonline.com")):
        return None
    return request


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, Any]:
    return {
        "filter_headers": [
            "authorization",
            "x-api-key",
            "api-key",
            "x-goog-api-key",
            "x-amz-security-token",
            "x-amz-date",
            "x-amz-content-sha256",
            "cookie",
            "anthropic-organization-id",
        ],
        "filter_query_parameters": ["key"],
        "before_record_response": _drop_response_headers,
        "before_record_request": _skip_auth_endpoints,
        "match_on": ["method", "scheme", "host", "path"],
    }


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    return str(CASSETTES)
