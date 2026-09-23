import pytest

from specster.config import LabelsConfig
from specster.event import EventError, Trigger, parse_event, skip_reason


def labeled(label: str, sender_type: str = "User") -> dict[str, object]:
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {"number": 7},
        "sender": {"login": "endika", "type": sender_type},
    }


def test_labeled_event_becomes_trigger() -> None:
    assert parse_event("issues", labeled("ai-spec"), None) == Trigger(
        7, "labeled", "ai-spec", "endika", "User"
    )


def test_other_label_is_skipped() -> None:
    reason = skip_reason(parse_event("issues", labeled("bug"), None), LabelsConfig())
    assert reason == "label 'bug' is not 'ai-spec'"


def test_bot_sender_is_skipped_to_avoid_loops() -> None:
    reason = skip_reason(parse_event("issues", labeled("ai-spec", "Bot"), None), LabelsConfig())
    assert reason == "sender endika is a bot"


def test_dispatch_needs_issue_number() -> None:
    with pytest.raises(EventError, match="issue_number"):
        parse_event("workflow_dispatch", {"sender": {"login": "e", "type": "User"}}, None)


def test_dispatch_with_issue_runs() -> None:
    trig = parse_event("workflow_dispatch", {"sender": {"login": "e", "type": "User"}}, "12")
    assert trig.kind == "dispatch" and trig.issue_number == 12
    assert skip_reason(trig, LabelsConfig()) is None


def test_pull_request_label_is_rejected() -> None:
    payload = labeled("ai-spec") | {"issue": {"number": 7, "pull_request": {"url": "x"}}}
    with pytest.raises(EventError, match="pull request"):
        parse_event("issues", payload, None)
