from datetime import timedelta
from pathlib import Path

from specster.config import PersonaConfig
from specster.github import Comment
from specster.llm.base import ToolCall
from specster.metrics import RunMetrics, last_marker
from specster.plan import normalize_plan
from specster.render import RenderContext, render_spec
from specster.schemas import SpecResult
from tests.fakes import ScriptedModel, bot_comment, spec_comment_body
from tests.test_run import QUESTIONS, SPEC, T0, comment, env, run, tracker

OLD = SPEC | {"objective": "Export CSV with commas."}


def test_an_owner_comment_after_the_spec_puts_the_prompt_in_revision_mode(tmp_path: Path) -> None:
    previous = bot_comment(9, spec_comment_body(OLD), T0 - timedelta(hours=2))
    ask = comment(1, "endika", "OWNER", "Use semicolons instead.", T0 - timedelta(hours=1))
    revised = SPEC | {"changes": ["Separator is now a semicolon."]}
    tr, model = tracker([previous, ask]), ScriptedModel([[ToolCall("1", "submit_spec", revised)]])
    assert run(env(tmp_path), tr, model) == 0
    assert "Revision mode" in model.system
    assert "previous_spec-" in model.user_text and "Export CSV with commas." in model.user_text
    assert "Changes from the previous spec" in tr.posted[0]
    assert "Separator is now a semicolon." in tr.posted[0]
    marker = last_marker(tr.posted[0])
    assert marker is not None and marker.revision


def test_without_comments_after_the_spec_changes_are_refused_and_hidden(tmp_path: Path) -> None:
    previous = bot_comment(9, spec_comment_body(OLD), T0 - timedelta(hours=2))
    bad = SPEC | {"changes": ["something"]}
    model = ScriptedModel(
        [[ToolCall("1", "submit_spec", bad)], [ToolCall("2", "submit_spec", SPEC)]]
    )
    tr = tracker([previous])
    assert run(env(tmp_path), tr, model) == 0
    assert "Revision mode" not in model.system
    assert "changes is only for revising" in model.received[1][0].content
    assert "Changes from the previous spec" not in tr.posted[0]


def test_a_previous_spec_with_a_broken_plan_marker_runs_without_revision(tmp_path: Path) -> None:
    previous = bot_comment(9, spec_comment_body(OLD) + "\ntampered", T0 - timedelta(hours=2))
    ask = comment(1, "endika", "OWNER", "Use semicolons instead.", T0 - timedelta(hours=1))
    tr, model = tracker([previous, ask]), ScriptedModel([[ToolCall("1", "submit_spec", SPEC)]])
    assert run(env(tmp_path), tr, model) == 0
    assert "Revision mode" not in model.system and "previous_spec-" not in model.user_text
    marker = last_marker(tr.posted[0])
    assert marker is not None and not marker.revision
    assert "previous spec has no valid plan marker: revision mode is off" in marker.warnings


def test_untrusted_or_late_comments_do_not_start_a_revision(tmp_path: Path) -> None:
    previous = bot_comment(9, spec_comment_body(OLD), T0 - timedelta(hours=2))
    stranger = comment(1, "eve", "NONE", "Rewrite it all.", T0 - timedelta(hours=1))
    late = comment(2, "endika", "OWNER", "After the label.", T0 + timedelta(minutes=30))
    tr = tracker([previous, stranger, late])
    model = ScriptedModel([[ToolCall("1", "submit_spec", SPEC)]])
    assert run(env(tmp_path), tr, model) == 0
    assert "Revision mode" not in model.system


def test_changes_render_in_spanish_right_under_the_title() -> None:
    result = SpecResult.model_validate(SPEC | {"changes": ["Punto y coma."]})
    tasks, fixes = normalize_plan(result.tasks)
    m = RunMetrics(run_id="1", outcome="spec", provider="p", model="m")
    body = render_spec(result, tasks, fixes, RenderContext(PersonaConfig(language="es"), m, (), ()))
    lines = body.splitlines()
    title = next(i for i, line in enumerate(lines) if line.startswith("### "))
    assert lines[title + 2 : title + 4] == [
        "**Cambios respecto a la spec anterior**",
        "- Punto y coma.",
    ]


UNKNOWN = "Specster's bot login is unknown"


def test_a_spec_from_another_app_never_starts_a_revision_once_the_login_is_known(
    tmp_path: Path,
) -> None:
    body = spec_comment_body(OLD)
    other = Comment(
        9, "other-app[bot]", "Bot", "NONE", body, T0 - timedelta(hours=2), T0 - timedelta(hours=2)
    )
    ask = comment(1, "endika", "OWNER", "Use semicolons instead.", T0 - timedelta(hours=1))
    tr, model = tracker([other, ask]), ScriptedModel([[ToolCall("1", "submit_spec", SPEC)]])
    tr.login = "specster[bot]"
    assert run(env(tmp_path), tr, model) == 0
    assert "Revision mode" not in model.system
    assert "Export CSV with commas." not in model.user_text
    marker = last_marker(tr.posted[0])
    assert marker is not None and not any(UNKNOWN in w for w in marker.warnings)


def test_the_configured_login_wins_and_without_one_any_bot_spec_counts(tmp_path: Path) -> None:
    previous = bot_comment(9, spec_comment_body(OLD), T0 - timedelta(hours=2))
    ask = comment(1, "endika", "OWNER", "Use semicolons instead.", T0 - timedelta(hours=1))
    revised = SPEC | {"changes": ["Semicolons."]}
    tr, model = tracker([previous, ask]), ScriptedModel([[ToolCall("1", "submit_spec", revised)]])
    assert run(env(tmp_path), tr, model) == 0
    assert "Revision mode" in model.system
    marker = last_marker(tr.posted[0])
    assert marker is not None and any(UNKNOWN in w for w in marker.warnings)

    config = "identity:\n  bot_login: other-app[bot]\n"
    tr, model = tracker([previous, ask]), ScriptedModel([[ToolCall("1", "submit_spec", SPEC)]])
    tr.login = "specster[bot]"
    assert run(env(tmp_path, config=config), tr, model) == 0
    assert "Revision mode" not in model.system


def test_questions_asked_in_revision_mode_are_not_marked_as_a_revision(tmp_path: Path) -> None:
    previous = bot_comment(9, spec_comment_body(OLD), T0 - timedelta(hours=2))
    ask = comment(1, "endika", "OWNER", "Use semicolons instead.", T0 - timedelta(hours=1))
    tr = tracker([previous, ask])
    model = ScriptedModel([[ToolCall("1", "submit_questions", QUESTIONS)]])
    assert run(env(tmp_path), tr, model) == 0
    assert "Revision mode" in model.system
    marker = last_marker(tr.posted[0])
    assert marker is not None and marker.outcome == "questions" and not marker.revision
