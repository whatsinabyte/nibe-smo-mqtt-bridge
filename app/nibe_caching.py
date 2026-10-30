"""
nibe_caching.py
================
LRUCache — a generic, self-contained caching primitive used by
EntityManager. Extracted from nibe_entity_manager.py because it has no
dependency on EntityManager or the bridge's domain model.

Responsibilities
-----------------
- LRUCache: a generic least-recently-used cache with eviction and hit/miss
  statistics.

What this module does NOT do
------------------------------
- No knowledge of MQTT, HA entities, or the Nibe API.
"""

import threading
from collections import OrderedDict
from typing import Any


class LRUCache:
    """Memory-efficient LRU (Least Recently Used) cache with automatic cleanup.

    This cache automatically removes least recently used items when the cache
    exceeds its maximum size, making it ideal for caching data where some
    items are more important than others.

    Thread-safe: EntityManager uses this as ``_point_string_cache``, which is
    mutated both from the main poll thread (``_fetch_bulk_data`` /
    ``_get_cached_entity_type``) and from the write/management executor
    threads (``disable_entity`` → ``_deindex_point``). ``put()`` in
    particular does a check-then-evict-then-insert sequence that is not
    atomic without a lock.

    Parameters
    ----------
    max_size : int
        Maximum number of items to keep in cache. When exceeded, LRU items are removed.
    """

    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._cache: OrderedDict = OrderedDict()  # OrderedDict maintains insertion order
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        """Get item from cache, marking it as recently used."""
        with self._lock:
            try:
                value = self._cache.pop(key)
                self._cache[key] = value  # Move to end (most recently used)
                self._hits += 1
                return value
            except KeyError:
                self._misses += 1
                return None

    def put(self, key: Any, value: Any) -> None:
        """Add item to cache, removing LRU item if max_size exceeded."""
        with self._lock:
            if key in self._cache:
                # Update existing item - move to end
                self._cache.pop(key)
            elif len(self._cache) >= self.max_size:
                # Remove least recently used item
                self._cache.popitem(last=False)

            self._cache[key] = value

    def __contains__(self, key: Any) -> bool:
        with self._lock:
            return key in self._cache

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def pop(self, key: Any, default: Any = None) -> Any:
        """Remove and return item from cache. Compatible with dict.pop()."""
        with self._lock:
            if key in self._cache:
                value = self._cache.pop(key)
                return value
            return default

    def __getitem__(self, key: Any) -> Any:
        """Support dict-like access: cache[key]. Promotes to MRU and counts as a hit."""
        with self._lock:
            value = self._cache.pop(key)  # raises KeyError if absent — correct
            self._cache[key] = value  # re-insert at end (most recently used)
            self._hits += 1
            return value

    def clear(self) -> None:
        """Clear the cache."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self) -> dict:
        """Return cache statistics."""
        with self._lock:
            return {
                "size": len(self._cache),
                "capacity": self.max_size,
                "hit_rate": self._hits / (self._hits + self._misses)
                if (self._hits + self._misses) > 0
                else 0,
                "hits": self._hits,
                "misses": self._misses,
            }
