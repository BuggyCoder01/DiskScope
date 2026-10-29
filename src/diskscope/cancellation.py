"""Thread-safe cooperative cancellation."""

from __future__ import annotations

import threading


class CancelToken:
    """One-way cancellation flag shared between UI and worker threads.

    ``cancel`` may be called from any thread. Long-running operations poll
    :attr:`is_cancelled` and return partial results marked incomplete.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation. Safe to call more than once."""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._event.is_set()
