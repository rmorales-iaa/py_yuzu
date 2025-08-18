#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path  # (kept since other modules may import this file similarly)

import multiprocessing
import multiprocessing.queues


class SharedCounter:
    """A synchronized shared counter based on multiprocessing.Value."""

    def __init__(self, n: int = 0):
        # 'i' -> signed int; Value provides an internal lock
        self._value = multiprocessing.Value("i", n)

    def increment(self, n: int = 1) -> None:
        """Atomically add n (default 1)."""
        with self._value.get_lock():
            self._value.value += n

    @property
    def value(self) -> int:
        """Current value."""
        return self._value.value


class Queue(multiprocessing.queues.Queue):
    """A portable multiprocessing Queue with reliable qsize()/empty().

    On some Unix platforms, standard Queue.qsize() can raise NotImplementedError.
    We keep a SharedCounter updated on successful put/get to provide
    reliable qsize() and empty().
    """

    def __init__(self, maxsize: int = 0, *, ctx: multiprocessing.context.BaseContext | None = None):
        if ctx is None:
            ctx = multiprocessing.get_context()  # use default context
        super().__init__(maxsize, ctx=ctx)
        self._size = SharedCounter(0)

    # NOTE: We only adjust the counter if the operation succeeds.

    def put(self, obj, *args, **kwargs) -> None:
        super().put(obj, *args, **kwargs)
        self._size.increment(+1)

    def get(self, *args, **kwargs):
        item = super().get(*args, **kwargs)
        self._size.increment(-1)
        return item

    def qsize(self) -> int:
        """Reliable size (does not depend on sem_getvalue)."""
        return self._size.value

    def empty(self) -> bool:
        """Reliable emptiness check."""
        return self.qsize() == 0

    def clear(self) -> None:
        """Remove all elements from the Queue (non-atomic)."""
        while not self.empty():
            # Best-effort drain; safe because we decrement only on successful get()
            try:
                self.get_nowait()
            except Exception:
                break
