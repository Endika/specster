from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    author: str
    author_association: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Comment:
    id: int
    author: str
    author_type: str
    association: str
    body: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


@dataclass(frozen=True)
class PullInfo:
    number: int
    state: str
    draft: bool
    title: str
    body: str
    author: str
    author_association: str
    head_sha: str
    head_ref: str
    head_repo: str
    base_sha: str
    base_ref: str
    base_repo: str


@dataclass(frozen=True)
class PullFile:
    path: str
    status: str
    additions: int
    deletions: int
    patch: str


@dataclass(frozen=True)
class PullFiles:
    files: tuple[PullFile, ...]
    truncated: bool


@dataclass(frozen=True)
class ReviewComment:
    id: int
    author: str
    association: str
    body: str
    path: str
    line: int | None
    original_line: int | None
    diff_hunk: str
    created_at: datetime
    edited_at: datetime | None


@dataclass(frozen=True)
class ReviewThread:
    is_resolved: bool
    comments: tuple[ReviewComment, ...]


@dataclass(frozen=True)
class Review:
    id: int
    author: str
    association: str
    state: str
    body: str
    submitted_at: datetime | None
    edited_at: datetime | None = None


MAX_PULL_FILES = 300
MAX_PULL_PATCH_CHARS = 200_000
MAX_THREAD_PAGES = 10
THREADS_PER_PAGE = 100
COMMENTS_PER_THREAD = 100
GHOST = "ghost"


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class IssueTracker(Protocol):
    def get_issue(self, number: int) -> Issue: ...
    def list_comments(self, number: int) -> list[Comment]: ...
    def label_applied_at(self, number: int, label: str) -> datetime | None: ...
    def body_edited_at(self, number: int) -> datetime | None: ...
    def ensure_labels(self, labels: Mapping[str, str]) -> None: ...
    def add_labels(self, number: int, labels: Sequence[str]) -> None: ...
    def remove_label(self, number: int, label: str) -> None: ...
    def post_comment(self, number: int, body: str) -> None: ...
    def own_login(self) -> str | None: ...
    def user_id(self, login: str) -> int | None: ...
    def default_branch(self) -> str: ...
    def branch_exists(self, branch: str) -> bool: ...
    def create_pull(self, title: str, body: str, head: str, base: str) -> PullRequest: ...
    def update_pull(self, number: int, body: str) -> None: ...


class PullTracker(IssueTracker, Protocol):
    def get_pull(self, number: int) -> PullInfo: ...
    def pull_files(self, number: int) -> PullFiles: ...
    def review_threads(self, number: int) -> list[ReviewThread]: ...
    def reviews(self, number: int) -> list[Review]: ...
    def reply_to_review_comment(self, number: int, comment_id: int, body: str) -> None: ...
    def branch_protected(self, branch: str) -> bool: ...
    def branch_ruleset_blocks_pushes(self, branch: str) -> bool: ...


_PUSH_BLOCKING_RULES = frozenset({"pull_request", "update"})
_MAX_RULE_PAGES = 10


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubRest:
    def __init__(
        self,
        repo: str,
        token: str,
        api_url: str = "https://api.github.com",
        graphql_url: str = "https://api.github.com/graphql",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._repo = repo
        self._graphql_url = graphql_url
        self._http = httpx.Client(
            base_url=api_url,
            transport=transport,
            timeout=30,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _pages(self, path: str) -> Iterator[Any]:
        url: str | None = f"/repos/{self._repo}{path}?per_page=100"
        while url:
            resp = self._http.get(url)
            resp.raise_for_status()
            yield from resp.json()
            url = resp.links.get("next", {}).get("url")

    def get_issue(self, number: int) -> Issue:
        resp = self._http.get(f"/repos/{self._repo}/issues/{number}")
        resp.raise_for_status()
        d = resp.json()
        return Issue(
            number=d["number"],
            title=d["title"],
            body=d.get("body") or "",
            author=d["user"]["login"],
            author_association=d.get("author_association", "NONE"),
            labels=tuple(label["name"] for label in d.get("labels", [])),
        )

    def list_comments(self, number: int) -> list[Comment]:
        return [
            Comment(
                id=c["id"],
                author=c["user"]["login"],
                author_type=c["user"].get("type", "User"),
                association=c.get("author_association", "NONE"),
                body=c.get("body") or "",
                created_at=_ts(c["created_at"]),
                updated_at=_ts(c["updated_at"]),
            )
            for c in self._pages(f"/issues/{number}/comments")
        ]

    def label_applied_at(self, number: int, label: str) -> datetime | None:
        times = [
            _ts(e["created_at"])
            for e in self._pages(f"/issues/{number}/events")
            if e.get("event") == "labeled" and e.get("label", {}).get("name") == label
        ]
        return max(times, default=None)

    def body_edited_at(self, number: int) -> datetime | None:
        owner, name = self._repo.split("/", 1)
        query = (
            "query($o:String!,$r:String!,$n:Int!)"
            "{repository(owner:$o,name:$r){issue(number:$n){lastEditedAt}}}"
        )
        resp = self._http.post(
            self._graphql_url,
            json={"query": query, "variables": {"o": owner, "r": name, "n": number}},
        )
        resp.raise_for_status()
        edited = resp.json()["data"]["repository"]["issue"]["lastEditedAt"]
        return _ts(edited) if edited else None

    def ensure_labels(self, labels: Mapping[str, str]) -> None:
        for name, color in labels.items():
            resp = self._http.get(f"/repos/{self._repo}/labels/{quote(name, safe='')}")
            if resp.status_code == 404:
                self._http.post(
                    f"/repos/{self._repo}/labels", json={"name": name, "color": color}
                ).raise_for_status()
            else:
                resp.raise_for_status()

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        self._http.post(
            f"/repos/{self._repo}/issues/{number}/labels", json={"labels": list(labels)}
        ).raise_for_status()

    def remove_label(self, number: int, label: str) -> None:
        resp = self._http.delete(
            f"/repos/{self._repo}/issues/{number}/labels/{quote(label, safe='')}"
        )
        if resp.status_code != 404:
            resp.raise_for_status()

    def post_comment(self, number: int, body: str) -> None:
        self._http.post(
            f"/repos/{self._repo}/issues/{number}/comments", json={"body": body}
        ).raise_for_status()

    def own_login(self) -> str | None:
        try:
            resp = self._http.post(
                self._graphql_url, json={"query": "query { viewer { login __typename } }"}
            )
            resp.raise_for_status()
            viewer = resp.json()["data"]["viewer"]
            login = str(viewer["login"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        # GraphQL names an App "name"; its comments, over REST, are by "name[bot]".
        if viewer.get("__typename") == "Bot" and not login.endswith("[bot]"):
            login += "[bot]"
        return login

    def user_id(self, login: str) -> int | None:
        try:
            resp = self._http.get(f"/users/{quote(login, safe='')}")
            resp.raise_for_status()
            return int(resp.json()["id"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    def default_branch(self) -> str:
        resp = self._http.get(f"/repos/{self._repo}")
        resp.raise_for_status()
        return str(resp.json()["default_branch"])

    def branch_exists(self, branch: str) -> bool:
        resp = self._http.get(f"/repos/{self._repo}/git/ref/heads/{quote(branch, safe='/')}")
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        raise GitHubError(f"check branch {branch}: HTTP {resp.status_code}")

    def branch_protected(self, branch: str) -> bool:
        resp = self._http.get(f"/repos/{self._repo}/branches/{quote(branch, safe='/')}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        protected = resp.json().get("protected")
        if not isinstance(protected, bool):
            raise GitHubError(f"branch {branch}: the response has no `protected` flag")
        return protected

    def branch_ruleset_blocks_pushes(self, branch: str) -> bool:
        path = f"/repos/{self._repo}/rules/branches/{quote(branch, safe='/')}"
        url = f"{path}?per_page=100"
        for _ in range(_MAX_RULE_PAGES):
            resp = self._http.get(url)
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            rules = resp.json()
            if not isinstance(rules, list):
                raise GitHubError(f"rules of {branch}: unexpected response")
            if any(isinstance(r, dict) and r.get("type") in _PUSH_BLOCKING_RULES for r in rules):
                return True
            nxt = resp.links.get("next", {}).get("url")
            if not nxt:
                return False
            url = nxt
        raise GitHubError(f"rules of {branch}: more than {_MAX_RULE_PAGES} pages")

    def create_pull(self, title: str, body: str, head: str, base: str) -> PullRequest:
        resp = self._http.post(
            f"/repos/{self._repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "maintainer_can_modify": False,
            },
        )
        if resp.status_code == 201:
            try:
                d = resp.json()
                return PullRequest(int(d["number"]), str(d["html_url"]))
            except (ValueError, KeyError, TypeError) as e:
                raise GitHubError(
                    f"create pull request: HTTP 201 without a number and url: {e!r}"
                ) from e
        raise GitHubError(
            f"create pull request: HTTP {resp.status_code}: {_error_message(resp)}",
            resp.status_code,
        )

    def update_pull(self, number: int, body: str) -> None:
        try:
            resp = self._http.patch(f"/repos/{self._repo}/pulls/{number}", json={"body": body})
        except httpx.TransportError as e:
            raise GitHubError(f"update pull request: {type(e).__name__}: {e}") from e
        if not resp.is_success:
            raise GitHubError(
                f"update pull request: HTTP {resp.status_code}: {_error_message(resp)}",
                resp.status_code,
            )

    def get_pull(self, number: int) -> PullInfo:
        resp = self._http.get(f"/repos/{self._repo}/pulls/{number}")
        resp.raise_for_status()
        d = resp.json()
        head, base = d["head"], d["base"]
        return PullInfo(
            number=d["number"],
            state=d["state"],
            draft=bool(d.get("draft")),
            title=d["title"],
            body=d.get("body") or "",
            author=_login(d.get("user")),
            author_association=d.get("author_association", "NONE"),
            head_sha=head["sha"],
            head_ref=head["ref"],
            head_repo=(head.get("repo") or {}).get("full_name", ""),
            base_sha=base["sha"],
            base_ref=base["ref"],
            base_repo=(base.get("repo") or {}).get("full_name", ""),
        )

    def pull_files(self, number: int) -> PullFiles:
        files: list[PullFile] = []
        chars = 0
        for f in self._pages(f"/pulls/{number}/files"):
            patch = f.get("patch") or ""
            if len(files) >= MAX_PULL_FILES or chars + len(patch) > MAX_PULL_PATCH_CHARS:
                return PullFiles(tuple(files), truncated=True)
            chars += len(patch)
            files.append(
                PullFile(
                    path=f["filename"],
                    status=f["status"],
                    additions=f["additions"],
                    deletions=f["deletions"],
                    patch=patch,
                )
            )
        return PullFiles(tuple(files), truncated=False)

    def review_threads(self, number: int) -> list[ReviewThread]:
        owner, name = self._repo.split("/", 1)
        query = (
            "query($o:String!,$r:String!,$n:Int!,$c:String)"
            "{repository(owner:$o,name:$r){pullRequest(number:$n){"
            f"reviewThreads(first:{THREADS_PER_PAGE},after:$c)"
            "{pageInfo{hasNextPage endCursor} nodes{isResolved "
            f"comments(first:{COMMENTS_PER_THREAD})"
            "{totalCount nodes{databaseId author{__typename login} authorAssociation body path "
            "line originalLine diffHunk createdAt lastEditedAt}}}}}}}"
        )
        threads: list[ReviewThread] = []
        cursor: str | None = None
        for _ in range(MAX_THREAD_PAGES):
            resp = self._http.post(
                self._graphql_url,
                json={
                    "query": query,
                    "variables": {"o": owner, "r": name, "n": number, "c": cursor},
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                raise GitHubError(f"review threads: {payload['errors'][0].get('message', '')}")
            pull = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest")
            if pull is None:
                raise GitHubError(f"review threads: no pull request #{number}", 404)
            page = pull["reviewThreads"]
            for t in page["nodes"]:
                if t["comments"]["totalCount"] > COMMENTS_PER_THREAD:
                    raise GitHubError(
                        f"review threads: a thread has more than {COMMENTS_PER_THREAD} comments"
                    )
                threads.append(
                    ReviewThread(
                        is_resolved=bool(t["isResolved"]),
                        comments=tuple(_review_comment(c) for c in t["comments"]["nodes"]),
                    )
                )
            if not page["pageInfo"]["hasNextPage"]:
                return threads
            cursor = page["pageInfo"]["endCursor"]
        raise GitHubError(
            f"review threads: more than {MAX_THREAD_PAGES * THREADS_PER_PAGE} threads"
        )

    def reviews(self, number: int) -> list[Review]:
        # GraphQL, since only it says when a review's text was last edited.
        owner, name = self._repo.split("/", 1)
        query = (
            "query($o:String!,$r:String!,$n:Int!,$c:String)"
            "{repository(owner:$o,name:$r){pullRequest(number:$n){"
            f"reviews(first:{THREADS_PER_PAGE},after:$c)"
            "{pageInfo{hasNextPage endCursor} nodes{databaseId author{__typename login} "
            "authorAssociation state body submittedAt lastEditedAt}}}}}"
        )
        reviews: list[Review] = []
        cursor: str | None = None
        for _ in range(MAX_THREAD_PAGES):
            resp = self._http.post(
                self._graphql_url,
                json={
                    "query": query,
                    "variables": {"o": owner, "r": name, "n": number, "c": cursor},
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                raise GitHubError(f"reviews: {payload['errors'][0].get('message', '')}")
            pull = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest")
            if pull is None:
                raise GitHubError(f"reviews: no pull request #{number}", 404)
            page = pull["reviews"]
            reviews += [
                Review(
                    id=r["databaseId"],
                    author=_graphql_login(r.get("author")),
                    association=r.get("authorAssociation", "NONE"),
                    state=r["state"],
                    body=r.get("body") or "",
                    submitted_at=_ts(r["submittedAt"]) if r.get("submittedAt") else None,
                    edited_at=_ts(r["lastEditedAt"]) if r.get("lastEditedAt") else None,
                )
                for r in page["nodes"]
            ]
            if not page["pageInfo"]["hasNextPage"]:
                return reviews
            cursor = page["pageInfo"]["endCursor"]
        raise GitHubError(f"reviews: more than {MAX_THREAD_PAGES * THREADS_PER_PAGE} reviews")

    def reply_to_review_comment(self, number: int, comment_id: int, body: str) -> None:
        try:
            resp = self._http.post(
                f"/repos/{self._repo}/pulls/{number}/comments/{comment_id}/replies",
                json={"body": body},
            )
        except httpx.TransportError as e:
            raise GitHubError(f"reply to review comment: {type(e).__name__}: {e}") from e
        if not resp.is_success:
            raise GitHubError(
                f"reply to review comment: HTTP {resp.status_code}: {_error_message(resp)}",
                resp.status_code,
            )


def _login(user: Mapping[str, Any] | None) -> str:
    return str(user["login"]) if user else GHOST


def _graphql_login(actor: Mapping[str, Any] | None) -> str:
    """A GraphQL author's login as REST spells it: a GitHub App's bot carries `[bot]` there."""
    login = _login(actor)
    if actor and actor.get("__typename") == "Bot" and not login.endswith("[bot]"):
        return f"{login}[bot]"
    return login


def _review_comment(c: Mapping[str, Any]) -> ReviewComment:
    return ReviewComment(
        id=c["databaseId"],
        author=_graphql_login(c.get("author")),
        association=c.get("authorAssociation", "NONE"),
        body=c.get("body") or "",
        path=c.get("path") or "",
        line=c.get("line"),
        original_line=c.get("originalLine"),
        diff_hunk=c.get("diffHunk") or "",
        created_at=_ts(c["createdAt"]),
        edited_at=_ts(c["lastEditedAt"]) if c.get("lastEditedAt") else None,
    )


def _error_message(resp: httpx.Response) -> str:
    try:
        d = resp.json()
        message = str(d.get("message", ""))
        errors = d.get("errors") or []
        if errors and isinstance(errors[0], dict) and errors[0].get("message"):
            message += f" ({errors[0]['message']})"
    except (ValueError, AttributeError):
        return resp.text[:500]
    return message
