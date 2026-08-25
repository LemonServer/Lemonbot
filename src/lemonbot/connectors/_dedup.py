"""Small bounded deduplication primitives used at connector boundaries."""

from __future__ import annotations

from collections import OrderedDict


class BoundedDeduplicator:
    """Remember recently accepted identifiers without unbounded memory growth.

    The core inbox remains the durable source of truth.  This guard only avoids
    enqueuing the same reconnect/redelivery burst repeatedly inside a worker.
    Calls are synchronous and intended for one asyncio event loop.
    """

    def __init__(self, capacity: int = 10_000) -> None:
        if capacity < 1:
            raise ValueError("deduplication capacity must be positive")
        self._capacity = capacity
        self._keys: OrderedDict[str, None] = OrderedDict()

    def add(self, key: str) -> bool:
        """Return ``True`` exactly when *key* was not already remembered."""

        if not key:
            raise ValueError("deduplication key must not be empty")
        if key in self._keys:
            self._keys.move_to_end(key)
            return False
        self._keys[key] = None
        if len(self._keys) > self._capacity:
            self._keys.popitem(last=False)
        return True

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def __len__(self) -> int:
        return len(self._keys)
