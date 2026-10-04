"""mise, downloaded on the first build that needs toolchains instead of baked into the image."""

import hashlib
import io
import os
import shutil
import stat
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

MISE_VERSION = "v2026.9.12"
MISE_SHA256 = "b4058dece685259910d3aba5782445996eea79dbdb3cf952a6eb81aadf0373ff"
MISE_ROOT = Path("/opt/mise")
INSTALL_MAX_S = 120.0
MAX_BYTES = 200 * 1024 * 1024
_STEP_S = 10.0

# (url, seconds left, size cap) -> the body; raises on any failure.
Fetch = Callable[[str, float, int], bytes]
MiseInstaller = Callable[[float], Path | str]


def download(
    url: str, timeout_s: float, max_bytes: int, transport: httpx.BaseTransport | None = None
) -> bytes:
    deadline = time.monotonic() + timeout_s
    step = min(timeout_s, _STEP_S)
    body = bytearray()
    with (
        httpx.Client(
            follow_redirects=True, timeout=httpx.Timeout(step), transport=transport
        ) as client,
        client.stream("GET", url) as resp,
    ):
        resp.raise_for_status()
        if int(resp.headers.get("content-length", 0)) > max_bytes:
            raise ValueError(f"more than {max_bytes} bytes")
        for chunk in resp.iter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise ValueError(f"more than {max_bytes} bytes")
            if time.monotonic() > deadline:
                raise TimeoutError(f"not finished after {timeout_s:g} s")
    return bytes(body)


def _trusted(binary: Path, root: Path) -> bool:
    """Owned by us or root, no group or other write, no symlink: the binary up to root's parent."""
    for path in (binary, binary.parent, root, root.parent):
        try:
            st = path.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode) or st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
            return False
    return binary.is_file()


def _set_modes(tree: Path) -> None:
    for path in [tree, *tree.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(0o755 if path.is_dir() or mode & 0o111 else 0o644)


def ensure_mise(timeout_s: float, root: Path = MISE_ROOT, fetch: Fetch = download) -> Path | str:
    """As root, outside any sandbox: the mise binary, downloaded once, or why not."""
    binary = root / "bin" / "mise"
    if _trusted(binary, root):
        return binary
    url = (
        f"https://github.com/jdx/mise/releases/download/{MISE_VERSION}/"
        f"mise-{MISE_VERSION}-linux-x64.tar.gz"
    )
    try:
        data = fetch(url, timeout_s, MAX_BYTES)
    except (httpx.HTTPError, OSError, ValueError) as e:
        return f"the download failed: {type(e).__name__}: {e}"
    got = hashlib.sha256(data).hexdigest()
    if got != MISE_SHA256:
        return f"the download's sha256 is {got}, expected {MISE_SHA256}"
    staging: Path | None = None
    try:
        root.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".mise-", dir=root.parent))
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(staging, filter="data")
        unpacked = staging / "mise"
        if not (unpacked / "bin" / "mise").is_file():
            return "the archive has no mise/bin/mise"
        _set_modes(unpacked)
        shutil.rmtree(root, ignore_errors=True)
        unpacked.rename(root)
    except (tarfile.TarError, OSError) as e:
        return f"unpacking failed: {type(e).__name__}: {e}"
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    return binary
