import subprocess
import sys

from specster import __version__
from specster.__main__ import USAGE


def test_version_flag_prints_package_version() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "specster", "--version"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == f"specster {__version__}"


def test_unknown_flag_prints_usage_and_exits_2() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "specster", "--bogus"], capture_output=True, text=True
    )
    assert out.returncode == 2
    assert "--version" in out.stderr
    assert "--self-check" in out.stderr
    assert "--isolation-check" in out.stderr
    assert "--help" in out.stderr


def test_help_flag_prints_usage_and_exits_0() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "specster", "--help"], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert out.stdout.strip() == USAGE
    assert out.stderr == ""
