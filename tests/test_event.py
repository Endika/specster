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


def pull_event(
    action: str, head_repo: str | None = "o/r", label: str = "", sender_type: str = "User"
) -> dict[str, object]:
    head: dict[str, object] = {"ref": "feature/x", "repo": None}
    if head_repo is not None:
        head["repo"] = {"full_name": head_repo}
    payload: dict[str, object] = {
        "action": action,
        "pull_request": {"number": 5, "head": head, "base": {"repo": {"full_name": "o/r"}}},
        "sender": {"login": "ana", "type": sender_type},
    }
    if label:
        payload["label"] = {"name": label}
    return payload


def test_closed_pull_request_is_a_cleanup() -> None:
    t = parse_event("pull_request", pull_event("closed", sender_type="Bot"), None)
    assert (t.issue_number, t.kind, t.head_ref) == (5, "closed", "feature/x")
    assert (t.head_repo, t.base_repo, t.same_repo) == ("o/r", "o/r", True)
    assert skip_reason(t, LabelsConfig()) is None
    assert phase_of(t, LabelsConfig()) == "cleanup"


@pytest.mark.parametrize("head_repo", ["fork/r", None])
def test_a_fork_or_deleted_fork_is_not_the_same_repository(head_repo: str | None) -> None:
    t = parse_event("pull_request", pull_event("closed", head_repo), None)
    assert t.head_repo == (head_repo or "") and not t.same_repo


def test_a_pull_request_label_becomes_a_pr_labeled_trigger() -> None:
    t = parse_event("pull_request", pull_event("labeled", label="ai-evidence"), None)
    assert t == Trigger(
        5,
        "pr_labeled",
        "ai-evidence",
        "ana",
        "User",
        head_ref="feature/x",
        head_repo="o/r",
        base_repo="o/r",
    )


def test_pull_request_labels_route_to_the_evidence_and_fix_phases() -> None:
    labels = LabelsConfig(evidence="show-me", fix="apply-review")
    show = parse_event("pull_request", pull_event("labeled", label="show-me"), None)
    apply = parse_event("pull_request", pull_event("labeled", label="apply-review"), None)
    assert skip_reason(show, labels) is None and phase_of(show, labels) == "evidence"
    assert skip_reason(apply, labels) is None and phase_of(apply, labels) == "fix"


@pytest.mark.parametrize("label", ["bug", "ai-spec", "ai-build"])
def test_any_other_pull_request_label_is_skipped(label: str) -> None:
    t = parse_event("pull_request", pull_event("labeled", label=label), None)
    assert skip_reason(t, LabelsConfig()) == f"label '{label}' is not 'ai-evidence' or 'ai-fix'"


def test_a_bot_labeling_a_pull_request_is_skipped() -> None:
    t = parse_event("pull_request", pull_event("labeled", label="ai-fix", sender_type="Bot"), None)
    assert skip_reason(t, LabelsConfig()) == "sender ana is a bot"


def test_other_pull_request_actions_are_unsupported() -> None:
    with pytest.raises(EventError):
        parse_event("pull_request", pull_event("opened"), None)


@pytest.mark.parametrize("label", ["ai-evidence", "ai-fix"])
def test_pull_request_labels_on_an_issue_are_skipped(label: str) -> None:
    t = parse_event("issues", labeled(label), None)
    assert skip_reason(t, LabelsConfig()) == f"label '{label}' is not 'ai-spec' or 'ai-build'"
