import re

_REPO = r"[\w.-]+/[\w.-]+"
_CLOSING = re.compile(
    r"\b(?P<keyword>close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(?P<sep>\s*:?\s*)"
    rf"(?:(?:https?://[^\s/]+/|(?:www\.)?github\.com/)(?P<url_repo>{_REPO})/issues/(?P<url_n>\d+)"
    rf"|(?P<repo>{_REPO})#(?P<repo_n>\d+)"
    r"|#(?P<n>\d+)"
    r"|gh-(?P<gh_n>\d+))\b",
    re.IGNORECASE,
)
_HASH = re.compile(r"\s*#(\d+)")


def _plain(m: re.Match[str]) -> str:
    repo = m["url_repo"] or m["repo"]
    number = m["url_n"] or m["repo_n"] or m["n"] or m["gh_n"]
    ref = f"{repo} issue {number}" if repo else f"issue {number}"
    return f"{m['keyword']}{m['sep']}{ref}"


def closes_an_issue(text: str) -> bool:
    return _CLOSING.search(text) is not None


def rewrite_references(text: str) -> str:
    return _CLOSING.sub(_plain, text)


def drop_references(text: str) -> str:
    return _HASH.sub(r" issue \1", rewrite_references(text))
