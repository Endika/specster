import contextlib
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from specster.git import Git

DEFAULT_MAX_FILE_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_PROCS = 512
_REAP_ROUNDS = 100
_REAP_BUDGET_S = 5.0
_REAP_PAUSE_S = 0.005
_DYING_GRACE_S = 2.0
_KILL_SIGNAL = signal.SIGKILL


@dataclass(frozen=True)
class Identity:
    uid: int
    gid: int


SANDBOX_BASE_UID = 61000
FILE_COMMANDS = (
    "GITHUB_ENV",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_STATE",
)
_MAX_SLOT = 8
# Mounted by the runner into a Docker action next to GITHUB_WORKSPACE; runner-owned, so taking
# away "other" access leaves the runner's own steps untouched.
RUNNER_DIRS = (Path("/github/home"), Path("/github/runner_temp"))
DOCKER_SOCKET = Path("/var/run/docker.sock")


def slot_identity(slot: int) -> Identity:
    if not 0 <= slot <= _MAX_SLOT:
        raise ValueError(f"sandbox slot {slot} is outside 0..{_MAX_SLOT}")
    return Identity(SANDBOX_BASE_UID + slot, SANDBOX_BASE_UID + slot)


class SandboxError(Exception):
    """Fatal for the build: the caller aborts and never reuses the slot."""


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    exit_code: int | None
    output: str
    timed_out: bool
    truncation: str | None
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def require_root(identity: Identity | None) -> None:
    if identity is not None and os.geteuid() != 0:
        raise SandboxError(
            "the build phase runs tests as unprivileged sandbox uids and must start as root,"
            " as the Docker action does"
        )


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    return f"{n / 1024:.1f}".removesuffix(".0") + " KB"


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _scan(uid: int) -> dict[int, tuple[str, str]]:
    found: dict[int, tuple[str, str]] = {}
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(
                line.split(":", 1) for line in status.read_text().splitlines() if ":" in line
            )
        except OSError:
            continue
        state = fields.get("State", "").strip()[:1]
        if str(uid) in fields.get("Uid", "").split() and state:
            found[int(status.parent.name)] = (state, fields.get("PPid", "").strip())
    return found


def _slot_states(uid: int) -> dict[int, str]:
    return {pid: state for pid, (state, _) in _scan(uid).items() if state not in "ZX"}


def _collect(uid: int) -> None:
    """Wait for the slot's zombies that are Specster's children (orphans reparent to PID 1).

    A zombie still counts against the uid's RLIMIT_NPROC, so uncollected ones would starve the
    slot's later runs. Only our own zombie children of the slot uid, never waitpid(-1): a pid
    of another uid may be a Popen another thread is waiting on.
    """
    me = str(os.getpid())
    for pid, (state, parent) in _scan(uid).items():
        if state == "Z" and parent == me:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)


def _live_pids(uid: int) -> list[int]:
    return sorted(_slot_states(uid))


def _signal_all(pids: Iterable[int], sig: int) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)


def _reap(identity: Identity) -> None:
    try:
        _kill_all(identity)
    finally:
        _collect(identity.uid)


def _kill_all(identity: Identity) -> None:
    """Stop and kill every process of the slot from root each round; raise if any is left.

    SIGKILL cannot be caught, blocked or undone, so killing every live pid every round beats
    a ring that resumes or forks: once a pid's kill is pending it dies and cannot spawn more.
    SIGSTOP first only quiets a fork storm so the scan converges faster. Kill never waits for
    the stop to take, so a process wedged in D (uninterruptible) can never stall the kill.
    """
    # A per-slot cgroup v2 with cgroup.kill would be atomic, but the action's container has no
    # writable cgroup mount, so the /proc loop below is what runs.
    uid = identity.uid
    live = _live_pids(uid)
    # The budget starts after the first scan: under a saturated uid that scan alone takes seconds.
    deadline = time.monotonic() + _REAP_BUDGET_S
    for _ in range(_REAP_ROUNDS):
        if not live:
            return
        _signal_all(live, signal.SIGSTOP)
        _signal_all(live, _KILL_SIGNAL)
        if time.monotonic() >= deadline:
            break
        time.sleep(_REAP_PAUSE_S)
        live = _live_pids(uid)
    # Killed pids (a D wait included) die on their own; keep killing until the grace bound.
    grace = time.monotonic() + _DYING_GRACE_S
    while time.monotonic() < grace:
        live = _live_pids(uid)
        if not live:
            return
        _signal_all(live, _KILL_SIGNAL)
        time.sleep(_REAP_PAUSE_S)
    left = _slot_states(uid)
    if left:
        raise SandboxError(f"processes of sandbox uid {uid} survived the kill: {left}")


class Sandbox:
    def __init__(
        self,
        identity: Identity | None,
        timeout_s: int,
        output_max_bytes: int,
        extra_env: Mapping[str, str],
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_procs: int = DEFAULT_MAX_PROCS,
        time_left: Callable[[], float] | None = None,
    ) -> None:
        self._identity = identity
        self._time_left = time_left
        self._max_file = max_file_bytes
        self._max_procs = max_procs
        self._locked = False
        self._timeout_s = timeout_s
        self._max = output_max_bytes
        self._extra_env = dict(extra_env)

    @property
    def identity(self) -> Identity | None:
        return self._identity

    def hand_over(self, tree: Path, keep_root: Sequence[str] = ()) -> None:
        """`keep_root` names the top-level entries that stay root's."""
        if self._identity is None:
            return
        uid, gid = self._identity.uid, self._identity.gid
        keep = set(keep_root)
        if tree.is_symlink():
            raise SandboxError(f"refusing to hand over a symlink: {tree}")
        # 0700, so another slot cannot reach into the tree even knowing its path.
        tree.chmod(0o700)
        os.chown(tree, uid, gid, follow_symlinks=False)
        # fwalk works on directory fds, so a swapped-in symlink cannot redirect a chown.
        for top, dirs, files, fd in os.fwalk(tree, follow_symlinks=False):
            if top == str(tree):
                dirs[:] = [d for d in dirs if d not in keep]
                files = [f for f in files if f not in keep]
            for name in [*dirs, *files]:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (st.st_uid, st.st_gid) == (uid, gid):
                    continue
                if not stat.S_ISDIR(st.st_mode) and st.st_nlink > 1:
                    raise SandboxError(f"refusing to hand over a hard link: {top}/{name}")
                os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)

    def new_home(self, parent: Path, name: str) -> Path:
        path = parent / name
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        (path / "tmp").mkdir(mode=0o700)
        self.hand_over(path)
        return path

    def lock_down(
        self,
        workspace: Path,
        git: Git,
        environ: Mapping[str, str],
        runner_dirs: Sequence[Path] = RUNNER_DIRS,
    ) -> list[str]:
        """`run` refuses until this is done."""
        done = lock_down(workspace, git, environ, runner_dirs)
        self._locked = True
        return done

    def _limited(self) -> list[str]:
        limits = [str(self._max_file), str(self._max_procs)]
        return [sys.executable, "-I", "-m", "specster.sandbox_exec", *limits, "--"]

    def run(self, argv: Sequence[str], cwd: Path, home: Path, label: str) -> RunResult:
        if not self._locked:
            raise SandboxError("refusing to run a test command before lock_down of the workspace")
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "TMPDIR": str(home / "tmp"),
        }
        env |= self._extra_env
        ids: dict[str, Any] = {}
        if self._identity is not None:
            ids = {"user": self._identity.uid, "group": self._identity.gid, "extra_groups": []}
        timeout: float = self._timeout_s
        if self._time_left is not None:
            # The build's wall-clock limit shortens the run; a second at least, so it still runs.
            timeout = min(timeout, max(1.0, self._time_left()))
        started = time.monotonic()
        with tempfile.TemporaryFile() as out:
            try:
                proc = subprocess.Popen(
                    [*self._limited(), *argv],
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    **ids,
                )
            except OSError as e:
                return RunResult(tuple(argv), 127, f"{type(e).__name__}: {e}", False, None, 0.0)
            timed_out = False
            code: int | None = None
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                if self._identity is not None:
                    _reap(self._identity)
            total = out.seek(0, os.SEEK_END)
            keep = min(total, self._max)
            out.seek(total - keep)
            data = out.read()
        cut = None
        if total > keep:
            # Drop a multi-byte character split by the cut instead of decoding it into U+FFFD.
            lead = next((i for i, b in enumerate(data[:4]) if b & 0xC0 != 0x80), 0)
            data = data[lead:]
            cut = f"{label}: output cut to the last {_size(self._max)} of {_size(total)}"
        text = data.decode("utf-8", errors="replace")
        if code is not None and code < 0:
            text += f"\n{label}: killed by {_signal_name(-code)}"
            if code == -signal.SIGXFSZ:
                text += f" (a file reached the {_size(self._max_file)} limit)"
            text += "\n"
        duration = round(time.monotonic() - started, 3)
        return RunResult(tuple(argv), code, text, timed_out, cut, duration)


def lock_down(
    workspace: Path,
    git: Git,
    environ: Mapping[str, str],
    runner_dirs: Sequence[Path] = RUNNER_DIRS,
) -> list[str]:
    done = git.strip_credentials()
    done.extend(_close_dir(path) for path in [workspace, *runner_dirs])
    files = sorted(workspace.glob("gha-creds-*.json"))
    google = environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if google:
        files.append(Path(google))
    # A test that could append to these would set env, outputs or PATH for the later steps.
    files += [Path(environ[name]) for name in FILE_COMMANDS if environ.get(name)]
    done.extend(_lock_file(path) for path in files)
    return done


def restore_owner(top: Path, uid: int, gid: int) -> list[str]:
    """Checkout's post step and later steps, as the runner user, must write what root rewrote."""
    moved: list[str] = []
    st = top.lstat()
    if (st.st_uid, st.st_gid) != (uid, gid):
        os.chown(top, uid, gid, follow_symlinks=False)
        moved.append(str(top))
    for folder, dirs, files, fd in os.fwalk(top, follow_symlinks=False):
        for name in [*dirs, *files]:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (st.st_uid, st.st_gid) != (uid, gid):
                os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)
                moved.append(f"{folder}/{name}")
    return moved


def _close_dir(path: Path) -> str:
    """chmod o-rwx without following a symlink: a slot then cannot even enter the directory."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return f"skipped {path}: no such directory"
    except OSError as e:
        why = "a symlink, not followed" if path.is_symlink() else type(e).__name__
        return f"skipped {path}: {why}"
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        os.fchmod(fd, mode & ~0o007)
    finally:
        os.close(fd)
    return f"closed {path} to other users"


def docker_socket_problem(path: Path = DOCKER_SOCKET) -> str | None:
    """Why the build must not start: a Docker socket any user can reach is root on the host."""
    try:
        st = path.stat()
    except OSError:
        return None
    slots = range(SANDBOX_BASE_UID, SANDBOX_BASE_UID + _MAX_SLOT + 1)
    if st.st_mode & 0o006 or st.st_uid in slots or st.st_gid in slots:
        return (
            f"{path} is readable or writable by a sandbox uid, so a test could drive Docker "
            "as root; do not mount the Docker socket into Specster's container, or make it 0660"
        )
    return None


def _lock_file(path: Path) -> str:
    # O_NOFOLLOW + fchmod: a symlink planted under a credential's name never redirects the chmod.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return f"skipped {path}: no such file"
    except OSError as e:
        why = "a symlink, not followed" if path.is_symlink() else type(e).__name__
        return f"skipped {path}: {why}"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return f"skipped {path}: not a regular file"
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return f"locked {path}"


def scratch_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="specster-build-"))
    # nobody may traverse to the subdirectories handed over to it, but not list their siblings.
    path.chmod(0o711)
    return path
