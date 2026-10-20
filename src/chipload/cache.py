"""Bounded process-local caching for trusted immutable data, never authority."""

from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError
from typing import Callable, TypeVar

from .model import Model

T = TypeVar("T")
DEFAULT_CACHE_BYTES = 32 * 1024 * 1024


class DataBusy(RuntimeError):
    """Bounded data loading is saturated; retry without changing domain meaning."""


def retained_size(value: object, *, stop_after: int) -> int:
    """Conservative retained heap estimate for our known model/container types.

    Shared objects count once within an entry, but again across entries. Include
    Pydantic instance storage and field sets; class/schema objects are shared
    process infrastructure. Stop as soon as the entry cannot fit the budget.
    """
    pending = [value]
    seen = set()
    size = 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        size += sys.getsizeof(item)
        if size > stop_after:
            return size
        if isinstance(item, Model):
            pending.extend(
                (
                    item.__dict__,
                    item.__pydantic_fields_set__,
                    item.__pydantic_extra__,
                    item.__pydantic_private__,
                )
            )
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, list, set, frozenset)):
            pending.extend(item)
    return size


class DataCache:
    """LRU retention plus coalesced misses; loaders must return immutable values.

    Successful values only are retained. Oversized values may be shared with
    current waiters, then discarded. A cold load never holds the cache lock.
    This bounds retention, not transient allocations or wall time in a loader.
    """

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_CACHE_BYTES,
        max_entries: int = 128,
        max_loads: int = 4,
        wait_seconds: float = 5,
    ):
        if max_bytes < 0 or max_entries < 1 or max_loads < 1 or wait_seconds <= 0:
            raise ValueError("invalid cache limits")
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.max_loads = max_loads
        self.wait_seconds = wait_seconds
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, tuple[object, int]] = OrderedDict()
        self._loading: dict[tuple, Future] = {}
        self._bytes = 0

    @property
    def usage(self) -> dict[str, int]:
        with self._lock:
            return {
                "bytes": self._bytes,
                "entries": len(self._entries),
                "loads": len(self._loading),
            }

    def get(self, key: tuple, load: Callable[[], T]) -> T:
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key][0]
            future = self._loading.get(key)
            owner = future is None
            if owner:
                if len(self._loading) >= self.max_loads:
                    raise DataBusy("data loading is at capacity; retry later")
                future = Future()
                self._loading[key] = future
        if not owner:
            try:
                return future.result(timeout=self.wait_seconds)
            except TimeoutError as error:
                if future.done():
                    # A loader's own TimeoutError is a completed failure, not
                    # evidence that validation is still running.
                    return future.result()
                raise DataBusy("data loading is still in progress; retry later") from error
        try:
            value = load()
            # Fixed allowance includes LRU/key bookkeeping in addition to the
            # measured Python object graph. Entries also have a count limit.
            weight = retained_size((key, value), stop_after=self.max_bytes) + 512
            with self._lock:
                if weight <= self.max_bytes:
                    while self._entries and (
                        self._bytes + weight > self.max_bytes
                        or len(self._entries) >= self.max_entries
                    ):
                        _, (_, removed) = self._entries.popitem(last=False)
                        self._bytes -= removed
                    self._entries[key] = value, weight
                    self._bytes += weight
                future.set_result(value)
                del self._loading[key]
            return value
        except BaseException as error:
            with self._lock:
                future.set_exception(error)
                del self._loading[key]
            raise
