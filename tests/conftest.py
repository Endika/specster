import shutil
import tempfile
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

# Imported here under a filter of its own: pytest lets a command-line `-W error` override any
# ini filterwarnings entry, and google-genai trips a Python 3.14 deprecation at import time.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="'_UnionGenericAlias' is deprecated",
        category=DeprecationWarning,
        module=r"google\.genai(\.|$)",
    )
    import google.genai.types  # noqa: F401


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A sandbox TMPDIR makes tmp_path longer than the 108 bytes an AF_UNIX path allows."""
    path = Path(tempfile.mkdtemp(prefix="sp-", dir="/tmp"))
    yield path
    shutil.rmtree(path)
