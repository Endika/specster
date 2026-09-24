import ctypes
import json
import os
import socket
import sys
import sysconfig
from pathlib import Path
from typing import Any

import specster
from specster.git import BOT_EMAIL, Author, Git
from specster.sandbox import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_PROCS,
    FILE_COMMANDS,
    Sandbox,
    SandboxError,
    docker_socket_problem,
    require_root,
    slot_identity,
)

CANARY_ENV = "SPECSTER_ISOLATION_CANARY"
PROBE_SLOT = slot_identity(1)
OTHER_SLOT = slot_identity(2)
_MIN_CANARY = 8

# Hostile daemons a test can leave behind: one stops every new process of its uid (so a killer
# started as that uid never runs), one keeps resuming the processes that were there before it.
STOPPER = """
import os, signal

me, tag = os.getpid(), f"\\nUid:\\t{os.getuid()}\\t"


def mine():
    for d in os.listdir("/proc"):
        try:
            with open(f"/proc/{d}/status") as f:
                if d.isdigit() and tag in f.read():
                    yield int(d)
        except OSError:
            pass


seen = set(mine())
while True:
    for pid in mine():
        if pid != me and pid not in seen:
            seen.add(pid)
            try:
                os.kill(pid, signal.SIGSTOP)
            except OSError:
                pass
"""
RESUMER = STOPPER.replace("pid not in seen", "pid in seen").replace("SIGSTOP", "SIGCONT")
# A ring: many processes each resuming every process of their uid, so no single stop sticks.
RING_MEMBER = (
    STOPPER.replace("if pid != me and pid not in seen:", "if True:")
    .replace("seen.add(pid)\n            ", "")
    .replace("SIGSTOP", "SIGCONT")
)
RING = (
    "import subprocess, sys\n"
    f"kids = [subprocess.Popen([sys.executable, '-I', '-c', {RING_MEMBER!r}]) for _ in range(20)]\n"
    "import time; time.sleep(3600)\n"
)

# Leaves `n` detached daemons behind; fails when a fork is refused (the slot's RLIMIT_NPROC).
DAEMONS = """
import os, shutil, sys, time

sleep, n = shutil.which("sleep"), int(sys.argv[1])
for i in range(n):
    try:
        pid = os.fork()
    except OSError as e:
        print(f"fork {i + 1} of {n} failed: {e}")
        sys.exit(1)
    if pid == 0:
        os.setsid()
        if sleep:
            os.execv(sleep, [sleep, "600"])
        time.sleep(600)
        os._exit(0)
print(f"forked {n}")
"""
DAEMON_COUNT = 200
DAEMON_RUNS = 3
_PR_SET_CHILD_SUBREAPER = 36

PROBE = """
import json, os, resource, socket, subprocess, sys, time

paths = json.loads(sys.argv[1])


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError as e:
        return type(e).__name__


def append(path):
    try:
        open(path, "ab").close()
    except OSError as e:
        return type(e).__name__
    return "opened"


def connect(path):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(path)
    except OSError as e:
        return type(e).__name__
    finally:
        s.close()
    return "connected"


def write(path):
    try:
        with open(path, "x") as f:
            f.write("1")
    except OSError as e:
        return type(e).__name__
    return "written"


daemon = subprocess.Popen(
    [sys.executable, "-I", "-c", "import time; time.sleep(120)"],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
hostile = [
    subprocess.Popen([sys.executable, "-I", "-c", code], start_new_session=True).pid
    for code in paths["hostile"]
]
time.sleep(0.5)  # let them take their baseline and race what runs after the probe
report = {
    "uid": os.getuid(),
    "gid": os.getgid(),
    "groups": os.getgroups(),
    "limits": [
        list(resource.getrlimit(r))
        for r in (resource.RLIMIT_FSIZE, resource.RLIMIT_NPROC, resource.RLIMIT_CORE)
    ],
    "env": dict(os.environ),
    "parent_environ": read(f"/proc/{os.getppid()}/environ"),
    "pid1_environ": read("/proc/1/environ"),
    "reads": {name: read(path) for name, path in paths["reads"].items()},
    "appends": {name: append(path) for name, path in paths["appends"].items()},
    "connects": {name: connect(path) for name, path in paths["connects"].items()},
    "wrote": write(paths["inside"]) == "written",
    "outside": {path: write(path) for path in paths["outside"]},
    "daemon": daemon.pid,
    "hostile": hostile,
}
print(json.dumps(report))
"""


def _repo(scratch: Path, canary: str) -> tuple[Path, Git, Path]:
    repo = scratch / "repo"
    repo.mkdir()
    git = Git(repo, Author("Specster", BOT_EMAIL), scratch / "git-home")
    git.run("init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    git.run("add", "--", "app.py")
    git.run("commit", "-q", "-m", "chore: init")
    header = f'[http "https://github.com/"]\n\textraheader = AUTHORIZATION: basic {canary}\n'
    # As actions/checkout v6+ does: the token in a file reached through includeIf.
    include = scratch / "checkout-creds.config"
    include.write_text(header)
    include.chmod(0o644)
    config = repo / ".git" / "config"
    with config.open("a") as f:
        f.write(header)
        f.write(f'[includeIf "gitdir:{repo}/.git/"]\n\tpath = {include}\n')
    config.chmod(0o644)
    return repo, git, include


def _alive(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False
    fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    return not fields.get("State", "").strip().startswith("Z")


def subreaper(on: bool) -> None:
    """Adopt orphaned descendants as PID 1 does, so a check outside the image sees their zombies."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, int(on), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER) failed")


def daemon_runs(sb: Sandbox, tree: Path, home: Path) -> list[str]:
    """Run the daemon launcher DAEMON_RUNS times in one slot; each run must still fork them all."""
    argv = [sys.executable, "-I", "-c", DAEMONS, str(DAEMON_COUNT)]
    for n in range(1, DAEMON_RUNS + 1):
        res = sb.run(argv, tree, home, f"daemons {n}")
        if not res.ok:
            return [f"run {n} leaving {DAEMON_COUNT} daemons could not fork: {res.output[-500:]}"]
    return []


def _docker_socket(scratch: Path) -> tuple[Path, list[str]]:
    path = scratch / "docker.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.close()
    failures = []
    path.chmod(0o666)
    if docker_socket_problem(path) is None:
        failures.append("a Docker socket every user can write was not refused")
    path.chmod(0o660)
    if docker_socket_problem(path) is not None:
        failures.append("a 0660 root-owned Docker socket was refused")
    return path, failures


def isolation_check(scratch: Path) -> list[str]:
    """Run a probe in a sandbox slot against planted secrets; return every way it got through."""
    canary = os.environ.get(CANARY_ENV, "")
    if len(canary) < _MIN_CANARY:
        return [
            f"{CANARY_ENV} is not set (or shorter than {_MIN_CANARY} chars): "
            "pass it with docker run -e so it is in PID 1's environment"
        ]
    try:
        require_root(PROBE_SLOT)
    except SandboxError as e:
        return [str(e)]
    repo, git, include = _repo(scratch, canary)
    gha = repo / "gha-creds-isolation.json"
    google = scratch / "google.json"
    for path in (gha, google):
        path.write_text(json.dumps({"private_key": canary}))
        path.chmod(0o644)
    environ = {"GOOGLE_APPLICATION_CREDENTIALS": str(google)}
    # Stand-ins for the runner's mounts, each with a file any user could read before lock_down.
    runner_dirs = [scratch / "github-home", scratch / "runner-temp"]
    for folder in [repo, *runner_dirs]:
        folder.mkdir(exist_ok=True)
        folder.chmod(0o755)
        (folder / "notes.txt").write_text(canary)
        (folder / "notes.txt").chmod(0o644)
    for name in FILE_COMMANDS:
        environ[name] = str(scratch / name.lower())
        Path(environ[name]).write_text("")
        Path(environ[name]).chmod(0o666)
    sb = Sandbox(PROBE_SLOT, 60, 1_000_000, {})
    sb.lock_down(repo, git, environ, runner_dirs)
    docker, failures = _docker_socket(scratch)

    if canary in (repo / ".git" / "config").read_text():
        failures.append("lock_down left the canary in .git/config")
    victim = scratch / "other-slot"
    victim.mkdir()
    (victim / "app.py").write_text(f"TOKEN = {canary!r}\n")
    Sandbox(OTHER_SLOT, 60, 1_000_000, {}).hand_over(victim)
    probe = scratch / "probe"
    probe.mkdir()
    sb.hand_over(probe)
    # Specster's own code and venv: a planted module would run later as root.
    package = Path(specster.__file__).parent
    site = Path(sysconfig.get_paths()["purelib"])
    outside = [
        scratch / "escape",
        repo / "escape",
        victim / "planted.py",
        package / "planted.py",
        site / "planted.pth",
    ]
    paths = {
        "reads": {
            "git_config": str(repo / ".git" / "config"),
            "include": str(include),
            "gha_creds": str(gha),
            "google_creds": str(google),
            "other_slot_tree": str(victim / "app.py"),
            "workspace file": str(repo / "notes.txt"),
            **{f"{d.name} file": str(d / "notes.txt") for d in runner_dirs},
        },
        "connects": {"a 0660 docker socket": str(docker)},
        "appends": {
            **{f"the {name} file": environ[name] for name in FILE_COMMANDS},
            "specster/__init__.py": str(package / "__init__.py"),
        },
        "hostile": [STOPPER, RESUMER, RING],
        "inside": str(probe / "out"),
        "outside": [str(p) for p in outside],
    }
    argv = [sys.executable, "-I", "-c", PROBE, json.dumps(paths)]
    try:
        res = sb.run(argv, probe, sb.new_home(scratch, "home"), "isolation probe")
    except SandboxError as e:
        return [*failures, str(e)]
    if canary in res.output:
        failures.append("the canary reached the test process")
    if not res.ok or res.truncation is not None:
        return [*failures, f"the probe failed (exit {res.exit_code}): {res.output[-2000:]}"]
    report: dict[str, Any] = json.loads(res.output.strip().splitlines()[-1])
    failures += _judge(report, outside)
    subreaper(True)
    try:
        return failures + daemon_runs(sb, probe, sb.new_home(scratch, "daemons-home"))
    except SandboxError as e:
        return [*failures, str(e)]
    finally:
        subreaper(False)


def _judge(report: dict[str, Any], outside: list[Path]) -> list[str]:
    failures: list[str] = []
    ids = (report["uid"], report["gid"], report["groups"])
    want: tuple[int, int, list[int]] = (PROBE_SLOT.uid, PROBE_SLOT.gid, [])
    if ids != want:
        failures.append(f"the probe ran as uid/gid/groups {ids}, not {want}")
    limits = [[n, n] for n in (DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_PROCS, 0)]
    if report["limits"] != limits:
        failures.append(f"the probe ran with rlimits {report['limits']}, not {limits}")
    if set(report["env"]) - {"PATH", "HOME", "LANG", "TMPDIR", "LC_CTYPE"}:
        failures.append(f"the probe saw extra environment variables: {sorted(report['env'])}")
    reads = {
        "/proc/<ppid>/environ": report["parent_environ"],
        "/proc/1/environ": report["pid1_environ"],
        **report["reads"],
    }
    for name, value in reads.items():
        if value != "PermissionError":
            failures.append(f"the probe could read {name} (got {value[:40]!r})")
    for name, value in report["connects"].items():
        if value != "PermissionError":
            failures.append(f"the probe could connect to {name} (got {value!r})")
    for name, value in report["appends"].items():
        if value != "PermissionError":
            failures.append(f"the probe could open {name} for writing (got {value!r})")
    if not report["wrote"]:
        failures.append("the probe could not write inside its handed-over tree")
    for path in outside:
        if report["outside"][str(path)] == "written" or path.exists():
            failures.append(f"the probe wrote outside its tree: {path}")
    for pid in [report["daemon"], *report["hostile"]]:
        if _alive(pid):
            failures.append(f"a process the probe left running survived the run: {pid}")
    return failures
