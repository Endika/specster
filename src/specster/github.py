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
    def default_branch(self) -> str: ...
    def branch_exists(self, branch: str) -> bool: ...
    def create_pull(self, title: str, body: str, head: str, base: str) -> PullRequest: ...


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
