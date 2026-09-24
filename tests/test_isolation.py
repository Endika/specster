import os
import shutil

import pytest

from specster.isolation import CANARY_ENV, isolation_check
from specster.sandbox import scratch_dir


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root; the docker job runs --isolation-check")
def test_a_test_process_cannot_read_the_parents_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CANARY_ENV, "canary-0123456789abcdef")
    scratch = scratch_dir()
    try:
        assert isolation_check(scratch) == []
    finally:
        shutil.rmtree(scratch)


def test_the_check_fails_without_a_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CANARY_ENV, raising=False)
    scratch = scratch_dir()
    try:
        failures = isolation_check(scratch)
    finally:
        shutil.rmtree(scratch)
    assert failures and CANARY_ENV in failures[0]
