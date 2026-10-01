import pytest

from specster.config import LabelsConfig
from specster.event import EventError, Trigger, parse_event, phase_of, skip_reason

SENDER = {"sender": {"login": "e", "type": "User"}}


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
    assert reason == "label 'bug' is not 'ai-spec' or 'ai-build'"


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


def test_build_label_and_dispatch_phase_select_the_build_phase() -> None:
    labels = LabelsConfig()
    trig = parse_event("issues", labeled("ai-build"), None)
    assert skip_reason(trig, labels) is None and phase_of(trig, labels) == "build"
    assert phase_of(parse_event("issues", labeled("ai-spec"), None), labels) == "spec"
    assert phase_of(parse_event("workflow_dispatch", SENDER, "3", "build"), labels) == "build"
    assert phase_of(parse_event("workflow_dispatch", SENDER, "3", ""), labels) == "spec"


def test_unknown_dispatch_phase_is_rejected() -> None:
    with pytest.raises(EventError, match="phase"):
        parse_event("workflow_dispatch", SENDER, "3", "deploy")


def test_closed_pull_request_is_a_cleanup() -> None:
    payload = {
        "action": "closed",
        "pull_request": {"number": 5, "head": {"ref": "specster/issue-3"}},
        "sender": {"login": "renovate[bot]", "type": "Bot"},
    }
    t = parse_event("pull_request", payload, None)
    assert (t.issue_number, t.kind, t.head_ref) == (5, "closed", "specster/issue-3")
    assert skip_reason(t, LabelsConfig()) is None
    assert phase_of(t, LabelsConfig()) == "cleanup"


def test_other_pull_request_actions_are_unsupported() -> None:
    payload = {"action": "opened", "pull_request": {"number": 1, "head": {"ref": "x"}}}
    with pytest.raises(EventError):
        parse_event("pull_request", payload, None)
