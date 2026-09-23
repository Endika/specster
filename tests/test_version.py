import subprocess
import sys

from specster import __version__


def test_version_flag_prints_package_version() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "specster", "--version"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == f"specster {__version__}"
