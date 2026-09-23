import pytest

from specster.llm.retry import with_retries


def test_retries_retryable_errors_with_exponential_backoff() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("429")
        return "ok"

    assert with_retries(flaky, lambda e: "429" in str(e), attempts=4, sleep=sleeps.append) == "ok"
    assert sleeps == [1.0, 2.0]


def _raise(error: Exception) -> str:
    raise error


def test_non_retryable_error_is_raised_immediately() -> None:
    sleeps: list[float] = []
    with pytest.raises(ValueError):
        with_retries(lambda: _raise(ValueError("400")), lambda _: False, 4, sleeps.append)
    assert sleeps == []


def test_gives_up_after_attempts() -> None:
    sleeps: list[float] = []
    with pytest.raises(RuntimeError):
        with_retries(lambda: _raise(RuntimeError("503")), lambda _: True, 2, sleeps.append)
    assert sleeps == [1.0]


def test_backoff_is_capped_at_thirty_seconds() -> None:
    sleeps: list[float] = []
    with pytest.raises(RuntimeError):
        with_retries(lambda: _raise(RuntimeError("503")), lambda _: True, 8, sleeps.append)
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
