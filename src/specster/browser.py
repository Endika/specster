"""Playwright and Chromium's headless shell, installed on the fly for a build that shoots pages."""

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PLAYWRIGHT_VERSION = "1.63.0"
BROWSER_ROOT = Path("/opt/specster-browser")
INSTALL_MAX_S = 300.0
SCRIPT = Path(__file__).with_name("screenshot_exec.py")
_TAIL_CHARS = 300
# Passed on so a runner behind a proxy can still download; nothing else of root's environment is.
_KEPT_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


@dataclass(frozen=True)
class BrowserEnv:
    python: Path
    browsers: Path


Installer = Callable[[float], BrowserEnv | str]


def install(timeout_s: float, root: Path = BROWSER_ROOT, uv: str = "uv") -> BrowserEnv | str:
    """As root, outside any sandbox: the venv's python and the browsers' path, or why not."""
    deadline = time.monotonic() + timeout_s
    venv, browsers = root / "venv", root / "browsers"
    python = venv / "bin" / "python"
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/root"),
        "LANG": "C.UTF-8",
        "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
        "DEBIAN_FRONTEND": "noninteractive",
        # The image's own python, readable by every slot; never one uv downloads under root's home.
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_CONFIG": "1",
    }
    env |= {k: v for k in _KEPT_ENV if (v := os.environ.get(k))}
    steps = [
        ("uv venv", [uv, "venv", "--clear", "--quiet", "--python", sys.executable, str(venv)]),
        (
            "uv pip install",
            [
                *(uv, "pip", "install", "--quiet", "--python", str(python)),
                f"playwright=={PLAYWRIGHT_VERSION}",
            ],
        ),
        (
            "playwright install",
            [str(python), "-m", "playwright", "install", "--with-deps", "--only-shell", "chromium"],
        ),
        ("chmod", ["chmod", "-R", "a+rX", str(root)]),
    ]
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name, argv in steps:
        left = deadline - time.monotonic()
        code, tail = _step(argv, root, env, left) if left > 0 else (None, "")
        if code is None:
            return f"the browser install timed out after {timeout_s:g} s, at {name}"
        if isinstance(code, OSError):
            return f"the browser install could not start {name}: {type(code).__name__}: {code}"
        if code != 0:
            return f"the browser install failed at {name} (exit {code}): {tail}"
    return BrowserEnv(python, browsers)


def _step(
    argv: Sequence[str], cwd: Path, env: dict[str, str], timeout_s: float
) -> tuple[int | OSError | None, str]:
    """The exit code, None on timeout, and the end of the output on one line."""
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            return e, ""
        code: int | None = None
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # Also whatever it left running, such as apt-get under Playwright's installer.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - 4 * _TAIL_CHARS))
        tail = " ".join(out.read().decode("utf-8", errors="replace").split())[-_TAIL_CHARS:]
    return code, tail
