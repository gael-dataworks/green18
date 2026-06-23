from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

DEFAULT_DELAYS = (2.0, 5.0, 15.0)


async def async_retry(
    fn: Callable[[int, str | None], Awaitable[T]],
    *,
    max_retries: int = 3,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
    on_attempt_failed: Callable[[int, BaseException], None] | None = None,
) -> T:
    """
    Run `fn(attempt, last_err)` up to `max_retries`+1 times.
    """
    last_err_str: str | None = None
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn(attempt, last_err_str)
        except Exception as exc:
            last_exc = exc
            last_err_str = f"{type(exc).__name__}: {exc}"
            if on_attempt_failed is not None:
                on_attempt_failed(attempt, exc)
            if attempt >= max_retries:
                break
            delay = delays[min(attempt, len(delays) - 1)]
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
