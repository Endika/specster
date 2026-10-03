from collections.abc import Sequence
from datetime import datetime, timedelta

from specster.config import TrustConfig
from specster.github import Comment, PullInfo, Review, ReviewComment, ReviewThread
from specster.review_input import (
    COMMENT_MAX_CHARS,
    HUNK_MAX_CHARS,
    REVIEW_MAX_CHARS,
    ReviewReading,
    answered_marker,
    read_review,
)
from specster.thread import FOOTER_OPEN
from tests.fakes import bot_comment

T0 = datetime.fromisoformat("2026-01-01T10:00:00+00:00")
SNAPSHOT = T0 + timedelta(hours=1)
PULL = PullInfo(
    5, "open", False, "t", "b", "ana", "NONE", "h" * 40, "f", "o/r", "b" * 40, "main", "o/r"
)


def rc(
    id: int, body: str, author: str = "bob", hunk: str = "@@", line: int | None = 3
) -> ReviewComment:
    return ReviewComment(id, author, "MEMBER", body, "app.py", line, 2, hunk, T0, None)


def read(
    threads: Sequence[ReviewThread],
    reviews: Sequence[Review] = (),
    comments: Sequence[Comment] = (),
    login: str | None = "specster[bot]",
) -> ReviewReading:
    return read_review(
        PULL, threads, reviews, comments, TrustConfig(), SNAPSHOT, "n0nce", login=login
    )


def test_an_outdated_thread_keeps_its_original_line_and_the_end_of_a_long_hunk() -> None:
    hunk = "@@ -1 +1 @@\n" + "x" * HUNK_MAX_CHARS + "\nTHE-LINE"
    got = read([ReviewThread(False, (rc(7, "Fix it.", hunk=hunk, line=None),))])
    assert got.items[0].where == "app.py:2" and got.items[0].reply_to == 7
    assert '<thread-n0nce id="c7" path="app.py" line="2" outdated="true">' in got.text
    assert "THE-LINE" in got.text and "@@ -1 +1 @@" not in got.text
    assert f"(cut to its last {HUNK_MAX_CHARS:,} of" in got.text


def test_a_long_comment_is_cut_and_the_cut_is_reported() -> None:
    got = read([ReviewThread(False, (rc(7, "y" * (COMMENT_MAX_CHARS + 5)),))])
    assert f'cut="cut to its first {COMMENT_MAX_CHARS:,} of' in got.text
    assert got.truncations and "a review comment by bob" in got.truncations[0]


def test_items_past_the_review_cap_are_left_out_and_said() -> None:
    # Each thread holds just over half the cap, in comments short enough to stay whole.
    part = "z" * (REVIEW_MAX_CHARS // 6 + 10)
    threads = [
        ReviewThread(False, tuple(rc(i * 10 + j, part) for j in range(3))) for i in (1, 2, 3)
    ]
    got = read(threads)
    assert [i.key for i in got.items] == ["c10"] and got.included == 3
    assert "2 of 3 review items left out" in got.truncations[-1]


def test_a_review_answered_in_an_earlier_fix_comment_is_skipped() -> None:
    review = Review(9, "bob", "MEMBER", "CHANGES_REQUESTED", "Rename x.", T0)
    note = bot_comment(1, f"done\n\n{FOOTER_OPEN}</details>\n{answered_marker(['r9'])}", T0)
    got = read([], [review], [note])
    assert got.items == () and got.answered == 1
    # A forged marker from someone else answers nothing.
    forged = Comment(2, "mallory", "User", "NONE", answered_marker(["r9"]), T0, T0)
    assert [i.key for i in read([], [review], [forged]).items] == ["r9"]


def test_with_the_login_unknown_no_review_comment_counts_as_specster() -> None:
    reply = rc(8, "Applied in abc.", author="specster[bot]")
    got = read([ReviewThread(False, (rc(7, "Fix it."), reply))], login=None)
    assert [i.key for i in got.items] == ["c7"] and 'role="specster"' not in got.text


def test_a_review_edited_after_the_label_is_left_out_and_counted() -> None:
    edited = Review(9, "bob", "MEMBER", "COMMENTED", "Rename x.", T0, SNAPSHOT + timedelta(1))
    got = read([], [edited])
    assert got.items == () and got.edited_after_label == 1


def test_a_review_edited_after_specster_answered_it_counts_again() -> None:
    note = bot_comment(1, answered_marker(["r9"]), T0 + timedelta(minutes=5))
    edited = Review(9, "bob", "MEMBER", "COMMENTED", "Rename y.", T0, T0 + timedelta(minutes=9))
    assert [i.key for i in read([], [edited], [note]).items] == ["r9"]
