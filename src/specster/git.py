import base64
import contextlib
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

BOT_EMAIL = "specster@users.noreply.github.com"
_TAIL = 2000
DEFAULT_TIMEOUT_S = 300.0
PUSH_TIMEOUT_S = 600.0
_BASE = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "maintenance.auto=false",
    "-c",
    "gc.auto=0",
)
_CREDENTIAL_KEYS = re.compile(
    r"^(http\..*extraheader|credential\..*|include\.path|includeif\..*\.path)$", re.IGNORECASE
)
_INCLUDE_KEYS = re.compile(r"^(include\.path|includeif\..*\.path)$", re.IGNORECASE)
_URL_VALUE_KEYS = re.compile(
    r"^(remote\..*\.(url|pushurl|proxy)|submodule\..*\.url|http\.(.*\.)?proxy"
    r"|url\..*\.(insteadof|pushinsteadof))$",
    re.IGNORECASE,
)
_REWRITE_KEYS = re.compile(r"^url\.(.*)\.(insteadof|pushinsteadof)$", re.IGNORECASE)
_URL = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)?([^/?#]*)(.*)$", re.DOTALL)


class GitError(Exception):
    pass


def _subcommand(args: Sequence[str]) -> str:
    i = 0
    while i + 1 < len(args) and args[i] == "-c":
        i += 2
    return args[i] if i < len(args) else "?"


@dataclass(frozen=True)
class Author:
    name: str
    email: str


def _without_userinfo(value: str, bare_host: bool = False) -> str:
    """A scheme-less value is stripped only when it is a bare host (a proxy)."""
    m = _URL.match(value)
    if not m or not (m.group(1) or bare_host):
        return value
    return f"{m.group(1) or ''}{m.group(2).rpartition('@')[2]}{m.group(3)}"


def push_invocation(url: str, branch: str, token: str) -> tuple[list[str], dict[str, str]]:
    """The push's argv (after `git` and the base options) and the env that carries the token."""
    header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    parts = urlsplit(url)
    extra: dict[str, str] = {}
    if parts.scheme in ("http", "https"):
        host = parts.netloc.rpartition("@")[2]
        extra = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{parts.scheme}://{host}/.extraheader",
            "GIT_CONFIG_VALUE_0": header,
        }
    ref = f"refs/heads/{branch}"
    # An empty expected value makes the lease "the ref must not exist": create, never update.
    args = [
        "-c",
        "credential.helper=",
        "-c",
        "http.lowSpeedLimit=1000",
        "-c",
        "http.lowSpeedTime=60",
        "push",
        "--porcelain",
        f"--force-with-lease={ref}:",
        url,
        f"{ref}:{ref}",
    ]
    return args, extra


class Git:
    def __init__(self, repo: Path, author: Author, home: Path, *, log_argv: bool = False) -> None:
        self.repo, self.author, self.home = repo, author, home
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Every argv run, for tests only: a build runs thousands of git commands.
        self.argv_log: list[list[str]] = []
        self._log_argv = log_argv
        self._trusted: dict[Path, None] = {repo.resolve(): None}

    def trust(self, path: Path) -> None:
        self._trusted[path.resolve()] = None

    def argv(self, *args: str) -> list[str]:
        safe = [a for p in self._trusted for a in ("-c", f"safe.directory={p}")]
        return ["git", *_BASE, *safe, *args]

    def _env(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            # Paths are file names chosen by the model, never globs or pathspec magic.
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_AUTHOR_NAME": self.author.name,
            "GIT_AUTHOR_EMAIL": self.author.email,
            "GIT_COMMITTER_NAME": self.author.name,
            "GIT_COMMITTER_EMAIL": self.author.email,
        }
        return env | dict(extra or {})

    def _exec(
        self,
        args: Sequence[str],
        cwd: Path | None,
        extra_env: Mapping[str, str] | None,
        secrets: Sequence[str],
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        argv = self.argv(*args)
        if any(s and s in a for s in secrets for a in argv):
            raise GitError(f"git {_subcommand(args)} refused: a secret in argv")
        if self._log_argv:
            self.argv_log.append(argv)
        # A session of its own, so a timeout kills git's children too (remote helpers, editors).
        with subprocess.Popen(
            argv,
            cwd=cwd or self.repo,
            env=self._env(extra_env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        ) as proc:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # The group may have exited on its own since the timeout fired.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise GitError(f"git {_subcommand(args)} timed out after {timeout} s") from None
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def _fail(
        self, args: Sequence[str], proc: subprocess.CompletedProcess[str], secrets: Sequence[str]
    ) -> GitError:
        text = (proc.stderr or proc.stdout).strip()
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        if len(text) > _TAIL:
            text = f"[{len(text) - _TAIL} earlier chars cut] {text[-_TAIL:]}"
        return GitError(f"git {_subcommand(args)} failed ({proc.returncode}): {text}")

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        extra_env: Mapping[str, str] | None = None,
        secrets: Sequence[str] = (),
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> str:
        proc = self._exec(args, cwd, extra_env, secrets, timeout)
        if proc.returncode != 0:
            raise self._fail(args, proc, secrets)
        return proc.stdout

    def head(self, tree: Path | None = None) -> str:
        return self.run("rev-parse", "HEAD", cwd=tree).strip()

    def add_worktree(self, path: Path, commit: str, branch: str | None = None) -> None:
        mode = ["-b", branch] if branch else ["--detach"]
        self.trust(path)
        self.run("worktree", "add", "-q", *mode, "--end-of-options", str(path), commit)

    def drop_worktree(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
        self._trusted.pop(path.resolve(), None)
        self.run("worktree", "prune")

    def commit_paths(self, tree: Path, paths: Sequence[str], subject: str) -> str | None:
        self.run("add", "--", *paths, cwd=tree)
        args = ("diff", "--cached", "--quiet")
        proc = self._exec(args, tree, None, ())
        if proc.returncode == 0:
            return None
        if proc.returncode != 1:
            raise self._fail(args, proc, ())
        self.run("commit", "-q", "--no-verify", "-m", subject, cwd=tree)
        return self.head(tree)

    def diff(self, base: str, head: str) -> str:
        return self.run(
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            "--end-of-options",
            base,
            head,
        )

    def strip_credentials(self) -> list[str]:
        git_dir = self.repo / ".git"
        report: list[str] = []
        seen: set[Path] = set()
        configs = [git_dir / "config", git_dir / "config.worktree"]
        for cfg in [*configs, *sorted(git_dir.glob("worktrees/*/config.worktree"))]:
            if cfg.is_file():
                self._strip_config(cfg, "" if cfg == configs[0] else f" in {cfg}", report, seen)
        return report

    def _strip_config(self, cfg: Path, where: str, report: list[str], seen: set[Path]) -> None:
        seen.add(cfg.resolve())
        cfg.chmod(0o600)
        base = ("config", "--file", str(cfg), "--no-includes")
        listing = self._exec((*base, "--name-only", "--list"), None, None, ())
        if listing.returncode != 0:
            report.append(f"skipped config {cfg}: {listing.stderr.strip()[:200]}")
            return

        def values(key: str) -> list[str]:
            out = self._exec((*base, "--get-all", key), None, None, ())
            return out.stdout.splitlines() if out.returncode == 0 else []

        for key in dict.fromkeys(listing.stdout.splitlines()):
            if _INCLUDE_KEYS.match(key):
                for value in values(key):
                    self._strip_include(cfg, f"{key}{where}", value, report, seen)
            rewrite = _REWRITE_KEYS.match(key)
            named = rewrite is not None and _without_userinfo(rewrite.group(1)) != rewrite.group(1)
            if _CREDENTIAL_KEYS.match(key) or named:
                self.run("config", "--file", str(cfg), "--unset-all", key)
                report.append(f"{key}{where}")
            elif _URL_VALUE_KEYS.match(key):
                old = values(key)
                new = [_without_userinfo(v, key.lower().endswith(".proxy")) for v in old]
                if new != old:
                    self.run("config", "--file", str(cfg), "--unset-all", key)
                    for value in new:
                        self.run("config", "--file", str(cfg), "--add", key, value)
                    report.append(f"{key}: userinfo removed{where}")
        cfg.chmod(0o600)

    def _strip_include(
        self, owner: Path, key: str, value: str, report: list[str], seen: set[Path]
    ) -> None:
        skipped = f"skipped include {value} ({key})"
        if value.startswith("~"):
            report.append(f"{skipped}: home-relative, not resolved")
            return
        target = Path(value) if Path(value).is_absolute() else owner.parent / value
        if target.is_symlink():
            report.append(f"{skipped}: a symlink, not followed")
        elif not target.is_file():
            report.append(f"{skipped}: no such file")
        elif target.resolve() not in seen:
            report.append(f"locked {target}")
            self._strip_config(target, f" in {target}", report, seen)

    def push(self, url: str, branch: str, token: str) -> None:
        args, extra = push_invocation(url, branch, token)
        header = extra.get("GIT_CONFIG_VALUE_0", "")
        secrets = (token, header, header.rpartition(" ")[2])
        self.run(*args, extra_env=extra, secrets=secrets, timeout=PUSH_TIMEOUT_S)
