import hashlib
import io
import stat
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from specster import mise
from specster.mise import download, ensure_mise


def archive(members: dict[str, bytes], mode: int | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = mode or (0o700 if name.endswith("/mise") else 0o600)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


class Fetcher:
    def __init__(self, data: bytes | Exception) -> None:
        self.data = data
        self.calls: list[tuple[str, float, int]] = []

    def __call__(self, url: str, timeout_s: float, max_bytes: int) -> bytes:
        self.calls.append((url, timeout_s, max_bytes))
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def pinned(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(mise, "MISE_SHA256", hashlib.sha256(data).hexdigest())


def test_a_binary_already_there_is_reused_without_fetching(tmp_path: Path) -> None:
    root = tmp_path / "mise"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "mise").write_text("x")
    fetch = Fetcher(OSError("no network"))
    assert ensure_mise(5, root, fetch) == root / "bin" / "mise" and fetch.calls == []


def test_the_pinned_release_is_downloaded_unpacked_and_made_executable_for_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = archive({"mise/bin/mise": b"#!/bin/sh\n"})
    pinned(monkeypatch, data)
    fetch = Fetcher(data)
    root = tmp_path / "opt" / "mise"
    got = ensure_mise(7, root, fetch)
    assert got == root / "bin" / "mise"
    url, timeout_s, cap = fetch.calls[0]
    assert url == (
        f"https://github.com/jdx/mise/releases/download/{mise.MISE_VERSION}/"
        f"mise-{mise.MISE_VERSION}-linux-x64.tar.gz"
    )
    assert timeout_s == 7 and cap == mise.MAX_BYTES
    assert got.stat().st_mode & 0o555 == 0o555 and root.stat().st_mode & 0o505 == 0o505
    assert sorted(p.name for p in root.parent.iterdir()) == ["mise"]


def test_a_sha256_mismatch_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    root = tmp_path / "opt" / "mise"
    got = ensure_mise(5, root, Fetcher(archive({"mise/bin/mise": b"evil"})))
    assert isinstance(got, str) and "sha256" in got and "expected" in got
    assert not root.parent.exists()


def test_a_member_escaping_the_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = archive({"mise/bin/mise": b"x", "../evil": b"x"})
    pinned(monkeypatch, data)
    root = tmp_path / "opt" / "mise"
    got = ensure_mise(5, root, Fetcher(data))
    assert isinstance(got, str) and got.startswith("unpacking failed")
    assert not (tmp_path / "evil").exists() and not root.exists()
    assert list(root.parent.iterdir()) == []


def test_an_archive_without_the_binary_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = archive({"mise/README": b"x"})
    pinned(monkeypatch, data)
    assert ensure_mise(5, tmp_path / "mise", Fetcher(data)) == "the archive has no mise/bin/mise"


def test_a_fetch_failure_is_a_reason(tmp_path: Path) -> None:
    got = ensure_mise(5, tmp_path / "mise", Fetcher(TimeoutError("not finished after 5 s")))
    assert got == "the download failed: TimeoutError: not finished after 5 s"


def test_a_world_writable_archive_ends_with_no_group_or_other_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = archive({"mise/bin/mise": b"x", "mise/share/doc": b"y"}, mode=0o777)
    pinned(monkeypatch, data)
    root = tmp_path / "mise"
    assert ensure_mise(5, root, Fetcher(data)) == root / "bin" / "mise"
    for path in [root, *root.rglob("*")]:
        assert not stat.S_IMODE(path.lstat().st_mode) & 0o022
    assert (root / "bin" / "mise").stat().st_mode & 0o555 == 0o555


def test_a_root_that_cannot_be_created_is_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = archive({"mise/bin/mise": b"x"})
    pinned(monkeypatch, data)
    (tmp_path / "file").write_text("")
    got = ensure_mise(5, tmp_path / "file" / "sub" / "mise", Fetcher(data))
    assert isinstance(got, str) and got.startswith("unpacking failed")


def test_a_binary_others_can_write_is_not_trusted_and_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = archive({"mise/bin/mise": b"good"})
    pinned(monkeypatch, data)
    root = tmp_path / "mise"
    (root / "bin").mkdir(parents=True)
    binary = root / "bin" / "mise"
    binary.write_text("bad")
    binary.chmod(0o777)
    fetch = Fetcher(data)
    assert ensure_mise(5, root, fetch) == binary
    assert len(fetch.calls) == 1 and binary.read_bytes() == b"good"


def test_a_symlinked_binary_is_not_trusted(tmp_path: Path) -> None:
    root = tmp_path / "mise"
    (root / "bin").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text("x")
    (root / "bin" / "mise").symlink_to(target)
    fetch = Fetcher(OSError("no network"))
    got = ensure_mise(5, root, fetch)
    assert isinstance(got, str) and len(fetch.calls) == 1


def get(handler: httpx.MockTransport) -> bytes:
    return download("https://example.test/mise.tgz", 5, 100, handler)


def test_download_returns_the_body_and_follows_redirects() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mise.tgz":
            return httpx.Response(302, headers={"location": "https://cdn.test/blob"})
        return httpx.Response(200, content=b"body")

    assert get(httpx.MockTransport(handle)) == b"body"


def test_download_refuses_an_http_error() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        get(httpx.MockTransport(lambda _r: httpx.Response(404)))


def test_download_refuses_a_body_over_the_cap_while_streaming() -> None:
    big = httpx.MockTransport(lambda _r: httpx.Response(200, content=iter([b"x" * 60] * 5)))
    with pytest.raises(ValueError, match="more than 100 bytes"):
        get(big)


def test_download_refuses_a_declared_length_over_the_cap() -> None:
    declared = httpx.MockTransport(
        lambda _r: httpx.Response(200, headers={"content-length": "101"}, content=b"x" * 101)
    )
    with pytest.raises(ValueError, match="more than 100 bytes"):
        get(declared)


def test_download_stops_a_drip_that_outlasts_the_deadline() -> None:
    def drip() -> Iterator[bytes]:
        for _ in range(100):
            time.sleep(0.05)
            yield b"x"

    slow = httpx.MockTransport(lambda _r: httpx.Response(200, content=drip()))
    with pytest.raises(TimeoutError, match="not finished"):
        download("https://example.test/mise.tgz", 0.2, 100, slow)
