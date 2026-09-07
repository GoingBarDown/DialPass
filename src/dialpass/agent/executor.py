"""How a Tier 2 call gets off the media loop.

`choose_menu_digit` / `probe` reach out to the Realtime API and block for 1-2s.
Run inline, that freezes audio ingestion (the classifier falls behind, the FSM
stampedes on the catch-up — see the M4 notes). So `AgentSession` doesn't call
Tier 2 directly: it hands the work to an executor, keeps ingesting audio, and
picks the result up on a later tick via `poll()`.

Two implementations:

* `InlineExecutor` — runs the call synchronously inside `submit()`. Deterministic,
  no threads: the offline sim and the tests use this so a fed frame produces the
  same result every run.
* `ThreadedExecutor` — runs the call on a single background thread. Production
  (the live media handler) uses this so the WebSocket keeps draining.

Only one job is ever outstanding at a time — the FSM never asks for a menu
decision and a probe at once — so the interface is a single slot, not a queue.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Protocol

log = logging.getLogger("dialpass.agent")

# (value, error) — exactly one is set. Returned by poll() when the job is done.
Result = tuple[object | None, BaseException | None]


class Tier2Executor(Protocol):
    def submit(self, fn: Callable[[], object]) -> None:
        """Start running `fn`. Caller guarantees no job is already outstanding."""
        ...

    def poll(self) -> Result | None:
        """None while the job is still running; `(value, error)` once, when done.
        A second call before the next `submit` returns None again."""
        ...

    def close(self) -> None: ...


class InlineExecutor:
    """Synchronous — the result is ready the instant `submit` returns."""

    def __init__(self) -> None:
        self._pending: Result | None = None

    def submit(self, fn: Callable[[], object]) -> None:
        try:
            self._pending = (fn(), None)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller via poll()
            self._pending = (None, exc)

    def poll(self) -> Result | None:
        out, self._pending = self._pending, None
        return out

    def close(self) -> None:
        self._pending = None


class ThreadedExecutor:
    """One background thread. `poll()` is non-blocking."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tier2")
        self._future: Future[object] | None = None

    def submit(self, fn: Callable[[], object]) -> None:
        if self._future is not None and not self._future.done():
            log.error("tier2 executor: submit while a job is still running; dropping the old one")
        self._future = self._pool.submit(fn)

    def poll(self) -> Result | None:
        fut = self._future
        if fut is None or not fut.done():
            return None
        self._future = None
        exc = fut.exception()
        return (None, exc) if exc is not None else (fut.result(), None)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
