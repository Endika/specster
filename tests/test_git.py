import base64
import os
import stat
import time
from pathlib import Path

import pytest

from specster.git import BOT_EMAIL, Author, Git, GitError, push_invocation
from tests.fakes import make_remote, make_repo

TOKEN = "ghs_SECRETTOKEN"


def test_commits_never_run_the_repo_hooks_and_only_touch_the_given_paths(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n", "other.py": "y = 1\n"})
    tree = tmp_path / "wt"
    git.add_worktree(tree, git.head(), branch="specster/issue-7")
    (tree / "app.py").write_text("x = 2\n")
    (tree / "other.py").write_text("y = 2\n")
    sha = git.commit_paths(tree, ["app.py"], "feat(a): bump x")
    assert (
        sha and not (tmp_path / "repo" / "HOOK_RAN").exists() and not (tree / "HOOK_RAN").exists()
    )
    assert "app.py" in git.diff(git.head(), sha) and "other.py" not in git.diff(git.head(), sha)
    assert git.commit_paths(tree, ["app.py"], "feat(a): again") is None


def test_a_dropped_worktree_is_pruned_from_the_main_repo(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    tree = tmp_path / "w1"
    git.add_worktree(tree, git.head())
    git.drop_worktree(tree)
    assert not tree.exists() and str(tree) not in git.run("worktree", "list")


def test_credentials_are_stripped_and_the_config_locked(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    creds = tmp_path / "creds.config"
    creds.write_text(f"[http]\n\textraheader = AUTHORIZATION: basic {TOKEN}\n")
    creds.chmod(0o644)
    git.run("config", "http.https://github.com/.extraheader", f"AUTHORIZATION: basic {TOKEN}")
    git.run("config", "credential.helper", "store")
    git.run("config", f"includeIf.gitdir:{tmp_path}/repo/.git.path", str(creds))
    git.run("remote", "add", "origin", f"https://x-access-token:{TOKEN}@github.com/o/r.git")
    removed = git.strip_credentials()
    config = tmp_path / "repo" / ".git" / "config"
    assert TOKEN not in config.read_text() and "credential.helper" in removed
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert git.run("remote", "get-url", "origin").strip() == "https://github.com/o/r.git"


def test_push_creates_the_branch_and_the_token_is_never_in_argv(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    remote = make_remote(tmp_path / "remote.git")
    git.run("branch", "specster/issue-7")
    git.push(str(remote), "specster/issue-7", TOKEN)
    assert git.run("ls-remote", str(remote)).count("refs/heads/specster/issue-7") == 1
    assert all(TOKEN not in arg for argv in git.argv_log for arg in argv)


def test_a_failed_push_does_not_show_the_token(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    git.run("branch", "b")
    header = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    # An out-of-range port fails inside curl, with the header env set, before any socket.
    with pytest.raises(GitError, match="push failed") as e:
        git.push("http://127.0.0.1:99999/o/r.git", "b", TOKEN)
    assert TOKEN not in str(e.value) and header not in str(e.value)


def test_push_never_overwrites_an_existing_remote_branch(tmp_path: Path) -> None:
    remote = make_remote(tmp_path / "remote.git")
    other = make_repo(tmp_path / "other", {"z.py": "z = 1\n"})
    other.run("branch", "b")
    other.push(str(remote), "b", TOKEN)
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    git.run("branch", "b")
    with pytest.raises(GitError, match="push failed"):
        git.push(str(remote), "b", TOKEN)


def test_push_does_not_even_fast_forward_an_existing_remote_branch(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    remote = make_remote(tmp_path / "remote.git")
    git.run("branch", "b")
    git.push(str(remote), "b", TOKEN)
    before = git.run("ls-remote", str(remote), "refs/heads/b")
    tree = tmp_path / "wt"
    git.add_worktree(tree, "b")
    (tree / "app.py").write_text("x = 2\n")
    git.run("update-ref", "refs/heads/b", str(git.commit_paths(tree, ["app.py"], "feat(a): x")))
    with pytest.raises(GitError, match="push failed"):
        git.push(str(remote), "b", TOKEN)
    assert git.run("ls-remote", str(remote), "refs/heads/b") == before


def test_the_token_reaches_the_push_only_as_an_env_header() -> None:
    args, env = push_invocation("https://github.com/o/r.git", "specster/issue-7", TOKEN)
    header = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert all(TOKEN not in a and header not in a for a in args)
    assert env == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {header}",
    }


def test_a_secret_is_refused_in_argv_and_scrubbed_from_errors(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    with pytest.raises(GitError, match="secret in argv"):
        git.push(f"https://x-access-token:{TOKEN}@github.com/o/r.git", "main", TOKEN)
    with pytest.raises(GitError) as e:
        git.run("status", extra_env={"GIT_DIR": f"/nonexistent/{TOKEN}"}, secrets=(TOKEN,))
    assert "/nonexistent/***" in str(e.value) and TOKEN not in str(e.value)
    assert not any(TOKEN in a for argv in git.argv_log for a in argv)


@pytest.mark.parametrize("name", ["*.py", ":a.py"])
def test_commit_paths_are_literal_names_not_pathspecs(tmp_path: Path, name: str) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n", "b.py": "b\n", name: "n\n"})
    tree = tmp_path / "wt"
    git.add_worktree(tree, git.head())
    for f in ("a.py", "b.py", name):
        (tree / f).write_text("changed\n")
    sha = git.commit_paths(tree, [name], "feat(a): one file")
    assert git.run("diff", "--name-only", git.head(), str(sha)).splitlines() == [name]


def test_only_the_repo_and_trusted_trees_are_safe_directories(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n"})
    argv = git.argv("status")
    assert "safe.directory=*" not in argv
    assert [a for a in argv if a.startswith("safe.directory=")] == [
        f"safe.directory={(tmp_path / 'repo').resolve()}"
    ]
    git.trust(tmp_path / "wt")
    assert f"safe.directory={(tmp_path / 'wt').resolve()}" in git.argv("status")


def test_a_repo_owned_by_someone_else_fails_closed_unless_trusted(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n"})
    stranger = make_repo(tmp_path / "stranger", {"s.py": "s\n"})
    # git's own switch that makes every directory look owned by another uid.
    other_owner = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    assert git.run("status", "--porcelain", extra_env=other_owner) == ""
    with pytest.raises(GitError, match="dubious ownership"):
        git.run("status", cwd=stranger.repo, extra_env=other_owner)


def test_a_hung_git_times_out_as_a_git_error_and_takes_its_children_down(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n"})
    started = time.monotonic()
    with pytest.raises(GitError, match=r"git commit timed out after 0\.5 s"):
        git.run("commit", "--allow-empty", extra_env={"GIT_EDITOR": "sleep 30 #"}, timeout=0.5)
    assert time.monotonic() - started < 5


def test_a_stalled_push_is_abandoned() -> None:
    args, _ = push_invocation("https://github.com/o/r.git", "b", TOKEN)
    assert "http.lowSpeedLimit=1000" in args and "http.lowSpeedTime=60" in args


def test_every_call_carries_the_hardening_options(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n"})
    argv = git.argv("status")
    for option in (
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "commit.gpgSign=false",
        "protocol.ext.allow=never",
        "maintenance.auto=false",
        "gc.auto=0",
    ):
        assert argv[argv.index(option) - 1] == "-c"


def test_a_revision_that_looks_like_an_option_is_only_a_bad_revision(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n"})
    planted = tmp_path / "planted"
    with pytest.raises(GitError):
        git.diff(f"--output={planted}", git.head())
    assert not planted.exists()
    with pytest.raises(GitError):
        git.add_worktree(tmp_path / "wt", "--no-checkout")


def test_credentials_in_rewrites_submodules_and_proxies_are_removed(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n"})
    userinfo = f"x-access-token:{TOKEN}@"
    git.run("config", "url.https://github.com/.insteadOf", f"https://{userinfo}github.com/")
    git.run("config", "url.https://github.com/.pushInsteadOf", "git@github.com:")
    git.run("config", "submodule.lib.url", f"https://{userinfo}github.com/o/lib.git")
    git.run("config", "http.proxy", f"http://{userinfo}proxy:3128")
    git.run("config", "http.https://github.com/.proxy", f"{userinfo}proxy:3128")
    removed = git.strip_credentials()
    assert TOKEN not in (tmp_path / "repo" / ".git" / "config").read_text()
    assert git.run("config", "url.https://github.com/.insteadOf").strip() == "https://github.com/"
    assert git.run("config", "url.https://github.com/.pushInsteadOf").strip() == "git@github.com:"
    assert git.run("config", "submodule.lib.url").strip() == "https://github.com/o/lib.git"
    assert git.run("config", "http.proxy").strip() == "http://proxy:3128"
    assert git.run("config", "http.https://github.com/.proxy").strip() == "proxy:3128"
    assert "submodule.lib.url: userinfo removed" in removed


def test_worktree_configs_lose_their_extraheader(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n"})
    git.add_worktree(tmp_path / "wt", git.head())
    git_dir = tmp_path / "repo" / ".git"
    configs = [git_dir / "config.worktree", git_dir / "worktrees" / "wt" / "config.worktree"]
    for cfg in configs:
        git.run("config", "--file", str(cfg), "http.https://github.com/.extraheader", TOKEN)
    git.strip_credentials()
    for cfg in configs:
        assert TOKEN not in cfg.read_text() and stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_readable_include_targets_are_stripped_too(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n"})
    inner = tmp_path / "inner.config"
    inner.write_text(f"[credential]\n\thelper = store --file {TOKEN}\n")
    outer = tmp_path / "outer.config"
    outer.write_text(f"[http]\n\textraheader = {TOKEN}\n[include]\n\tpath = inner.config\n")
    git.run("config", "include.path", str(outer))
    removed = git.strip_credentials()
    assert TOKEN not in outer.read_text() and TOKEN not in inner.read_text()
    assert f"locked {outer}" in removed and f"locked {inner}" in removed
    assert stat.S_IMODE(inner.stat().st_mode) == 0o600


def test_includes_it_cannot_strip_are_reported_not_dropped_silently(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"a.py": "a\n"})
    (tmp_path / "real.config").write_text(f"[http]\n\textraheader = {TOKEN}\n")
    (tmp_path / "link.config").symlink_to(tmp_path / "real.config")
    for value in (
        "~/creds.config",
        str(tmp_path / "missing.config"),
        str(tmp_path / "link.config"),
    ):
        git.run("config", "--add", "include.path", value)
    removed = git.strip_credentials()
    skipped = [r for r in removed if r.startswith("skipped include")]
    assert len(skipped) == 3
    assert all(
        any(v in r for r in skipped) for v in ("~/creds.config", "missing.config", "link.config")
    )
    assert "include.path" in removed


def test_argv_is_only_logged_when_asked(tmp_path: Path) -> None:
    git = Git(tmp_path, Author("t", BOT_EMAIL), tmp_path / "home")
    git.run("--version")
    assert git.argv_log == []


def test_a_timed_out_git_whose_group_is_already_gone_still_reports_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text("#!/bin/sh\nexec sleep 30\n")
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    real = os.killpg

    def already_gone(pid: int, sig: int) -> None:
        real(pid, sig)
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", already_gone)
    git = Git(tmp_path, Author("t", BOT_EMAIL), tmp_path / "home")
    with pytest.raises(GitError, match=r"timed out after 0\.2 s"):
        git.run("status", timeout=0.2)
