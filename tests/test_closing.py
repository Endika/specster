import pytest

from specster.closing import closes_an_issue, drop_references, rewrite_references

FORMS = [
    ("#3", "issue 3"),
    ("o/r#3", "o/r issue 3"),
    ("https://github.com/o/r/issues/3", "o/r issue 3"),
    ("http://github.com/o/r/issues/3", "o/r issue 3"),
    ("github.com/o/r/issues/3", "o/r issue 3"),
    ("www.github.com/o/r/issues/3", "o/r issue 3"),
    ("GH-3", "issue 3"),
    ("gh-3", "issue 3"),
]
KEYWORDS = ["Fixes ", "closed ", "RESOLVE ", "fix: ", "Closes:", "resolves\n"]


@pytest.mark.parametrize(("ref", "plain"), FORMS)
@pytest.mark.parametrize("keyword", KEYWORDS)
def test_every_reference_after_a_keyword_is_spelled_out(keyword: str, ref: str, plain: str) -> None:
    text = f"this {keyword}{ref} now"
    assert closes_an_issue(text)
    out = rewrite_references(text)
    assert out == f"this {keyword}{plain} now" and not closes_an_issue(out)


@pytest.mark.parametrize(
    "text",
    ["see #3", "prefix #3", "fixture #3", "fixes the parser", "#3", "GH-3", "fixes issue 3"],
)
def test_other_text_is_left_alone(text: str) -> None:
    assert not closes_an_issue(text) and rewrite_references(text) == text


def test_every_reference_in_a_text_is_rewritten() -> None:
    text = "Fixes #1, and closes o/r#2; resolved GH-3"
    assert rewrite_references(text) == "Fixes issue 1, and closes o/r issue 2; resolved issue 3"


def test_dropping_references_leaves_no_issue_to_close() -> None:
    assert drop_references("Fixes #12 and o/r#4 export") == "Fixes issue 12 and o/r issue 4 export"
