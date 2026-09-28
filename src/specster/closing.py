import re

_REPO = r"[\w.-]+/[\w.-]+"


def _url(name: str) -> str:
    host = r"(?:https?://[^\s/]+/|(?:www\.)?github\.com/)"
    return rf"{host}(?P<{name}_repo>{_REPO})/issues/(?P<{name}_n>\d+)"


# GitHub reads the URL of a Markdown link or an autolink right after the keyword, whatever the
# link text says.
_CLOSING = re.compile(
    r"\b(?P<keyword>close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(?P<sep>\s*:?\s*)"
    rf"(?:\[(?P<text>[^\]\n]*)\]\({_url('link')}\)"
    rf"|<{_url('auto')}>"
    rf"|{_url('url')}\b"
    rf"|(?P<repo>{_REPO})#(?P<repo_n>\d+)\b"
    r"|#(?P<n>\d+)\b"
    r"|gh-(?P<gh_n>\d+)\b)",
    re.IGNORECASE,
)
_HASH = re.compile(r"\s*#(\d+)")


def _plain(m: re.Match[str]) -> str:
    repo = m["link_repo"] or m["auto_repo"] or m["url_repo"] or m["repo"]
    number = m["link_n"] or m["auto_n"] or m["url_n"] or m["repo_n"] or m["n"] or m["gh_n"]
    ref = f"{repo} issue {number}" if repo else f"issue {number}"
    if m["link_repo"]:
        text = _HASH.sub(r" issue \1", " " + m["text"]).strip()
        ref = f"{text} ({ref})"
    return f"{m['keyword']}{m['sep']}{ref}"


def closes_an_issue(text: str) -> bool:
    return _CLOSING.search(text) is not None


def rewrite_references(text: str) -> str:
    return _CLOSING.sub(_plain, text)


def drop_references(text: str) -> str:
    return _HASH.sub(r" issue \1", rewrite_references(text))
