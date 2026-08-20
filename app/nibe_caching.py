"""
nibe_caching.py
================
ValueCache and LRUCache — generic, self-contained caching primitives used
by EntityManager. Extracted from nibe_entity_manager.py because neither
class has any dependency on EntityManager or the bridge's domain model;
they are general-purpose data structures that happened to be defined
alongside their only consumer.

Responsibilities
-----------------
- ValueCache: rate-limits MQTT state publishes via a change-threshold and
  minimum-interval guard.
- LRUCache: a generic least-recently-used cache with eviction and hit/miss
  statistics.

What this module does NOT do
------------------------------
- No knowledge of MQTT, HA entities, or the Nibe API.
"""

import threading
import time
from collections import OrderedDict

# ============================================================================
# VALUE CACHE
# ============================================================================


class ValueCache:
    """Rate-limits MQTT state publishes via a change-threshold and minimum interval.

    Without this cache every bulk fetch would republish every entity on every
    poll cycle regardless of whether the value changed, producing unnecessary
    MQTT traffic and HA history entries.

    _cache stores the last-published raw integer value per point_id.
    _last_publish stores the timestamp of the last publish per point_id.
    These are kept separate so _cache stays a simple int lookup.
    """
    __slots__ = ('_cache', '_last_publish', '_lock')

    def __init__(self):
        self._cache        = {}   # point_id → last published raw int value
        self._last_publish = {}   # point_id → timestamp of last publish
        self._lock         = threading.Lock()

    def should_publish(
        self,
        point_id: int,
        raw_value: int,
        threshold: int,
        force: bool = False,
        min_interval: int = 30,
    ) -> bool:
        """Return True if the value warrants publishing to MQTT.

        threshold=0 (the default for any point without explicit firmware
        'change' metadata) deliberately means "never suppress on value" —
        every poll republishes once min_interval has elapsed, even with an
        unchanged value. This matches HA's expectation of continuous history
        for static sensors and is intentional, not a bug: abs(diff) >= 0 is
        always true by design here, not an oversight.
        """
        current_time = time.time()
        with self._lock:
            if force or point_id not in self._cache:
                self._cache[point_id]        = raw_value
                self._last_publish[point_id] = current_time
                return True

            if (
                point_id in self._last_publish
                and current_time - self._last_publish[point_id] < min_interval
                and not force
            ):
                return False

            old_value = self._cache[point_id]
            if abs(raw_value - old_value) >= threshold:
                self._cache[point_id]        = raw_value
                self._last_publish[point_id] = current_time
                return True
        return False

    def update(self, point_id: int, raw_value: int) -> None:
        """Update cached value without triggering a publish decision."""
        with self._lock:
            self._cache[point_id] = raw_value

    def discard(self, point_id: int) -> None:
        """Remove all cached state for a point (called on entity disable)."""
        with self._lock:
            self._cache.pop(point_id, None)
            self._last_publish.pop(point_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


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
    atomic without a lock — mirrors the locking already used by ``ValueCache``
    above for the same reason.

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

    def get(self, key):
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

    def put(self, key, value):
        """Add item to cache, removing LRU item if max_size exceeded."""
        with self._lock:
            if key in self._cache:
                # Update existing item - move to end
                self._cache.pop(key)
            elif len(self._cache) >= self.max_size:
                # Remove least recently used item
                self._cache.popitem(last=False)

            self._cache[key] = value

    def __contains__(self, key):
        with self._lock:
            return key in self._cache

    def __len__(self):
        with self._lock:
            return len(self._cache)

    def pop(self, key, default=None):
        """Remove and return item from cache. Compatible with dict.pop()."""
        with self._lock:
            if key in self._cache:
                value = self._cache.pop(key)
                return value
            return default

    def __getitem__(self, key):
        """Support dict-like access: cache[key]. Promotes to MRU and counts as a hit."""
        with self._lock:
            value = self._cache.pop(key)        # raises KeyError if absent — correct
            self._cache[key] = value            # re-insert at end (most recently used)
            self._hits += 1
            return value

    def clear(self):
        """Clear the cache."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self):
        """Return cache statistics."""
        with self._lock:
            return {
                'size': len(self._cache),
                'capacity': self.max_size,
                'hit_rate': self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0,
                'hits': self._hits,
                'misses': self._misses
            }
