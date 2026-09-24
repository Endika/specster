"""Run this repository's own build.setup_command and build.test_command as a sandbox slot, the
way a worker runs them. As root inside the image: python -P tests/dogfood_as_slot.py <repo>."""

import os
import shutil
import sys
from pathlib import Path

from specster.config import load_config
from specster.git import BOT_EMAIL, Author, Git
from specster.sandbox import Sandbox, require_root, scratch_dir, slot_identity


def main(repo: Path) -> int:
    build = load_config(repo / ".github" / "specster" / "config.yml").build
    slot = slot_identity(1)
    require_root(slot)
    scratch = scratch_dir()
    try:
        locked = scratch / "locked"
        locked.mkdir()
        git = Git(locked, Author("Specster", BOT_EMAIL), scratch / "git-home")
        git.run("init", "-q")
        sb = Sandbox(
            slot,
            build.test_timeout_s,
            build.test_output_max_kb * 1024,
            build.test_env,
            max_file_bytes=build.test_output_max_file_mb * 1024 * 1024,
        )
        sb.lock_down(locked, git, {}, runner_dirs=())
        enclosure = scratch / "enclosure"
        enclosure.mkdir(mode=0o750)
        enclosure.chmod(0o750)
        os.chown(enclosure, -1, slot.gid)
        tree = enclosure / "tree"
        skip = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".*_cache")
        shutil.copytree(repo, tree, symlinks=True, ignore=skip)
        sb.hand_over(tree)
        home = sb.new_home(enclosure, "home")
        for label, argv in (("setup", build.setup_command), ("tests", build.test_command)):
            if argv is None:
                continue
            res = sb.run(argv, tree, home, label)
            timed_out = " (timed out)" if res.timed_out else ""
            print(f"{label}: {argv} exit {res.exit_code}{timed_out} in {res.duration_s} s")
            if not res.ok:
                print(res.output)
                return 1
            lines = res.output.strip().splitlines()
            print(lines[-1] if lines else "(no output)")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
