import json
import os
import shutil
import signal
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from specster import sandbox, sandbox_exec
from specster.isolation import RESUMER, STOPPER, daemon_runs, subreaper
from specster.sandbox import (
    SANDBOX_BASE_UID,
    RunResult,
    Sandbox,
    SandboxError,
    lock_down,
    require_root,
    scratch_dir,
    slot_identity,
)
from tests.fakes import make_repo, unix_socket

PY = sys.executable
ROOT_ONLY = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="drops to a sandbox uid; covered as root by --isolation-check in the docker job",
)


def ready(sb: Sandbox, root: Path) -> Sandbox:
    workspace = root / f"workspace-{len(list(root.glob('workspace-*')))}"
    sb.lock_down(workspace, make_repo(workspace, {"app.py": "x = 1\n"}), {})
    return sb


def box(
    root: Path, timeout: int = 20, max_bytes: int = 10_000, env: dict[str, str] | None = None
) -> Sandbox:
    return ready(Sandbox(None, timeout, max_bytes, env or {}), root)


def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    return h


@pytest.fixture
def sandbox_dir() -> Iterator[Path]:
    # pytest's basetemp is 0700, so a sandbox uid could never reach a tmp_path.
    scratch = scratch_dir()
    yield scratch
    shutil.rmtree(scratch)


def test_the_child_sees_only_the_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    code = "import json, os; print(json.dumps(dict(os.environ)))"
    res = box(tmp_path, env={"CI": "1"}).run([PY, "-c", code], tmp_path, home(tmp_path), "tests")
    seen = json.loads(res.output)
    assert res.ok and "ANTHROPIC_API_KEY" not in seen and seen["CI"] == "1"
    assert seen["HOME"] == str(home(tmp_path))
    assert seen["TMPDIR"] == str(home(tmp_path) / "tmp")
    assert set(seen) <= {"PATH", "HOME", "LANG", "TMPDIR", "CI", "LC_CTYPE"}


def test_a_new_home_is_private_and_carries_the_temporary_directory(tmp_path: Path) -> None:
    h = box(tmp_path).new_home(tmp_path, "h")
    assert stat.S_IMODE(h.stat().st_mode) == 0o700 and (h / "tmp").is_dir()


def test_a_hung_test_is_killed_at_the_timeout(tmp_path: Path) -> None:
    started = time.monotonic()
    res = box(tmp_path, timeout=1).run(
        [PY, "-c", "import time; time.sleep(60)"], tmp_path, home(tmp_path), "tests"
    )
    assert res.timed_out and res.exit_code is None and not res.ok
    assert time.monotonic() - started < 15


def test_a_background_child_does_not_outlive_the_run(tmp_path: Path) -> None:
    code = (
        "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)']); print(p.pid)"
    )
    res = box(tmp_path).run([PY, "-c", code], tmp_path, home(tmp_path), "tests")
    pid = int(res.output.strip())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the background child survived the run")


def test_the_time_left_in_the_build_shortens_a_run(tmp_path: Path) -> None:
    started = time.monotonic()
    sb = ready(Sandbox(None, 60, 10_000, {}, time_left=lambda: 1.0), tmp_path)
    res = sb.run([PY, "-c", "import time; time.sleep(60)"], tmp_path, home(tmp_path), "tests")
    assert res.timed_out and time.monotonic() - started < 15


def test_long_output_keeps_the_tail_and_reports_the_cut(tmp_path: Path) -> None:
    res = box(tmp_path, max_bytes=2048).run(
        [PY, "-c", "print('a' * 9000 + 'END')"], tmp_path, home(tmp_path), "tests a"
    )
    assert res.output.rstrip().endswith("END") and len(res.output.encode()) <= 2048
    assert res.truncation is not None
    assert res.truncation.startswith("tests a: output cut to the last 2 KB")


def test_a_missing_command_is_a_failed_run(tmp_path: Path) -> None:
    res = box(tmp_path).run(["/nonexistent/tool"], tmp_path, home(tmp_path), "setup")
    assert res.exit_code == 127 and not res.ok


def test_a_file_past_the_size_limit_fails_the_run_and_says_why(tmp_path: Path) -> None:
    sb = ready(Sandbox(None, 20, 10_000, {}, max_file_bytes=1024 * 1024), tmp_path)
    dd = ["dd", "if=/dev/zero", "of=big", "bs=65536", "count=64"]
    res = sb.run(dd, tmp_path, home(tmp_path), "tests")
    assert res.exit_code == -signal.SIGXFSZ and not res.ok
    assert "tests: killed by SIGXFSZ (a file reached the 1024 KB limit)" in res.output
    assert (tmp_path / "big").stat().st_size == 1024 * 1024


def test_a_python_writer_past_the_size_limit_gets_efbig(tmp_path: Path) -> None:
    sb = ready(Sandbox(None, 20, 10_000, {}, max_file_bytes=1024 * 1024), tmp_path)
    code = "open('big', 'wb').write(bytes(2 * 1024 * 1024))"
    res = sb.run([PY, "-c", code], tmp_path, home(tmp_path), "tests")
    assert res.exit_code == 1 and "File too large" in res.output


def test_the_process_and_core_limits_are_applied(tmp_path: Path) -> None:
    sb = ready(Sandbox(None, 20, 10_000, {}, max_file_bytes=5_000_000, max_procs=64), tmp_path)
    code = (
        "import json, resource as r, signal; print(json.dumps([r.getrlimit(r.RLIMIT_NPROC), "
        "r.getrlimit(r.RLIMIT_CORE), r.getrlimit(r.RLIMIT_FSIZE)]))"
    )
    res = sb.run([PY, "-c", code], tmp_path, home(tmp_path), "tests")
    assert json.loads(res.output) == [[64, 64], [0, 0], [5_000_000, 5_000_000]]
    assert res.argv == (PY, "-c", code)


def test_the_command_starts_with_sigpipe_and_sigxfsz_not_ignored(tmp_path: Path) -> None:
    res = box(tmp_path).run(
        ["grep", "^SigIgn:", "/proc/self/status"], tmp_path, home(tmp_path), "tests"
    )
    ignored = int(res.output.split()[1], 16)
    assert res.ok and not ignored & (1 << (signal.SIGPIPE - 1) | 1 << (signal.SIGXFSZ - 1))


def test_the_limits_wrapper_refuses_a_malformed_call() -> None:
    assert sandbox_exec.main(["1024", "--", "true"]) == 2
    assert sandbox_exec.main(["x", "512", "--", "true"]) == 126


def test_lock_down_locks_google_credentials_and_the_git_config(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    creds = tmp_path / "repo" / "gha-creds-1a2b.json"
    creds.write_text("{}")
    creds.chmod(0o644)
    google = tmp_path / "google.json"
    google.write_text("{}")
    google.chmod(0o644)
    done = lock_down(tmp_path / "repo", git, {"GOOGLE_APPLICATION_CREDENTIALS": str(google)})
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600 and any("gha-creds" in d for d in done)
    assert stat.S_IMODE(google.stat().st_mode) == 0o600 and any(str(google) in d for d in done)
    assert stat.S_IMODE((tmp_path / "repo" / ".git" / "config").stat().st_mode) == 0o600


def test_lock_down_locks_the_actions_file_command_files(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    names = ["GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STEP_SUMMARY", "GITHUB_STATE"]
    environ = {}
    for name in names:
        path = tmp_path / name.lower()
        path.write_text("")
        path.chmod(0o666)
        environ[name] = str(path)
    done = lock_down(tmp_path / "repo", git, environ)
    for name in names:
        assert stat.S_IMODE((tmp_path / name.lower()).stat().st_mode) == 0o600
        assert f"locked {tmp_path / name.lower()}" in done


def test_lock_down_never_follows_a_symlinked_credential_file(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    target.chmod(0o644)
    (tmp_path / "repo" / "gha-creds-link.json").symlink_to(target)
    google = tmp_path / "google-link.json"
    google.symlink_to(target)
    done = lock_down(tmp_path / "repo", git, {"GOOGLE_APPLICATION_CREDENTIALS": str(google)})
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    skipped = [d for d in done if d.startswith("skipped") and "symlink" in d]
    assert len(skipped) == 2 and not any(d.startswith("locked") for d in done)


def test_the_scratch_dir_can_be_traversed_but_not_listed() -> None:
    scratch = scratch_dir()
    try:
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o711
        assert scratch.name.startswith("specster-build-")
    finally:
        scratch.rmdir()


def test_a_sandbox_refuses_to_run_before_the_workspace_is_locked_down(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="lock_down"):
        Sandbox(None, 20, 10_000, {}).run([PY, "-c", "pass"], tmp_path, home(tmp_path), "tests")


def test_without_an_identity_hand_over_changes_nothing(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    box(tmp_path).hand_over(tmp_path)
    assert (tmp_path / "f").stat().st_uid == os.geteuid()


@pytest.mark.skipif(os.geteuid() == 0, reason="only meaningful unprivileged")
def test_the_build_refuses_to_start_unprivileged() -> None:
    require_root(None)
    with pytest.raises(SandboxError, match="must start as root"):
        require_root(slot_identity(1))


@ROOT_ONLY
def test_the_child_runs_as_its_slot_and_cannot_read_a_root_only_file(sandbox_dir: Path) -> None:
    secret = sandbox_dir / "secret"
    secret.write_text("s3cret")
    secret.chmod(0o600)
    tree = sandbox_dir / "tree"
    tree.mkdir()
    sb = ready(Sandbox(slot_identity(3), 20, 10_000, {}), sandbox_dir)
    sb.hand_over(tree)
    code = (
        "import os; print(os.getuid(), flush=True); open('ok', 'w').write('1'); "
        f"print(open({str(secret)!r}).read())"
    )
    res = sb.run([PY, "-c", code], tree, sb.new_home(sandbox_dir, "h"), "tests")
    assert res.output.startswith(str(SANDBOX_BASE_UID + 3)) and "PermissionError" in res.output
    assert (tree / "ok").read_text() == "1"


@ROOT_ONLY
def test_hand_over_gives_away_the_whole_tree_but_the_kept_names(sandbox_dir: Path) -> None:
    tree = sandbox_dir / "tree"
    (tree / ".git" / "objects").mkdir(parents=True)
    (tree / ".git" / "config").write_text("[core]\n")
    (tree / "keep").mkdir()
    (tree / "keep" / "f").write_text("x")
    (tree / "link").symlink_to("/etc/passwd")
    Sandbox(slot_identity(1), 20, 10_000, {}).hand_over(tree, keep_root=("keep",))
    owners = {p.name: p.lstat().st_uid for p in [tree, *tree.rglob("*")]}
    sb1 = SANDBOX_BASE_UID + 1
    assert owners == {
        "tree": sb1,
        ".git": sb1,
        "objects": sb1,
        "config": sb1,
        "keep": 0,
        "f": 0,
        "link": sb1,
    }
    assert stat.S_IMODE(tree.stat().st_mode) == 0o700
    assert Path("/etc/passwd").stat().st_uid == 0


def test_every_slot_has_its_own_uid_and_gid() -> None:
    ids = [slot_identity(n) for n in range(9)]
    assert ids[0].uid == SANDBOX_BASE_UID == ids[0].gid and len({i.uid for i in ids}) == 9
    with pytest.raises(ValueError, match="outside"):
        slot_identity(9)


@ROOT_ONLY
def test_one_slot_cannot_reach_another_slots_tree(sandbox_dir: Path) -> None:
    other = Sandbox(slot_identity(2), 20, 10_000, {})
    victim = sandbox_dir / "victim"
    victim.mkdir()
    (victim / "app.py").write_text("x = 1\n")
    other.hand_over(victim)
    sb = ready(Sandbox(slot_identity(1), 20, 10_000, {}), sandbox_dir)
    tree = sandbox_dir / "tree"
    tree.mkdir()
    sb.hand_over(tree)
    code = (
        "import sys\n"
        "for op in (lambda: open(sys.argv[1]).read(), lambda: open(sys.argv[2], 'x')):\n"
        "    try:\n        op()\n        print('reached')\n"
        "    except OSError as e:\n        print(type(e).__name__)\n"
    )
    argv = [PY, "-c", code, str(victim / "app.py"), str(victim / "planted.py")]
    res = sb.run(argv, tree, sb.new_home(sandbox_dir, "h"), "tests")
    assert res.output.split() == ["PermissionError", "PermissionError"]
    assert not (victim / "planted.py").exists()


DAEMON = (
    "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', "
    "'import time; time.sleep(60)'], start_new_session=True); print(p.pid)"
)


def _alive(pid: int) -> bool:
    try:
        return "\nState:\tZ" not in Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return False


@ROOT_ONLY
def test_a_detached_process_of_the_slot_is_killed_after_the_run(sandbox_dir: Path) -> None:
    sb = ready(Sandbox(slot_identity(4), 20, 10_000, {}), sandbox_dir)
    tree = sandbox_dir / "tree"
    tree.mkdir()
    sb.hand_over(tree)
    res = sb.run([PY, "-c", DAEMON], tree, sb.new_home(sandbox_dir, "h"), "tests")
    assert res.ok and not _alive(int(res.output.strip()))


# Gives the daemons time to take their baseline, so they race whatever starts after the run.
LAUNCH = (
    "import subprocess, sys, time\n"
    "for code in sys.argv[1:]:\n"
    "    print(subprocess.Popen([sys.executable, '-c', code], start_new_session=True).pid)\n"
    "time.sleep(0.5)\n"
)
# A ring of processes, each resuming (SIGCONT) every process of its uid.
RING_MEMBER = (
    "import os, signal, sys, time\n"
    "tag = chr(10) + 'Uid:' + chr(9) + str(os.getuid()) + chr(9)\n"
    "fork = len(sys.argv) > 1\n"
    "def mine():\n"
    "    for d in os.listdir('/proc'):\n"
    "        try:\n"
    "            if d.isdigit() and tag in open(f'/proc/{d}/status').read():\n"
    "                yield int(d)\n"
    "        except OSError:\n"
    "            pass\n"
    "while True:\n"
    "    for pid in mine():\n"
    "        try:\n"
    "            os.kill(pid, signal.SIGCONT)\n"
    "        except OSError:\n"
    "            pass\n"
    "    if fork:\n"
    "        try:\n"
    "            if os.fork() == 0:\n"
    "                time.sleep(30); os._exit(0)\n"
    "        except OSError:\n"
    "            pass\n"
)


def ring(count: int, fork: bool) -> str:
    extra = ", 'fork'" if fork else ""
    return (
        "import subprocess, sys, time\n"
        f"kids = [subprocess.Popen([sys.executable, '-c', {RING_MEMBER!r}{extra}]) "
        f"for _ in range({count})]\n"
        "time.sleep(0.5)\n"
    )


FORK_BOMB = (
    "import os\nwhile True:\n    try:\n        os.fork()\n    except OSError:\n        pass\n"
)


def _slot_run(sandbox_dir: Path, slot: int, argv: list[str], **limits: Any) -> RunResult:
    sb = ready(Sandbox(slot_identity(slot), 20, 10_000, {}, **limits), sandbox_dir)
    tree = sandbox_dir / "tree"
    tree.mkdir()
    sb.hand_over(tree)
    return sb.run(argv, tree, sb.new_home(sandbox_dir, "h"), "tests")


@ROOT_ONLY
def test_daemons_that_stop_and_resume_their_siblings_are_still_killed(sandbox_dir: Path) -> None:
    res = _slot_run(sandbox_dir, 6, [PY, "-c", LAUNCH, STOPPER, RESUMER])
    pids = [int(p) for p in res.output.split()]
    assert res.ok and len(pids) == 2 and not any(_alive(p) for p in pids)
    assert not sandbox._live_pids(SANDBOX_BASE_UID + 6)


@ROOT_ONLY
def test_a_fork_bomb_is_bounded_and_killed(sandbox_dir: Path) -> None:
    res = _slot_run(sandbox_dir, 7, [PY, "-c", LAUNCH, FORK_BOMB], max_procs=64)
    assert res.ok and not sandbox._live_pids(SANDBOX_BASE_UID + 7)


def _reap_repeatedly(sandbox_dir: Path, slot: int, code: str, times: int, **limits: Any) -> None:
    sb = ready(Sandbox(slot_identity(slot), 20, 10_000, {}, **limits), sandbox_dir)
    tree = sandbox_dir / "tree"
    tree.mkdir()
    sb.hand_over(tree)
    home = sb.new_home(sandbox_dir, "h")
    for _ in range(times):
        res = sb.run([PY, "-c", code], tree, home, "tests")
        assert res.ok and not sandbox._live_pids(SANDBOX_BASE_UID + slot)


@ROOT_ONLY
def test_a_sigcont_ring_of_twenty_is_killed_every_time(sandbox_dir: Path) -> None:
    _reap_repeatedly(sandbox_dir, 6, LAUNCH + ring(20, fork=False), times=10)


@ROOT_ONLY
def test_a_forking_sigcont_ring_under_nproc_is_killed(sandbox_dir: Path) -> None:
    _reap_repeatedly(sandbox_dir, 7, LAUNCH + ring(10, fork=True), times=5, max_procs=64)


@ROOT_ONLY
def test_a_child_blocked_opening_a_fifo_does_not_stall_the_reap(sandbox_dir: Path) -> None:
    # The child blocks in open() on a FIFO with no writer (state S, not D); nothing may survive.
    code = (
        "import os, subprocess, sys, time\n"
        "fifo = os.path.join(os.environ['HOME'], 'f')\n"
        "os.mkfifo(fifo)\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import sys; open(sys.argv[1])', fifo], "
        "start_new_session=True)\n"
        "print(p.pid)\n"
        "time.sleep(0.5)\n"
    )
    res = _slot_run(sandbox_dir, 8, [PY, "-c", code])
    assert res.ok and not sandbox._live_pids(SANDBOX_BASE_UID + 8)


@ROOT_ONLY
def test_a_process_that_survives_the_kill_fails_the_run(
    sandbox_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_KILL_SIGNAL", signal.SIGCONT)
    with pytest.raises(SandboxError, match="survived"):
        _slot_run(sandbox_dir, 5, [PY, "-c", DAEMON])
    monkeypatch.undo()
    sandbox._reap(slot_identity(5))
    assert not sandbox._live_pids(SANDBOX_BASE_UID + 5)


@ROOT_ONLY
def test_runs_that_leave_daemons_behind_never_exhaust_the_slot(sandbox_dir: Path) -> None:
    # Orphans reparent to PID 1 in the image; as a subreaper the test process stands in for it.
    sb = ready(Sandbox(slot_identity(3), 60, 10_000, {}), sandbox_dir)
    tree = sandbox_dir / "tree"
    tree.mkdir()
    sb.hand_over(tree)
    subreaper(True)
    try:
        assert daemon_runs(sb, tree, sb.new_home(sandbox_dir, "h")) == []
    finally:
        subreaper(False)
    assert not sandbox._scan(SANDBOX_BASE_UID + 3)


def test_lock_down_closes_the_workspace_and_the_runner_dirs_to_other_users(
    tmp_path: Path,
) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    home_dir, temp = tmp_path / "home", tmp_path / "temp"
    for d in (tmp_path / "repo", home_dir, temp):
        d.mkdir(exist_ok=True)
        d.chmod(0o755)
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target)
    dirs = [home_dir, temp, link, tmp_path / "missing"]
    done = lock_down(tmp_path / "repo", git, {}, dirs)
    for d in (tmp_path / "repo", home_dir, temp):
        assert stat.S_IMODE(d.stat().st_mode) == 0o750 and f"closed {d} to other users" in done
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert f"skipped {link}: a symlink, not followed" in done
    assert f"skipped {tmp_path / 'missing'}: no such directory" in done


def test_a_docker_socket_other_users_can_reach_stops_the_build(short_dir: Path) -> None:
    assert sandbox.docker_socket_problem(short_dir / "none.sock") is None
    ok = sandbox.docker_socket_problem(unix_socket(short_dir / "ok.sock", 0o660))
    # Run by a worker, the socket is the slot uid's own, which is refused just the same.
    slots = range(SANDBOX_BASE_UID, SANDBOX_BASE_UID + 9)
    assert (ok is not None) == (os.getuid() in slots)
    for mode in (0o666, 0o664, 0o662):
        problem = sandbox.docker_socket_problem(unix_socket(short_dir / f"{mode:o}.sock", mode))
        assert problem is not None and "readable or writable by a sandbox uid" in problem


def owners(top: Path) -> set[tuple[int, int]]:
    return {(p.lstat().st_uid, p.lstat().st_gid) for p in [top, *top.rglob("*")]}


def test_restoring_the_owner_leaves_a_git_dir_that_is_already_theirs_alone(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    git_dir = tmp_path / "repo" / ".git"
    assert sandbox.restore_owner(git_dir, os.getuid(), os.getgid()) == []
    assert owners(git_dir) == {(os.getuid(), os.getgid())}


@ROOT_ONLY
def test_restoring_the_owner_gives_back_what_root_wrote_never_through_a_symlink(
    tmp_path: Path,
) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    git_dir = tmp_path / "repo" / ".git"
    for p in [git_dir, *git_dir.rglob("*")]:
        os.chown(p, 4321, 4321, follow_symlinks=False)
    git.run("config", "user.name", "root was here")
    git.run("branch", "specster/issue-7")
    outside = tmp_path / "outside"
    outside.write_text("root's")
    (git_dir / "link").symlink_to(outside)
    moved = sandbox.restore_owner(git_dir, 4321, 4321)
    assert f"{git_dir}/config" in moved and f"{git_dir}/link" in moved
    assert owners(git_dir) == {(4321, 4321)} and outside.stat().st_uid == 0
