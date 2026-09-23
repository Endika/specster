import time
from collections.abc import Callable


def with_retries[T](
    fn: Callable[[], T],
    retryable: Callable[[Exception], bool],
    attempts: int,
    sleep: Callable[[float], None] = time.sleep,
    base: float = 1.0,
) -> T:
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1 or not retryable(e):
                raise
            sleep(min(base * 2**i, 30.0))
    raise AssertionError("unreachable")
