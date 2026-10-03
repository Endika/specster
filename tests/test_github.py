import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from specster import github
from specster.github import GitHubError, GitHubRest


class FakeGitHubServer:
    def __init__(self) -> None:
        self.labels_on_issue = {"ai-spec"}
        self.repo_labels = {"ai-spec"}
        self.comments: list[str] = []
        self.branches = {"specster/issue-3"}
        self.pulls: list[dict[str, object]] = []
        self.pull_refused: set[str] = set()
        self.patched: dict[int, dict[str, object]] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/repos/o/r/issues/7" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "title": "T",
                    "body": None,
                    "user": {"login": "ana"},
                    "author_association": "NONE",
                    "labels": [{"name": n} for n in self.labels_on_issue],
                },
            )
        if path == "/repos/o/r/issues/7/comments" and method == "GET":
            page = request.url.params.get("page", "1")
            if page == "1":
                return httpx.Response(
                    200,
                    json=[self._c(1)],
                    headers={
                        "Link": (
                            "<https://api.github.com/repos/o/r/issues/7/comments"
                            '?per_page=100&page=2>; rel="next"'
                        )
                    },
                )
            return httpx.Response(200, json=[self._c(2)])
        if path == "/repos/o/r/issues/7/events" and method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "event": "labeled",
                        "label": {"name": "ai-spec"},
                        "created_at": "2026-01-01T10:00:00Z",
                    },
                    {
                        "event": "labeled",
                        "label": {"name": "bug"},
                        "created_at": "2026-01-03T10:00:00Z",
                    },
                    {
                        "event": "labeled",
                        "label": {"name": "ai-spec"},
                        "created_at": "2026-01-02T10:00:00Z",
                    },
                ],
            )
        if path == "/graphql":
            return httpx.Response(
                200, json={"data": {"repository": {"issue": {"lastEditedAt": None}}}}
            )
        if path == "/repos/o/r/labels" and method == "POST":
            self.repo_labels.add(json.loads(request.content)["name"])
            return httpx.Response(201, json={})
        if path.startswith("/repos/o/r/labels/") and method == "GET":
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(200 if name in self.repo_labels else 404, json={})
        if path == "/repos/o/r/issues/7/labels" and method == "POST":
            self.labels_on_issue |= set(json.loads(request.content)["labels"])
            return httpx.Response(200, json=[])
        if path.startswith("/repos/o/r/issues/7/labels/") and method == "DELETE":
            name = path.rsplit("/", 1)[-1]
            if name not in self.labels_on_issue:
                return httpx.Response(404, json={"message": "Label does not exist"})
            self.labels_on_issue.discard(name)
            return httpx.Response(200, json=[])
        if path == "/repos/o/r/issues/7/comments" and method == "POST":
            self.comments.append(json.loads(request.content)["body"])
            return httpx.Response(201, json={})
        if path == "/repos/o/r" and method == "GET":
            return httpx.Response(200, json={"default_branch": "trunk"})
        if path.startswith("/repos/o/r/branches/") and method == "GET":
            name = path.removeprefix("/repos/o/r/branches/")
            if name == "secret":
                return httpx.Response(403, json={"message": "Resource not accessible"})
            if name not in self.branches | {"trunk"}:
                return httpx.Response(404, json={"message": "Branch not found"})
            return httpx.Response(200, json={"name": name, "protected": name == "trunk"})
        if path.startswith("/repos/o/r/git/ref/heads/") and method == "GET":
            name = path.removeprefix("/repos/o/r/git/ref/heads/")
            return httpx.Response(200 if name in self.branches else 404, json={})
        if path == "/repos/o/r/pulls" and method == "POST":
            sent = json.loads(request.content)
            if sent["head"] in self.pull_refused:
                return httpx.Response(
                    403,
                    json={
                        "message": (
                            "GitHub Actions is not permitted to create or approve pull requests."
                        )
                    },
                )
            self.pulls.append(sent)
            return httpx.Response(
                201, json={"number": 12, "html_url": "https://github.com/o/r/pull/12"}
            )
        if path.startswith("/repos/o/r/pulls/") and method == "PATCH":
            self.patched[int(path.rsplit("/", 1)[-1])] = json.loads(request.content)
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})

    @staticmethod
    def _c(i: int) -> dict[str, object]:
        return {
            "id": i,
            "user": {"login": f"u{i}", "type": "User"},
            "author_association": "MEMBER",
            "body": f"c{i}",
            "created_at": "2026-01-01T09:00:00Z",
            "updated_at": "2026-01-01T09:00:00Z",
        }


def client(server: FakeGitHubServer) -> GitHubRest:
    return GitHubRest("o/r", "tok", transport=httpx.MockTransport(server.handle))


def test_reads_issue_and_all_comment_pages() -> None:
    server = FakeGitHubServer()
    gh = client(server)
    assert gh.get_issue(7).body == ""
    assert [c.body for c in gh.list_comments(7)] == ["c1", "c2"]


def test_label_time_is_the_latest_application_of_that_label() -> None:
    assert client(FakeGitHubServer()).label_applied_at(7, "ai-spec") == datetime(
        2026, 1, 2, 10, tzinfo=UTC
    )


def test_label_swap_and_comment_change_server_state() -> None:
    server = FakeGitHubServer()
    gh = client(server)
    gh.ensure_labels({"ai-spec": "5319e7", "spec-ready": "0e8a16"})
    gh.remove_label(7, "ai-spec")
    gh.remove_label(7, "ai-spec")  # already gone: no error
    gh.add_labels(7, ["spec-ready"])
    gh.post_comment(7, "hello")
    assert server.repo_labels == {"ai-spec", "spec-ready"}
    assert server.labels_on_issue == {"spec-ready"}
    assert server.comments == ["hello"]


def viewer_client(response: httpx.Response) -> GitHubRest:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "viewer" in json.loads(request.content)["query"]
        return response

    return GitHubRest("o/r", "tok", transport=httpx.MockTransport(handle))


def test_own_login_reads_the_viewer_and_names_a_bot_like_its_comments() -> None:
    bot = {"data": {"viewer": {"login": "specster-endika", "__typename": "Bot"}}}
    user = {"data": {"viewer": {"login": "endika", "__typename": "User"}}}
    assert viewer_client(httpx.Response(200, json=bot)).own_login() == "specster-endika[bot]"
    assert viewer_client(httpx.Response(200, json=user)).own_login() == "endika"


def test_own_login_is_none_when_the_token_cannot_say() -> None:
    denied = {"errors": [{"message": "Resource not accessible by integration"}]}
    assert viewer_client(httpx.Response(200, json=denied)).own_login() is None
    assert viewer_client(httpx.Response(403, json={})).own_login() is None
    assert viewer_client(httpx.Response(200, text="not json")).own_login() is None


def test_user_id_reads_the_account_and_is_none_when_github_cannot_say() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/specster-endika[bot]":
            return httpx.Response(200, json={"login": "specster-endika[bot]", "id": 333103899})
        return httpx.Response(404, json={"message": "Not Found"})

    gh = GitHubRest("o/r", "tok", transport=httpx.MockTransport(handle))
    assert gh.user_id("specster-endika[bot]") == 333103899
    assert gh.user_id("nobody") is None


def test_default_branch_and_branch_existence() -> None:
    gh = client(FakeGitHubServer())
    assert gh.default_branch() == "trunk"
    assert gh.branch_exists("specster/issue-3") and not gh.branch_exists("specster/issue-7")


def test_branch_protection_is_read_and_a_missing_branch_is_not_protected() -> None:
    gh = client(FakeGitHubServer())
    assert gh.branch_protected("trunk") and not gh.branch_protected("specster/issue-3")
    assert not gh.branch_protected("specster/issue-7")
    with pytest.raises(httpx.HTTPStatusError):
        gh.branch_protected("secret")


def test_create_pull_returns_its_url_and_explains_a_refusal() -> None:
    server = FakeGitHubServer()
    gh = client(server)
    pr = gh.create_pull("CSV", "Closes #7", "specster/issue-7", "trunk")
    assert pr.url.endswith("/pull/12") and server.pulls[0]["maintainer_can_modify"] is False
    server.pull_refused.add("specster/issue-8")
    with pytest.raises(GitHubError, match="HTTP 403: GitHub Actions is not permitted") as e:
        gh.create_pull("CSV", "b", "specster/issue-8", "trunk")
    assert e.value.status == 403


def test_a_renamed_repo_redirect_is_followed() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/r":
            return httpx.Response(
                301, headers={"Location": "https://api.github.com/repositories/1"}
            )
        if request.url.path == "/repositories/1":
            return httpx.Response(200, json={"default_branch": "trunk"})
        return httpx.Response(404, json={})

    gh = GitHubRest("o/r", "tok", transport=httpx.MockTransport(handle))
    assert gh.default_branch() == "trunk"


def test_a_created_pull_without_its_url_is_an_error() -> None:
    gh = GitHubRest(
        "o/r", "tok", transport=httpx.MockTransport(lambda _: httpx.Response(201, json={}))
    )
    with pytest.raises(GitHubError, match="HTTP 201 without a number and url"):
        gh.create_pull("t", "b", "h", "main")


def test_update_pull_patches_only_its_body() -> None:
    server = FakeGitHubServer()
    client(server).update_pull(5, "new body")
    assert server.patched == {5: {"body": "new body"}}


def test_a_refused_pull_update_raises() -> None:
    gh = GitHubRest(
        "o/r",
        "tok",
        transport=httpx.MockTransport(lambda _: httpx.Response(502, json={"message": "Bad"})),
    )
    with pytest.raises(GitHubError, match="update pull request: HTTP 502: Bad") as e:
        gh.update_pull(5, "b")
    assert e.value.status == 502


def test_a_pull_update_that_never_reaches_github_raises() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    gh = GitHubRest("o/r", "tok", transport=httpx.MockTransport(handle))
    with pytest.raises(GitHubError, match="update pull request: ConnectError: connection reset"):
        gh.update_pull(5, "b")


def _pull_json() -> dict[str, object]:
    return {
        "number": 9,
        "state": "open",
        "draft": False,
        "title": "T",
        "body": None,
        "user": {"login": "ana"},
        "author_association": "MEMBER",
        "head": {"sha": "h1", "ref": "feat", "repo": {"full_name": "o/r"}},
        "base": {"sha": "b1", "ref": "main", "repo": {"full_name": "o/r"}},
    }


def test_get_pull_reads_both_sides_and_survives_a_deleted_fork() -> None:
    pull = _pull_json()
    pull["head"] = {"sha": "h1", "ref": "feat", "repo": None}
    pull["user"] = None

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/o/r/pulls/9"
        return httpx.Response(200, json=pull)

    info = GitHubRest("o/r", "tok", transport=httpx.MockTransport(handle)).get_pull(9)
    assert (info.head_sha, info.head_repo, info.base_repo, info.base_ref) == (
        "h1",
        "",
        "o/r",
        "main",
    )
    assert (info.author, info.body, info.draft, info.state) == ("ghost", "", False, "open")


def _file(i: int, patch: str | None = "+x") -> dict[str, object]:
    return {
        "filename": f"f{i}.py",
        "status": "modified",
        "additions": 1,
        "deletions": 0,
        "patch": patch,
    }


def _files_client(pages: list[list[dict[str, object]]]) -> GitHubRest:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/o/r/pulls/9/files"
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page < len(pages):
            headers["Link"] = (
                f'<https://api.github.com/repos/o/r/pulls/9/files?page={page + 1}>; rel="next"'
            )
        return httpx.Response(200, json=pages[page - 1], headers=headers)

    return GitHubRest("o/r", "tok", transport=httpx.MockTransport(handle))


def test_pull_files_follows_pages_and_keeps_binary_files_without_a_patch() -> None:
    result = _files_client([[_file(1)], [_file(2, None)]]).pull_files(9)
    assert [(f.path, f.patch) for f in result.files] == [("f1.py", "+x"), ("f2.py", "")]
    assert not result.truncated


def test_pull_files_stops_at_the_file_cap_and_says_so() -> None:
    pages = [[_file(i) for i in range(100)] for _ in range(4)]
    result = _files_client(pages).pull_files(9)
    assert len(result.files) == github.MAX_PULL_FILES
    assert result.truncated


def test_pull_files_exactly_at_the_file_cap_is_not_truncated() -> None:
    pages = [[_file(i) for i in range(100)] for _ in range(3)]
    result = _files_client(pages).pull_files(9)
    assert len(result.files) == github.MAX_PULL_FILES
    assert not result.truncated


def test_pull_files_stops_before_the_file_that_overflows_the_patch_budget() -> None:
    big = "+" * (github.MAX_PULL_PATCH_CHARS // 2 + 1)
    result = _files_client([[_file(1, big), _file(2, big), _file(3)]]).pull_files(9)
    assert [f.path for f in result.files] == ["f1.py"]
    assert result.truncated


def _thread(i: int, resolved: bool = False, total: int = 1) -> dict[str, object]:
    return {
        "isResolved": resolved,
        "comments": {
            "totalCount": total,
            "nodes": [
                {
                    "databaseId": i,
                    "author": None if i == 2 else {"login": "rev"},
                    "authorAssociation": "MEMBER",
                    "body": f"b{i}",
                    "path": "a.py",
                    "line": None,
                    "originalLine": 4,
                    "diffHunk": "@@",
                    "createdAt": "2026-01-01T09:00:00Z",
                    "lastEditedAt": "2026-01-02T09:00:00Z" if i == 1 else None,
                }
            ],
        },
    }


def _threads_client(
    pages: list[list[dict[str, object]]], seen: list[object] | None = None
) -> GitHubRest:
    def handle(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        if seen is not None:
            seen.append(variables["c"])
        index = 0 if variables["c"] is None else int(variables["c"])
        more = index + 1 < len(pages)
        page = {
            "pageInfo": {"hasNextPage": more, "endCursor": str(index + 1)},
            "nodes": pages[index],
        }
        return httpx.Response(
            200, json={"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}
        )

    return GitHubRest("o/r", "tok", transport=httpx.MockTransport(handle))


def test_review_threads_follow_the_cursor_and_map_every_field() -> None:
    seen: list[object] = []
    threads = _threads_client([[_thread(1)], [_thread(2, resolved=True)]], seen).review_threads(9)
    assert seen == [None, "1"]
    assert [t.is_resolved for t in threads] == [False, True]
    first = threads[0].comments[0]
    assert (first.id, first.author, first.association, first.path) == (1, "rev", "MEMBER", "a.py")
    assert (first.line, first.original_line, first.diff_hunk) == (None, 4, "@@")
    assert first.edited_at == datetime(2026, 1, 2, 9, tzinfo=UTC)
    assert threads[1].comments[0].author == "ghost"
    assert threads[1].comments[0].edited_at is None


def test_review_threads_refuse_to_drop_threads_past_the_page_bound() -> None:
    endless = [[_thread(1)]] * (github.MAX_THREAD_PAGES + 1)
    with pytest.raises(GitHubError, match="more than"):
        _threads_client(endless).review_threads(9)


def test_review_threads_refuse_to_drop_comments_past_the_per_thread_bound() -> None:
    too_many = _thread(1, total=github.COMMENTS_PER_THREAD + 1)
    with pytest.raises(GitHubError, match="more than"):
        _threads_client([[too_many]]).review_threads(9)


def test_review_threads_surface_graphql_errors() -> None:
    gh = GitHubRest(
        "o/r",
        "tok",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"errors": [{"message": "Could not resolve"}]})
        ),
    )
    with pytest.raises(GitHubError, match="Could not resolve"):
        gh.review_threads(9)


def test_review_threads_of_an_unknown_pull_request_raise_a_github_error() -> None:
    gh = GitHubRest(
        "o/r",
        "tok",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": {"repository": {"pullRequest": None}}})
        ),
    )
    with pytest.raises(GitHubError, match="no pull request #9"):
        gh.review_threads(9)


def _reviews_client(pages: list[list[dict[str, Any]]], seen: list[object]) -> GitHubRest:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        variables = json.loads(request.content)["variables"]
        seen.append(variables["c"])
        index = len(seen) - 1
        more = index + 1 < len(pages)
        page = {
            "pageInfo": {"hasNextPage": more, "endCursor": str(index + 1)},
            "nodes": pages[index],
        }
        return httpx.Response(
            200, json={"data": {"repository": {"pullRequest": {"reviews": page}}}}
        )

    return GitHubRest(
        "o/r",
        "tok",
        graphql_url="https://api.github.com/graphql",
        transport=httpx.MockTransport(handle),
    )


def test_reviews_follow_the_cursor_and_say_when_their_text_was_edited() -> None:
    review = {
        "databaseId": 5,
        "author": {"login": "rev"},
        "authorAssociation": "OWNER",
        "state": "CHANGES_REQUESTED",
        "body": None,
        "submittedAt": "2026-01-01T09:00:00Z",
        "lastEditedAt": "2026-01-02T09:00:00Z",
    }
    pending = {**review, "databaseId": 6, "author": None, "state": "PENDING", "submittedAt": None}
    seen: list[object] = []
    reviews = _reviews_client([[review], [{**pending, "lastEditedAt": None}]], seen).reviews(9)
    assert seen == [None, "1"]
    assert [(r.id, r.author, r.association, r.state, r.body) for r in reviews] == [
        (5, "rev", "OWNER", "CHANGES_REQUESTED", ""),
        (6, "ghost", "OWNER", "PENDING", ""),
    ]
    assert reviews[0].submitted_at == datetime(2026, 1, 1, 9, tzinfo=UTC)
    assert reviews[0].edited_at == datetime(2026, 1, 2, 9, tzinfo=UTC)
    assert reviews[1].submitted_at is None and reviews[1].edited_at is None


def test_reviews_refuse_to_drop_reviews_past_the_page_bound() -> None:
    endless: list[list[dict[str, Any]]] = [[]] * (github.MAX_THREAD_PAGES + 1)
    with pytest.raises(GitHubError, match="more than"):
        _reviews_client(endless, []).reviews(9)


def test_a_reply_posts_to_the_comment_and_a_refusal_raises() -> None:
    sent: list[tuple[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/99/replies"):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(201, json={})

    gh = GitHubRest("o/r", "tok", transport=httpx.MockTransport(handle))
    gh.reply_to_review_comment(9, 5, "done")
    assert sent == [("/repos/o/r/pulls/9/comments/5/replies", {"body": "done"})]
    with pytest.raises(GitHubError, match="HTTP 404") as raised:
        gh.reply_to_review_comment(9, 99, "x")
    assert raised.value.status == 404


def test_a_pull_label_time_comes_from_the_same_issue_events() -> None:
    assert client(FakeGitHubServer()).label_applied_at(7, "ai-spec") is not None


def test_a_graphql_bot_login_is_spelled_as_rest_spells_it() -> None:
    thread = _thread(1)
    nodes: Any = thread["comments"]
    app = {"__typename": "Bot", "login": "specster-endika"}
    person = {"__typename": "User", "login": "specster-endika"}
    nodes["nodes"] = [nodes["nodes"][0] | {"author": a} for a in (app, person)]
    nodes["totalCount"] = 2
    comments = _threads_client([[thread]]).review_threads(9)[0].comments
    assert [c.author for c in comments] == ["specster-endika[bot]", "specster-endika"]
    review = {
        "databaseId": 5,
        "author": app,
        "authorAssociation": "NONE",
        "state": "COMMENTED",
        "body": "b",
        "submittedAt": None,
        "lastEditedAt": None,
    }
    assert _reviews_client([[review]], []).reviews(9)[0].author == "specster-endika[bot]"
