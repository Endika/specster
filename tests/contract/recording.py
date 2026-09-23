import re
from pathlib import Path
from typing import Any

CASSETTES = Path(__file__).parent / "cassettes"

# An allowlist, not a denylist: token endpoints (iamcredentials, STS, metadata servers) answer
# with live credentials, and this repository is public.
_PROVIDER_HOSTS = re.compile(
    r"api\.anthropic\.com"
    r"|bedrock-(mantle|runtime)\.[a-z0-9-]+\.(api\.aws|amazonaws\.com)"
    r"|([a-z0-9-]+-)?aiplatform(\.(us|eu)\.rep)?\.googleapis\.com"
    r"|generativelanguage\.googleapis\.com"
    r"|api\.openai\.com"
    r"|[a-z0-9-]+\.openai\.azure\.com"
)


def record_provider_hosts_only(request: Any) -> Any:
    host = (request.host or "").lower()
    return request if _PROVIDER_HOSTS.fullmatch(host) else None


def drop_response_headers(response: dict[str, Any]) -> dict[str, Any]:
    response["headers"] = {
        k: v for k, v in response["headers"].items() if k.lower() == "content-type"
    }
    return response
