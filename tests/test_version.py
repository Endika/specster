import subprocess
import sys

from specster import __version__


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
