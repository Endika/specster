from collections.abc import Mapping
from pathlib import Path

from specster.git import Git

EVIDENCE_BRANCH = "specster-evidence"
_FETCHED = "refs/specster/evidence"


class EvidenceBranchError(Exception):
    pass


def _check(paths: list[str]) -> None:
    for path in paths:
        if path.startswith(".github/") or ".." in path:
            raise EvidenceBranchError(f"refusing to write {path!r} to {EVIDENCE_BRANCH}")


def _update(
    git: Git,
    url: str,
    token: str,
    folder: str,
    files: Mapping[str, bytes],
    message: str,
    scratch: Path,
    *,
    removing: bool,
) -> str | None:
    """Rewrite `folder` on the branch tip under a lease; None when a removal changes nothing."""
    scratch.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        head = git.remote_head(url, EVIDENCE_BRANCH, token)
        if head is None and removing:
            return None
        if head is not None:
            git.fetch_ref(url, EVIDENCE_BRANCH, token, _FETCHED)
        sha = git.commit_tree_files(head, files, f"{folder}/", message, scratch / "evidence.index")
        if removing and head is not None and _tree(git, sha) == _tree(git, head):
            return None
        if git.push_update(url, sha, EVIDENCE_BRANCH, token, head):
            return sha
    raise EvidenceBranchError(f"{EVIDENCE_BRANCH} moved twice while publishing")


def _tree(git: Git, commit: str) -> str:
    return git.run("rev-parse", "--end-of-options", f"{commit}^{{tree}}").strip()


def publish(
    git: Git, url: str, token: str, folder: str, files: Mapping[str, bytes], scratch: Path
) -> str:
    """Replace `folder` on the evidence branch with `files`; returns the published sha."""
    placed = {f"{folder}/{name}": data for name, data in files.items()}
    _check([f"{folder}/", *placed])
    message = f"evidence: {folder} [skip ci]"
    sha = _update(git, url, token, folder, placed, message, scratch, removing=False)
    assert sha is not None
    return sha


def remove(git: Git, url: str, token: str, folder: str, scratch: Path) -> bool:
    """Drop `folder` from the evidence branch; False when there was nothing to drop."""
    _check([f"{folder}/"])
    message = f"evidence: remove {folder} [skip ci]"
    return _update(git, url, token, folder, {}, message, scratch, removing=True) is not None
