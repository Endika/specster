import json
from datetime import UTC, datetime

import httpx
import pytest

from specster.github import GitHubError, GitHubRest


class FakeGitHubServer:
    def __init__(self) -> None:
        self.labels_on_issue = {"ai-spec"}
        self.repo_labels = {"ai-spec"}
        self.comments: list[str] = []
        self.branches = {"specster/issue-3"}
        self.pulls: list[dict[str, object]] = []
        self.pull_refused: set[str] = set()

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


def test_default_branch_and_branch_existence() -> None:
    gh = client(FakeGitHubServer())
    assert gh.default_branch() == "trunk"
    assert gh.branch_exists("specster/issue-3") and not gh.branch_exists("specster/issue-7")


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
