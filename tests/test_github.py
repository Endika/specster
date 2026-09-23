import json
from datetime import UTC, datetime

import httpx

from specster.github import GitHubRest


class FakeGitHubServer:
    def __init__(self) -> None:
        self.labels_on_issue = {"ai-spec"}
        self.repo_labels = {"ai-spec"}
        self.comments: list[str] = []

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
