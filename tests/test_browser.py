import json
import os
import shutil
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from specster.browser import INSTALL_MAX_S, PLAYWRIGHT_VERSION, SCRIPT, BrowserEnv, install
from specster.evidence import PAGE_MAX_HEIGHT, PNG_MAGIC, read_shots
from specster.sandbox import Sandbox, scratch_dir, slot_identity
from specster.schemas import EvidencePage
from tests.test_evidence import free_port
from tests.test_sandbox import ready


@pytest.fixture
def scratch() -> Iterator[Path]:
    # pytest's basetemp is 0700, so the slot could never reach a tmp_path.
    path = scratch_dir()
    yield path
    shutil.rmtree(path)


# Stands in for Playwright's installer: records its arguments and leaves a private file.
FAKE_PYTHON = """\
#!/bin/sh
echo "$*" >> {calls}
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
touch "$PLAYWRIGHT_BROWSERS_PATH/chrome"
chmod 600 "$PLAYWRIGHT_BROWSERS_PATH/chrome"
"""
# Stands in for uv: `venv` makes a venv with that python.
FAKE_UV = """\
#!{python}
import json, os, pathlib, sys, time
here = pathlib.Path(__file__).parent
mode = (here / "mode").read_text()
with open(here / "calls", "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd()}}))
    f.write("\\n")
if mode == "hang":
    time.sleep(60)
if mode == "fail" and sys.argv[1] == "pip":
    sys.exit("error: no matching distribution found for playwright")
if sys.argv[1] == "venv":
    python = pathlib.Path(sys.argv[-1]) / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text({fake_python!r})
    python.chmod(0o755)
"""


def fake_uv(tmp_path: Path, mode: str) -> str:
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    fake_python = FAKE_PYTHON.format(calls=uv.with_name("calls"))
    uv.write_text(FAKE_UV.format(python=sys.executable, fake_python=fake_python))
    uv.chmod(0o755)
    (uv.parent / "mode").write_text(mode)
    return str(uv)


def calls(uv: str) -> list[str]:
    return Path(uv).with_name("calls").read_text().splitlines()


def test_install_makes_a_venv_with_playwright_and_its_shell_readable_by_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    uv = fake_uv(tmp_path, "ok")
    root = tmp_path / "browser"
    got = install(60, root, uv)
    assert got == BrowserEnv(root / "venv" / "bin" / "python", root / "browsers")
    venv, pip, shell = calls(uv)
    first, second = json.loads(venv), json.loads(pip)
    assert first["argv"] == [
        "venv",
        "--clear",
        "--quiet",
        "--python",
        sys.executable,
        str(root / "venv"),
    ]
    assert second["argv"][:2] == ["pip", "install"]
    assert f"playwright=={PLAYWRIGHT_VERSION}" in second["argv"]
    assert "GITHUB_TOKEN" not in first["env"] and "UV_NO_CONFIG" in first["env"]
    # Away from the repository: a uv.toml there must not choose where Playwright comes from.
    assert first["cwd"] == str(root)
    assert shell == "-m playwright install --with-deps --only-shell chromium"
    mode = (root / "browsers" / "chrome").stat().st_mode
    assert stat.S_IMODE(mode) & 0o044 == 0o044


def test_a_failed_step_is_the_reason_with_the_end_of_its_output(tmp_path: Path) -> None:
    uv = fake_uv(tmp_path, "fail")
    got = install(60, tmp_path / "browser", uv)
    assert got == (
        "the browser install failed at uv pip install (exit 1): "
        "error: no matching distribution found for playwright"
    )


def test_the_install_is_cut_at_its_time_limit(tmp_path: Path) -> None:
    uv = fake_uv(tmp_path, "hang")
    started = time.monotonic()
    got = install(1, tmp_path / "browser", uv)
    assert got == "the browser install timed out after 1 s, at uv venv"
    assert time.monotonic() - started < 10


def test_a_missing_uv_is_the_reason(tmp_path: Path) -> None:
    got = install(60, tmp_path / "browser", str(tmp_path / "nowhere" / "uv"))
    assert isinstance(got, str) and got.startswith("the browser install could not start uv venv")


def test_no_time_left_installs_nothing(tmp_path: Path) -> None:
    uv = fake_uv(tmp_path, "ok")
    assert install(0, tmp_path / "browser", uv) == (
        "the browser install timed out after 0 s, at uv venv"
    )
    assert not Path(uv).with_name("calls").exists()


@pytest.mark.browser
@pytest.mark.skipif(os.geteuid() != 0, reason="installs the browser's system packages as root")
def test_real_chromium_shoots_a_page_as_the_slot_while_the_server_runs(scratch: Path) -> None:
    got = install(INSTALL_MAX_S, scratch / "browser")
    assert isinstance(got, BrowserEnv), got
    sb = ready(Sandbox(slot_identity(0), 120, 100_000, {}), scratch)
    home = sb.new_home(scratch, "home")
    (home / "index.html").write_text("<h1>Hola</h1>" + "<p>line</p>" * 2000)
    sb.hand_over(home)
    port = free_port()
    serve = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]
    server = sb.start(serve, home, home, "serve")
    try:
        time.sleep(1)
        pages = [EvidencePage(name="home", path="/", why="w")]
        argv = [
            "/usr/bin/env",
            f"PLAYWRIGHT_BROWSERS_PATH={got.browsers}",
            str(got.python),
            "-I",
            str(SCRIPT),
            f"http://127.0.0.1:{port}",
            str(home / "shots"),
            json.dumps([{"name": "home", "path": "/"}]),
            str(PAGE_MAX_HEIGHT),
            "30",
        ]
        res = sb.run(argv, home, home, "browser", reap=False)
        assert res.ok, res.output
        assert server.alive()
    finally:
        server.stop()
    desktop, mobile = read_shots(home / "shots", pages, "head")["home"]
    assert desktop.png is not None and desktop.png.startswith(PNG_MAGIC), desktop.note
    assert mobile.png is not None and mobile.png != desktop.png
