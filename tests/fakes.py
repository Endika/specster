from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from specster.github import Comment, Issue


@dataclass
class FakeTracker:
    issue: Issue
    comments: list[Comment] = field(default_factory=list)
    label_events: dict[str, datetime] = field(default_factory=dict)
    edited_at: datetime | None = None
    repo_labels: set[str] = field(default_factory=set)
    posted: list[str] = field(default_factory=list)
    now: datetime = datetime(2026, 1, 1, 12, tzinfo=UTC)

    def get_issue(self, number: int) -> Issue:
        return self.issue

    def list_comments(self, number: int) -> list[Comment]:
        return list(self.comments)

    def label_applied_at(self, number: int, label: str) -> datetime | None:
        return self.label_events.get(label)

    def body_edited_at(self, number: int) -> datetime | None:
        return self.edited_at

    def ensure_labels(self, labels: Mapping[str, str]) -> None:
        self.repo_labels |= set(labels)

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        self.issue = _with_labels(self.issue, set(self.issue.labels) | set(labels))

    def remove_label(self, number: int, label: str) -> None:
        self.issue = _with_labels(self.issue, set(self.issue.labels) - {label})

    def post_comment(self, number: int, body: str) -> None:
        self.posted.append(body)


def _with_labels(issue: Issue, labels: set[str]) -> Issue:
    return Issue(
        issue.number,
        issue.title,
        issue.body,
        issue.author,
        issue.author_association,
        tuple(sorted(labels)),
    )
