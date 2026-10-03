from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from specster.config import LabelsConfig

RunPhase = Literal["spec", "build", "cleanup", "evidence", "fix"]


class EventError(Exception):
    pass


@dataclass(frozen=True)
class Trigger:
    issue_number: int
    kind: Literal["labeled", "dispatch", "closed", "pr_labeled"]
    label: str | None
    sender: str
    sender_type: str
    dispatch_phase: RunPhase = "spec"
    head_ref: str = ""
    # owner/name of each side of a pull request; "" when the head's fork was deleted.
    head_repo: str = ""
    base_repo: str = ""

    @property
    def same_repo(self) -> bool:
        return self.head_repo != "" and self.head_repo == self.base_repo


def _repo_of(side: Mapping[str, Any]) -> str:
    return str((side.get("repo") or {}).get("full_name") or "")


def parse_event(
    event_name: str,
    payload: Mapping[str, Any],
    dispatch_issue: str | None,
    dispatch_phase: str | None = None,
) -> Trigger:
    sender = payload.get("sender") or {}
    login, kind = str(sender.get("login", "")), str(sender.get("type", "User"))
    if event_name == "workflow_dispatch":
        if not dispatch_issue or not dispatch_issue.strip().isdigit():
            raise EventError("workflow_dispatch needs the issue_number input")
        phase = (dispatch_phase or "spec").strip() or "spec"
        if phase not in ("spec", "build"):
            raise EventError("workflow_dispatch phase must be spec or build")
        return Trigger(int(dispatch_issue), "dispatch", None, login, kind, cast(RunPhase, phase))
    if event_name == "pull_request":
        action = payload.get("action")
        if action not in ("closed", "labeled"):
            raise EventError(f"unsupported event {event_name}/{action}")
        pr = payload["pull_request"]
        head, base = pr["head"], pr["base"]
        return Trigger(
            int(pr["number"]),
            "closed" if action == "closed" else "pr_labeled",
            str(payload["label"]["name"]) if action == "labeled" else None,
            login,
            kind,
            head_ref=str(head["ref"]),
            head_repo=_repo_of(head),
            base_repo=_repo_of(base),
        )
    if event_name != "issues" or payload.get("action") != "labeled":
        raise EventError(f"unsupported event {event_name}/{payload.get('action')}")
    issue = payload["issue"]
    if "pull_request" in issue:
        raise EventError("labels on pull requests are not supported")
    return Trigger(int(issue["number"]), "labeled", str(payload["label"]["name"]), login, kind)


def skip_reason(trigger: Trigger, labels: LabelsConfig) -> str | None:
    # Bots merge and close pull requests too, and a cleanup never writes to the issue.
    if trigger.kind == "closed":
        return None
    if trigger.sender_type == "Bot":
        return f"sender {trigger.sender} is a bot"
    if trigger.kind == "labeled" and trigger.label not in (labels.spec, labels.build):
        return f"label '{trigger.label}' is not '{labels.spec}' or '{labels.build}'"
    if trigger.kind == "pr_labeled" and trigger.label not in (labels.evidence, labels.fix):
        return f"label '{trigger.label}' is not '{labels.evidence}' or '{labels.fix}'"
    return None


def phase_of(trigger: Trigger, labels: LabelsConfig) -> RunPhase:
    if trigger.kind == "dispatch":
        return trigger.dispatch_phase
    if trigger.kind == "closed":
        return "cleanup"
    if trigger.kind == "pr_labeled":
        return "fix" if trigger.label == labels.fix else "evidence"
    if trigger.label == labels.build:
        return "build"
    return "spec"
