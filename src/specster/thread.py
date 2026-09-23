import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape

from specster.config import TrustConfig
from specster.github import Comment, Issue
from specster.metrics import RunMetrics, extract_markers, strip_markers
from specster.sanitize import sanitize

TRUSTED: dict[str, frozenset[str]] = {
    "owner": frozenset({"OWNER"}),
    "collaborators": frozenset({"OWNER", "MEMBER", "COLLABORATOR"}),
}
_OWN = "<!-- specster:"
_FOOTER = re.compile(r"<details data-specster=\"metrics\">.*?</details>", re.DOTALL)
_FRAMING_TAG = re.compile(r"</?(entry|issue_thread)\b[^>]*>", re.IGNORECASE)
_FRAMING_BARE = re.compile(r"</?(?:entry|issue_thread)", re.IGNORECASE)


def _defuse(text: str) -> str:
    # Neutralize forged <entry>/<issue_thread> framing tags (and any attributes they
    # carry, e.g. a fake role) without a blanket HTML escape, so code snippets with
    # < and > stay readable to the model. A well-formed tag is collapsed to an inert
    # placeholder; a bare/unclosed occurrence just gets its "<" escaped.
    def collapse(m: re.Match[str]) -> str:
        slash = "/" if m.group(0)[1] == "/" else ""
        return f"&lt;{slash}{m.group(1).lower()}&gt;"

    text = _FRAMING_TAG.sub(collapse, text)
    return _FRAMING_BARE.sub(lambda m: "&lt;" + m.group(0)[1:], text)


@dataclass(frozen=True)
class HiddenItem:
    where: str
    content: str


@dataclass(frozen=True)
class Thread:
    text: str
    included: int
    untrusted: tuple[str, ...]
    after_label: int
    edited_after_label: int
    hidden: tuple[HiddenItem, ...]
    previous_runs: tuple[RunMetrics, ...]


def _entry(author: str, role: str, at: str, body: str) -> str:
    return f'<entry author="{escape(author)}" role="{role}" at="{at}">\n{_defuse(body)}\n</entry>'


def build_thread(
    issue: Issue, comments: Sequence[Comment], trust: TrustConfig, snapshot_at: datetime | None
) -> Thread:
    hidden: list[HiddenItem] = []
    body = sanitize(issue.body)
    hidden += [HiddenItem("issue body", h) for h in body.removed]
    entries = [_entry(issue.author, "author", "issue", f"# {issue.title}\n\n{body.text}")]
    untrusted: list[str] = []
    after = edited = included = 0
    previous: list[RunMetrics] = []
    allowed = TRUSTED.get(trust.comments)

    for comment in sorted(comments, key=lambda x: x.created_at):
        if comment.author_type == "Bot" and _OWN in comment.body:
            previous += extract_markers(comment.body)
            text = _FOOTER.sub("", strip_markers(comment.body))
            text = re.sub(r"<!-- specster:[^>]*-->", "", text, flags=re.DOTALL).strip()
            entries.append(_entry(comment.author, "specster", comment.created_at.isoformat(), text))
            continue
        if snapshot_at is not None and comment.created_at > snapshot_at:
            after += 1
            continue
        if snapshot_at is not None and comment.updated_at > snapshot_at:
            edited += 1
            continue
        if allowed is not None and comment.association not in allowed:
            untrusted.append(comment.author)
            continue
        clean = sanitize(comment.body)
        hidden += [HiddenItem(f"comment by {comment.author}", h) for h in clean.removed]
        role = "author" if comment.author == issue.author else "participant"
        entries.append(_entry(comment.author, role, comment.created_at.isoformat(), clean.text))
        included += 1

    text = "<issue_thread>\n" + "\n".join(entries) + "\n</issue_thread>"
    return Thread(text, included, tuple(untrusted), after, edited, tuple(hidden), tuple(previous))
