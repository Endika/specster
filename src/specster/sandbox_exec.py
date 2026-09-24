"""Started by `Sandbox.run` already as the slot uid:
`python -I -m specster.sandbox_exec <max file bytes> <max processes> -- argv...`
"""

import os
import resource
import signal
import sys

USAGE = "usage: sandbox_exec <max file bytes> <max processes> -- argv..."


def _cap(which: int, value: int) -> None:
    _, hard = resource.getrlimit(which)
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    resource.setrlimit(which, (value, value))


def main(args: list[str]) -> int:
    if len(args) < 4 or args[2] != "--":
        print(USAGE, file=sys.stderr)
        return 2
    try:
        max_file, max_procs = int(args[0]), int(args[1])
        _cap(resource.RLIMIT_FSIZE, max_file)
        _cap(resource.RLIMIT_NPROC, max_procs)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError) as e:
        print(f"sandbox_exec: could not apply the limits: {e}", file=sys.stderr)
        return 126
    # Python ignores these and exec keeps ignored signals: give the tests the defaults back.
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(sig, signal.SIG_DFL)
    argv = args[3:]
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
