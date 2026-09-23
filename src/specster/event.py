from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from specster.config import LabelsConfig


class EventError(Exception):
    pass


@dataclass(frozen=True)
class Trigger:
    issue_number: int
    kind: Literal["labeled", "dispatch"]
    label: str | None
    sender: str
    sender_type: str


def parse_event(event_name: str, payload: Mapping[str, Any], dispatch_issue: str | None) -> Trigger:
    sender = payload.get("sender") or {}
    login, kind = str(sender.get("login", "")), str(sender.get("type", "User"))
    if event_name == "workflow_dispatch":
        if not dispatch_issue or not dispatch_issue.strip().isdigit():
            raise EventError("workflow_dispatch needs the issue_number input")
        return Trigger(int(dispatch_issue), "dispatch", None, login, kind)
    if event_name != "issues" or payload.get("action") != "labeled":
        raise EventError(f"unsupported event {event_name}/{payload.get('action')}")
    issue = payload["issue"]
    if "pull_request" in issue:
        raise EventError("labels on pull requests are not supported")
    return Trigger(int(issue["number"]), "labeled", str(payload["label"]["name"]), login, kind)


def skip_reason(trigger: Trigger, labels: LabelsConfig) -> str | None:
    if trigger.sender_type == "Bot":
        return f"sender {trigger.sender} is a bot"
    if trigger.kind == "labeled" and trigger.label != labels.spec:
        return f"label '{trigger.label}' is not '{labels.spec}'"
    return None
