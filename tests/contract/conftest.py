from typing import Any

import pytest

from tests.contract.recording import CASSETTES, drop_response_headers, record_provider_hosts_only


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
        "before_record_response": drop_response_headers,
        "before_record_request": record_provider_hosts_only,
        "match_on": ["method", "scheme", "host", "path"],
    }


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    return str(CASSETTES)
