"""
test_caching.py
================
Tests for ValueCache and LRUCache (app/nibe_caching.py) — generic,
self-contained caching primitives with no dependency on EntityManager.
Moved out of test_entity_manager.py, where they originally lived alongside
the classes' only consumer.

EntityManager integration tests that happen to exercise ValueCache/LRUCache
as part of a larger flow (e.g. TestProcessAndPublishState,
TestChangeThresholdWiring, TestValueCacheDeduplication) remain in
test_entity_manager.py — they are testing EntityManager's wiring, not the
cache classes themselves.
"""

import unittest
from typing import ClassVar

from conftest import _nibe_point_id
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)


class TestLRUCacheHypothesisProperties(unittest.TestCase):
    """Hypothesis properties for LRUCache."""

    @given(
        st.integers(min_value=1, max_value=100),
        st.lists(
            st.tuples(st.integers(min_value=0, max_value=200), st.integers()),
            min_size=0,
            max_size=200,
        ),
    )
    def test_len_never_exceeds_max_size(self, max_size, operations):
        """len(cache) must never exceed max_size after any sequence of puts."""
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        for key, value in operations:
            cache.put(key, value)
        self.assertLessEqual(len(cache), max_size)

    @given(
        st.integers(min_value=1, max_value=50),
        st.integers(min_value=0, max_value=1000),
        st.integers(),
    )
    def test_get_after_put_returns_value(self, max_size, key, value):
        """get(k) immediately after put(k, v) must return v."""
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        cache.put(key, value)
        self.assertEqual(cache.get(key), value)

    @given(
        st.integers(min_value=1, max_value=50),
        st.integers(min_value=0, max_value=1000),
        st.integers(),
    )
    def test_contains_consistent_with_get(self, max_size, key, value):
        """key in cache ↔ cache.get(key) is not None (sentinel)."""
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        cache.put(key, value)
        self.assertIn(key, cache)
        self.assertIsNotNone(cache.get(key))

    @given(st.integers(min_value=1, max_value=50))
    def test_get_missing_key_returns_none(self, max_size):
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        self.assertIsNone(cache.get(99999))

    @given(
        st.integers(min_value=2, max_value=20),
        st.lists(st.integers(min_value=0, max_value=1000), min_size=2, max_size=2, unique=True),
    )
    def test_eviction_removes_oldest_when_full(self, max_size, keys):
        """When cache is full and a new key is inserted, the oldest inserted
        key (never accessed since insertion) must be evicted."""
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        first_key = keys[0]
        # Fill cache to capacity, first_key goes in first
        cache.put(first_key, "first")
        for i in range(1, max_size):
            cache.put(i + 10000, i)  # unique keys outside our range
        # Cache is now full. Add one more — first_key should be evicted
        cache.put(keys[1], "new")
        self.assertNotIn(first_key, cache)

    @given(
        st.integers(min_value=1, max_value=50),
        st.integers(min_value=0, max_value=1000),
        st.integers(),
    )
    def test_put_same_key_updates_value(self, max_size, key, value):
        """Putting the same key twice must update to the new value."""
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        cache.put(key, "old")
        cache.put(key, value)
        self.assertEqual(cache.get(key), value)

    @given(
        st.integers(min_value=1, max_value=50),
        st.integers(min_value=0, max_value=1000),
        st.integers(),
    )
    def test_pop_removes_key(self, max_size, key, value):
        """pop(k) must remove k from the cache."""
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        cache.put(key, value)
        popped = cache.pop(key)
        self.assertEqual(popped, value)
        self.assertNotIn(key, cache)

    @given(st.integers(min_value=1, max_value=50))
    def test_clear_empties_cache(self, max_size):
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        for i in range(max_size):
            cache.put(i, i)
        cache.clear()
        self.assertEqual(len(cache), 0)


class TestValueCacheHypothesisProperties(unittest.TestCase):
    """Hypothesis properties for ValueCache.should_publish."""

    @given(_nibe_point_id, st.integers(min_value=-32768, max_value=32767))
    def test_first_call_always_publishes(self, point_id, raw_value):
        """First call for any point_id must always return True."""
        from nibe_caching import ValueCache

        cache = ValueCache()
        self.assertTrue(cache.should_publish(point_id, raw_value, threshold=1))

    def test_default_min_interval_is_exactly_thirty_seconds(self):
        """min_interval's default is 30. threshold=0 means every call
        republishes once min_interval has elapsed, regardless of value —
        at 29s elapsed a call must still be suppressed (< 30); at 31s
        elapsed it must not be."""
        from unittest.mock import patch

        from nibe_caching import ValueCache

        cache = ValueCache()
        with patch("nibe_caching.time.time", return_value=1000.0):
            cache.should_publish(1, 100, threshold=0)
        with patch("nibe_caching.time.time", return_value=1000.0 + 29):
            self.assertFalse(cache.should_publish(1, 100, threshold=0))
        with patch("nibe_caching.time.time", return_value=1000.0 + 31):
            self.assertTrue(cache.should_publish(1, 100, threshold=0))

        # Exactly at 30s elapsed (fresh cache, so this check's own call
        # doesn't get skewed by the state left behind above): `< min_interval`
        # is False only when min_interval is exactly 30 — a default of 29
        # or 31 would flip this specific boundary check.
        cache2 = ValueCache()
        with patch("nibe_caching.time.time", return_value=1000.0):
            cache2.should_publish(1, 100, threshold=0)
        with patch("nibe_caching.time.time", return_value=1000.0 + 30):
            self.assertTrue(cache2.should_publish(1, 100, threshold=0))

    @given(
        _nibe_point_id,
        st.integers(min_value=-32768, max_value=32767),
        st.integers(min_value=1, max_value=100),
    )
    def test_same_value_no_change_suppresses(self, point_id, raw_value, threshold):
        """Same value, within min_interval → must return False."""
        from nibe_caching import ValueCache

        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=threshold)
        result = cache.should_publish(point_id, raw_value, threshold=threshold, min_interval=9999)
        self.assertFalse(result)

    @given(
        _nibe_point_id,
        st.integers(min_value=-32768, max_value=32767),
        st.integers(min_value=1, max_value=100),
    )
    def test_large_change_always_publishes(self, point_id, raw_value, threshold):
        """Change ≥ threshold (with no interval restriction) must return True."""
        from nibe_caching import ValueCache

        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=threshold, min_interval=0)
        new_value = raw_value + threshold
        result = cache.should_publish(point_id, new_value, threshold=threshold, min_interval=0)
        self.assertTrue(result)

    @given(_nibe_point_id, st.integers(min_value=-32768, max_value=32767))
    def test_force_always_publishes(self, point_id, raw_value):
        """force=True must always return True regardless of value or interval."""
        from nibe_caching import ValueCache

        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=1, min_interval=9999)
        result = cache.should_publish(
            point_id, raw_value, threshold=1, min_interval=9999, force=True
        )
        self.assertTrue(result)

    @given(
        st.integers(min_value=0, max_value=99999), st.integers(min_value=-32768, max_value=32767)
    )
    def test_after_update_same_value_suppresses_publish(self, point_id, raw_value):
        """After update(pid, v), should_publish(pid, v) returns False — value unchanged."""
        from nibe_caching import ValueCache

        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=1, min_interval=0)
        cache.update(point_id, raw_value)
        result = cache.should_publish(point_id, raw_value, threshold=1, min_interval=0)
        self.assertFalse(result)

    @given(
        st.integers(min_value=0, max_value=99999), st.integers(min_value=-32768, max_value=32767)
    )
    def test_after_discard_next_publish_always_true(self, point_id, raw_value):
        """After discard(pid), the next should_publish always returns True."""
        from nibe_caching import ValueCache

        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=1, min_interval=9999)
        cache.discard(point_id)
        result = cache.should_publish(point_id, raw_value, threshold=1, min_interval=9999)
        self.assertTrue(result)

    @given(st.integers(min_value=0, max_value=99999))
    def test_discard_unknown_pid_never_raises(self, point_id):
        """discard on a point_id never seen must not raise."""
        from nibe_caching import ValueCache

        cache = ValueCache()
        cache.discard(point_id)  # must not raise

    @given(st.integers(min_value=0, max_value=99999), st.integers())
    def test_len_matches_cache_after_publish(self, point_id, raw_value):
        """len(cache) must reflect the number of tracked point_ids, via the
        public locked API rather than reaching into the private _cache dict."""
        from nibe_caching import ValueCache

        cache = ValueCache()
        self.assertEqual(len(cache), 0)
        cache.should_publish(point_id, raw_value, threshold=1, min_interval=9999)
        self.assertEqual(len(cache), 1)
        cache.discard(point_id)
        self.assertEqual(len(cache), 0)


class TestLRUCacheGetStatsProperties(unittest.TestCase):
    """Hypothesis properties for LRUCache.get_stats."""

    @given(
        st.integers(min_value=1, max_value=50),
        st.lists(st.tuples(st.integers(min_value=0, max_value=200), st.integers()), max_size=100),
    )
    def test_size_always_leq_capacity(self, max_size, operations):
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        for k, v in operations:
            cache.put(k, v)
        stats = cache.get_stats()
        self.assertLessEqual(stats["size"], stats["capacity"])

    @given(
        st.integers(min_value=1, max_value=50),
        st.lists(st.tuples(st.integers(min_value=0, max_value=200), st.integers()), max_size=50),
    )
    def test_size_equals_len_cache(self, max_size, operations):
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        for k, v in operations:
            cache.put(k, v)
        stats = cache.get_stats()
        self.assertEqual(stats["size"], len(cache))

    @given(
        st.integers(min_value=1, max_value=50),
        st.lists(st.integers(min_value=0, max_value=100), max_size=50),
        st.lists(st.integers(min_value=0, max_value=100), max_size=50),
    )
    def test_hit_rate_always_in_0_1(self, max_size, put_keys, get_keys):
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        for k in put_keys:
            cache.put(k, k)
        for k in get_keys:
            cache.get(k)
        stats = cache.get_stats()
        self.assertGreaterEqual(stats["hit_rate"], 0.0)
        self.assertLessEqual(stats["hit_rate"], 1.0)

    @given(
        st.integers(min_value=1, max_value=50),
        st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=50),
        st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=50),
    )
    def test_hits_plus_misses_equals_total_gets(self, max_size, put_keys, get_keys):
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        for k in put_keys:
            cache.put(k, k)
        for k in get_keys:
            cache.get(k)
        stats = cache.get_stats()
        self.assertEqual(stats["hits"] + stats["misses"], len(get_keys))

    @given(
        st.integers(min_value=1, max_value=50),
        st.lists(st.tuples(st.integers(min_value=0, max_value=100), st.integers()), max_size=30),
    )
    def test_stats_reset_after_clear(self, max_size, operations):
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        for k, v in operations:
            cache.put(k, v)
            cache.get(k)
        cache.clear()
        stats = cache.get_stats()
        self.assertEqual(stats["size"], 0)
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["hit_rate"], 0)

    @given(st.integers(min_value=1, max_value=100))
    def test_capacity_always_equals_max_size(self, max_size):
        from nibe_caching import LRUCache

        cache = LRUCache(max_size=max_size)
        self.assertEqual(cache.get_stats()["capacity"], max_size)


class TestValueCache(unittest.TestCase):
    def setUp(self):
        from nibe_caching import ValueCache

        self.cache = ValueCache()

    def test_first_call_always_publishes(self):
        self.assertTrue(self.cache.should_publish(1, 100, threshold=1))

    def test_same_value_suppressed_by_min_interval(self):
        self.cache.should_publish(1, 100, threshold=1, min_interval=30)
        self.assertFalse(self.cache.should_publish(1, 100, threshold=1, min_interval=30))

    def test_change_within_threshold_suppressed(self):
        self.cache.should_publish(1, 100, threshold=5, min_interval=0)
        self.assertFalse(self.cache.should_publish(1, 103, threshold=5, min_interval=0))

    def test_change_exceeds_threshold_published(self):
        self.cache.should_publish(1, 100, threshold=5, min_interval=0)
        self.assertTrue(self.cache.should_publish(1, 106, threshold=5, min_interval=0))

    def test_unchanged_value_still_published_with_zero_threshold(self):
        """change=0 (the default for any point without explicit firmware
        'change' metadata) is deliberate 'never suppress' behavior — static
        sensors must keep publishing every poll for continuous HA history,
        matching test_zero_change_threshold_always_publishes in
        test_entity_manager_state.py. Not a bug: do not 'fix' should_publish
        to suppress unchanged values when threshold=0."""
        self.cache.should_publish(1, 100, threshold=0, min_interval=0)
        self.assertTrue(self.cache.should_publish(1, 100, threshold=0, min_interval=0))

    def test_force_overrides_suppression(self):
        self.cache.should_publish(1, 100, threshold=1, min_interval=30)
        self.assertTrue(self.cache.should_publish(1, 100, threshold=1, min_interval=30, force=True))

    def test_discard_makes_next_fresh(self):
        self.cache.should_publish(1, 100, threshold=1)
        self.cache.discard(1)
        self.assertTrue(self.cache.should_publish(1, 100, threshold=1))

    def test_multiple_points_independent(self):
        self.cache.should_publish(1, 100, threshold=1)
        self.cache.should_publish(2, 200, threshold=1)
        self.assertFalse(self.cache.should_publish(1, 100, threshold=1, min_interval=0))
        self.assertTrue(self.cache.should_publish(2, 210, threshold=5, min_interval=0))

    def test_update_does_not_publish(self):
        self.cache.should_publish(1, 100, threshold=1, min_interval=30)
        self.cache.update(1, 200)
        self.assertFalse(self.cache.should_publish(1, 200, threshold=1, min_interval=30))

    def test_changed_value_within_min_interval_still_suppressed(self):
        """A CHANGED value (well past threshold) arriving before min_interval
        has elapsed must still be suppressed by the interval check itself —
        distinct from test_same_value_suppressed_by_min_interval, which uses
        an unchanged value and is actually suppressed by the threshold check
        regardless of the interval logic (abs(0) < threshold is always
        false-ish there), never exercising the interval branch at all."""
        from unittest.mock import patch

        with patch("nibe_caching.time.time", side_effect=[1000.0, 1005.0]):
            self.cache.should_publish(1, 100, threshold=1, min_interval=30)
            # 5s later, value changed by 50 (way past threshold=1) — but
            # only 5s < min_interval=30 have elapsed, so must be suppressed.
            result = self.cache.should_publish(1, 150, threshold=1, min_interval=30)
        self.assertFalse(result)

    def test_changed_value_after_min_interval_elapses_publishes(self):
        """The interval boundary must use strict '<' (not '<=') — a change
        arriving exactly at or past min_interval must publish."""
        from unittest.mock import patch

        with patch("nibe_caching.time.time", side_effect=[1000.0, 1030.0]):
            self.cache.should_publish(1, 100, threshold=1, min_interval=30)
            # Exactly 30s later == min_interval — must NOT be suppressed.
            result = self.cache.should_publish(1, 150, threshold=1, min_interval=30)
        self.assertTrue(result)

    def test_default_min_interval_throttles_rapid_republish(self):
        """Calling should_publish without an explicit min_interval must use
        the real default (30s) — a change arriving 10s later (well under
        any plausible default) must still be suppressed."""
        from unittest.mock import patch

        with patch("nibe_caching.time.time", side_effect=[1000.0, 1010.0]):
            self.cache.should_publish(1, 100, threshold=1)  # no min_interval given
            result = self.cache.should_publish(1, 150, threshold=1)  # no min_interval given
        self.assertFalse(result)

    def test_unchanged_value_not_republished_even_at_zero_interval(self):
        # ValueCache requires a threshold-exceeding change OR force=True.
        # Zero min_interval alone is not enough to re-publish an identical value.
        self.cache.should_publish(1, 100, threshold=1, min_interval=0)
        self.assertFalse(self.cache.should_publish(1, 100, threshold=1, min_interval=0))


class TestLRUCache(unittest.TestCase):
    def setUp(self):
        from nibe_caching import LRUCache

        self.cache = LRUCache(max_size=3)

    def test_put_new_item(self):
        self.cache.put("a", 1)
        self.assertEqual(self.cache.get("a"), 1)

    def test_put_updates_existing_item(self):
        """put() with an already-present key updates in place (line 250)."""
        self.cache.put("a", 1)
        self.cache.put("a", 99)
        self.assertEqual(self.cache.get("a"), 99)
        self.assertEqual(len(self.cache), 1)  # no duplicate

    def test_put_evicts_lru_at_capacity(self):
        """put() evicts the least-recently-used entry when at max_size (line 258)."""
        self.cache.put("a", 1)
        self.cache.put("b", 2)
        self.cache.put("c", 3)
        self.cache.put("d", 4)  # should evict 'a' (oldest)
        self.assertNotIn("a", self.cache)
        self.assertIn("d", self.cache)
        self.assertEqual(len(self.cache), 3)

    def test_pop_existing_key_returns_value(self):
        """pop() on a present key returns the value and removes it (lines 276-278)."""
        self.cache.put("x", 42)
        result = self.cache.pop("x")
        self.assertEqual(result, 42)
        self.assertNotIn("x", self.cache)

    def test_pop_missing_key_returns_default(self):
        self.assertIsNone(self.cache.pop("missing"))
        self.assertEqual(self.cache.pop("missing", "fallback"), "fallback")

    def test_getitem_promotes_to_mru(self):
        """cache[key] promotes the item to most-recently-used (line 282 path)."""
        self.cache.put("a", 1)
        self.cache.put("b", 2)
        self.cache.put("c", 3)
        _ = self.cache["a"]  # promote 'a' to MRU
        self.cache.put("d", 4)  # should evict 'b' (now LRU), not 'a'
        self.assertIn("a", self.cache)
        self.assertNotIn("b", self.cache)

    def test_getitem_raises_keyerror_for_absent_key(self):
        """cache[key] raises KeyError when key is absent (line 282)."""
        with self.assertRaises(KeyError):
            _ = self.cache["no_such_key"]

    def test_getitem_counts_as_hit(self):
        self.cache.put("a", 1)
        _ = self.cache["a"]
        stats = self.cache.get_stats()
        self.assertEqual(stats["hits"], 1)

    def test_getitem_hits_accumulate_across_multiple_accesses(self):
        """Repeated cache[key] access must INCREMENT the hit counter, not
        reset it to 1 each time — a single-access test can't distinguish
        `self._hits += 1` from a buggy `self._hits = 1`, since both give 1
        starting from 0."""
        self.cache.put("a", 1)
        _ = self.cache["a"]
        _ = self.cache["a"]
        _ = self.cache["a"]
        stats = self.cache.get_stats()
        self.assertEqual(stats["hits"], 3)

    def test_getitem_value_correct_on_repeated_access(self):
        """cache[key] must return the REAL stored value on a second access,
        not a value corrupted by the MRU-promotion re-insert step (which
        re-inserts under the same key after popping — a bug there would
        silently replace the value with None, invisible on the very access
        that reads it since the return value is captured before re-insert,
        but corrupting the cache for the next read)."""
        self.cache.put("a", "real-value")
        first = self.cache["a"]
        second = self.cache["a"]
        self.assertEqual(first, "real-value")
        self.assertEqual(second, "real-value")

    def test_get_value_correct_on_repeated_access(self):
        """Same as above but for the .get() method's MRU-promotion path."""
        self.cache.put("a", "real-value")
        first = self.cache.get("a")
        second = self.cache.get("a")
        self.assertEqual(first, "real-value")
        self.assertEqual(second, "real-value")

    def test_get_hit_rate(self):
        self.cache.put("a", 1)
        self.cache.get("a")  # hit
        self.cache.get("z")  # miss
        stats = self.cache.get_stats()
        self.assertAlmostEqual(stats["hit_rate"], 0.5)

    def test_hit_rate_at_exactly_one_total_access(self):
        """hit_rate must reflect the real ratio even with exactly ONE total
        access (hits+misses==1) — the '> 0' guard against division by zero
        must not accidentally require MORE than one access to kick in."""
        self.cache.put("a", 1)
        self.cache.get("a")  # exactly one access: a single hit
        stats = self.cache.get_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 0)
        self.assertAlmostEqual(stats["hit_rate"], 1.0)

    def test_default_max_size_is_1000(self):
        """LRUCache() with no explicit max_size must use the real default
        of 1000 — putting exactly 1000 items must not evict any of them,
        and the 1001st must evict the oldest."""
        from nibe_caching import LRUCache

        cache = LRUCache()
        for i in range(1000):
            cache.put(i, i)
        self.assertEqual(len(cache), 1000)
        self.assertIn(0, cache)
        cache.put(1000, 1000)
        self.assertEqual(len(cache), 1000)
        self.assertNotIn(0, cache)  # oldest evicted
        self.assertIn(1000, cache)

    def test_clear(self):
        self.cache.put("a", 1)
        self.cache.clear()
        self.assertEqual(len(self.cache), 0)


class LRUCacheMachine(RuleBasedStateMachine):
    """Stateful test machine for LRUCache.

    Explores arbitrary put/get/pop/clear sequences and checks invariants
    after every operation. Key invariants:
      1. len(cache) always ≤ max_size (capacity never exceeded)
      2. After put(k,v): k in cache is True
      3. After pop(k): k in cache is False
      4. hit_rate always in [0.0, 1.0]
      5. hits + misses == total get() calls
      6. After clear(): len == 0 and hit_rate == 0
    """

    CAPACITY = 10
    KEYS: ClassVar[list] = list(range(20))  # more keys than capacity to force eviction

    @initialize()
    def setup(self):
        from nibe_caching import LRUCache

        self.cache = LRUCache(max_size=self.CAPACITY)
        self.total_gets = 0

    @rule(key=st.sampled_from(KEYS), value=st.integers(min_value=0, max_value=100))
    def put(self, key, value):
        self.cache.put(key, value)

    @rule(key=st.sampled_from(KEYS))
    def get(self, key):
        self.cache.get(key)
        self.total_gets += 1

    @rule(key=st.sampled_from(KEYS))
    def pop(self, key):
        self.cache.pop(key, None)

    @rule()
    def clear(self):
        self.cache.clear()
        self.total_gets = 0

    @rule(key=st.sampled_from(KEYS), value=st.integers())
    def put_then_get_is_consistent(self, key, value):
        """After put(k, v), get(k) must return v."""
        self.cache.put(key, value)
        result = self.cache.get(key)
        self.total_gets += 1
        assert result == value, f"get({key}) returned {result!r}, expected {value!r}"

    @rule(key=st.sampled_from(KEYS))
    def getitem_dict_access(self, key):
        """cache[key] (nibe_caching.py:160-166) has semantics distinct from
        get(key): it raises KeyError on a miss instead of returning None, and
        a miss is NOT counted in _misses at all (no except branch touches the
        counters) — whereas get() always counts every call as a hit or a
        miss. Neither branch was previously exercised by this machine."""
        present = key in self.cache
        stats_before = self.cache.get_stats()
        if present:
            value = self.cache[key]
            # Cross-check against get(), an independently-verified method
            # reading the same underlying store — a real __getitem__ bug
            # (wrong value, no promotion) would surface as a mismatch here.
            readback = self.cache.get(key)
            assert value == readback, f"cache[{key}]={value!r} but cache.get({key})={readback!r}"
            self.total_gets += 2  # __getitem__ hit + get() hit
            stats_after = self.cache.get_stats()
            assert stats_after["hits"] == stats_before["hits"] + 2, (
                f"expected +2 hits from __getitem__+get() on present key "
                f"{key}, got {stats_before['hits']} -> {stats_after['hits']}"
            )
            assert stats_after["misses"] == stats_before["misses"], (
                "a present-key __getitem__/get() must not touch misses"
            )
        else:
            try:
                self.cache[key]
            except KeyError:
                pass
            else:
                raise AssertionError(f"cache[{key}] should have raised KeyError for an absent key")
            stats_after = self.cache.get_stats()
            assert stats_after["hits"] == stats_before["hits"], (
                "a missing-key __getitem__ must not touch hits"
            )
            assert stats_after["misses"] == stats_before["misses"], (
                "a missing-key __getitem__ must NOT increment misses "
                "(unlike get(), it has no except branch — nibe_caching.py:160-166)"
            )

    @invariant()
    def size_never_exceeds_capacity(self):
        assert len(self.cache) <= self.CAPACITY, (
            f"LRUCache size {len(self.cache)} exceeded capacity {self.CAPACITY}"
        )

    @invariant()
    def size_matches_get_stats(self):
        assert len(self.cache) == self.cache.get_stats()["size"]

    @invariant()
    def hit_rate_in_0_1(self):
        rate = self.cache.get_stats()["hit_rate"]
        assert 0.0 <= rate <= 1.0, f"hit_rate {rate} out of [0, 1]"

    @invariant()
    def hits_plus_misses_equals_gets(self):
        stats = self.cache.get_stats()
        assert stats["hits"] + stats["misses"] == self.total_gets, (
            f"hits({stats['hits']}) + misses({stats['misses']}) != total_gets({self.total_gets})"
        )

    @invariant()
    def capacity_constant(self):
        assert self.cache.get_stats()["capacity"] == self.CAPACITY


LRUCacheStatefulTest = LRUCacheMachine.TestCase


class ValueCacheMachine(RuleBasedStateMachine):
    """Stateful test machine for ValueCache.

    Key invariants:
      1. _cache and _last_publish always have the same keys
      2. After discard(pid): pid not in _cache and not in _last_publish
      3. After should_publish returns True: value is stored in _cache
      4. should_publish(same_value, same_pid, threshold=1, min_interval=9999)
         always returns False immediately after a True
    """

    PIDS: ClassVar[list] = [100, 200, 300]
    THRESH = 5

    @initialize()
    def setup(self):
        from nibe_caching import ValueCache

        self.cache = ValueCache()
        self.last_published = {}  # pid → last value that caused True
        # Mirrors of the real ValueCache internals, maintained incrementally by
        # every rule below (including update_cache, which populates _cache
        # without touching _last_publish — see nibe_caching.py:76-79).
        self.model_cache_keys = set()  # mirrors self.cache._cache.keys()
        self.model_last_publish_keys = set()  # mirrors self.cache._last_publish.keys()
        self.model_cache_values = {}  # mirrors self.cache._cache values

    @rule(
        pid=st.sampled_from(PIDS),
        value=st.integers(min_value=0, max_value=100),
    )
    def should_publish_normal(self, pid, value):
        result = self.cache.should_publish(pid, value, threshold=self.THRESH, min_interval=0)
        if result:
            self.last_published[pid] = value
            self.model_cache_keys.add(pid)
            self.model_last_publish_keys.add(pid)
            self.model_cache_values[pid] = value

    @rule(pid=st.sampled_from(PIDS))
    def discard(self, pid):
        self.cache.discard(pid)
        self.last_published.pop(pid, None)
        self.model_cache_keys.discard(pid)
        self.model_last_publish_keys.discard(pid)
        self.model_cache_values.pop(pid, None)

    @rule(
        pid=st.sampled_from(PIDS),
        value=st.integers(min_value=0, max_value=100),
    )
    def force_publish(self, pid, value):
        result = self.cache.should_publish(
            pid, value, threshold=self.THRESH, force=True, min_interval=0
        )
        assert result is True, "force=True must always return True"
        self.last_published[pid] = value
        self.model_cache_keys.add(pid)
        self.model_last_publish_keys.add(pid)
        self.model_cache_values[pid] = value

    @rule(pid=st.sampled_from(PIDS))
    def after_discard_next_always_true(self, pid):
        """After discard, should_publish must return True for any value."""
        self.cache.discard(pid)
        result = self.cache.should_publish(pid, 42, threshold=self.THRESH, min_interval=9999)
        assert result is True, f"After discard({pid}), should_publish returned False"
        self.last_published[pid] = 42
        self.model_cache_keys.discard(pid)
        self.model_cache_keys.add(pid)
        self.model_last_publish_keys.discard(pid)
        self.model_last_publish_keys.add(pid)
        self.model_cache_values[pid] = 42

    @rule(
        pid=st.sampled_from(PIDS),
        value=st.integers(min_value=0, max_value=100),
    )
    def update_cache(self, pid, value):
        """update() must seed _cache without ever touching _last_publish.

        This is the 199->203 code path (nibe_caching.py:65,76-79): a point
        can be present in _cache with no corresponding _last_publish entry,
        which is exactly the scenario that used to require a dedicated
        non-stateful regression test (TestValueCacheUpdateThenPublish) to
        catch, because no rule here ever called update() before.
        """
        self.cache.update(pid, value)
        self.model_cache_keys.add(pid)
        self.model_cache_values[pid] = value
        # _last_publish is deliberately left untouched by update().

    # ── Invariants ───────────────────────────────────────────────────────────

    @invariant()
    def cache_keys_match_model(self):
        """_cache's key set must match everything we ever put into it (via
        should_publish/force_publish/update_cache) minus discards."""
        cache_keys = set(self.cache._cache.keys())
        assert cache_keys == self.model_cache_keys, (
            f"_cache keys {cache_keys} != expected {self.model_cache_keys}"
        )

    @invariant()
    def last_publish_keys_match_model(self):
        """_last_publish's key set must match only the pids that actually
        went through a successful should_publish/force call — update() alone
        must never add a key here."""
        publish_keys = set(self.cache._last_publish.keys())
        assert publish_keys == self.model_last_publish_keys, (
            f"_last_publish keys {publish_keys} != expected {self.model_last_publish_keys}"
        )

    @invariant()
    def last_publish_keys_subset_of_cache_keys(self):
        """Every point with a _last_publish entry must also have a _cache
        entry (should_publish always sets both together)."""
        cache_keys = set(self.cache._cache.keys())
        publish_keys = set(self.cache._last_publish.keys())
        assert publish_keys <= cache_keys, (
            f"_last_publish keys {publish_keys} not a subset of _cache keys {cache_keys}"
        )

    @invariant()
    def cache_values_match_model(self):
        """_cache's stored values must match the model, including values
        seeded purely via update() with no publish."""
        for pid, expected in self.model_cache_values.items():
            actual = self.cache._cache.get(pid)
            assert actual == expected, (
                f"_cache[{pid}]={actual!r} does not match expected "
                f"{expected!r} (model tracks update()+should_publish alike)"
            )

    @rule(pid=st.sampled_from(PIDS), value=st.integers(min_value=0, max_value=100))
    def repeat_publish_suppressed_by_interval(self, pid, value):
        """After any True return, an immediate repeat with min_interval=9999
        must return False — interval suppression must hold."""
        first = self.cache.should_publish(
            pid, value, threshold=self.THRESH, force=True, min_interval=0
        )
        assert first is True
        self.last_published[pid] = value
        self.model_cache_keys.add(pid)
        self.model_last_publish_keys.add(pid)
        self.model_cache_values[pid] = value
        second = self.cache.should_publish(pid, value, threshold=self.THRESH, min_interval=9999)
        assert second is False, (
            f"Immediate repeat of should_publish({pid}, {value}) with "
            f"min_interval=9999 returned True — interval suppression failed"
        )


ValueCacheStatefulTest = ValueCacheMachine.TestCase


class TestValueCacheUpdateThenPublish(unittest.TestCase):
    """ValueCache: 199→203 — point_id in _cache but NOT in _last_publish.

    This happens when update() is called before should_publish() has ever
    been called for that point.  update() populates _cache only; _last_publish
    stays empty for that point_id.  The next should_publish() must therefore
    fall through the 'if point_id in _last_publish' guard (False) and reach
    the threshold comparison at line 203.
    """

    def test_update_before_first_publish_uses_threshold_not_interval(self):
        """update() seeds _cache without _last_publish; should_publish must
        still reach the threshold comparison (199→203 False branch)."""
        from nibe_caching import ValueCache

        vc = ValueCache()
        # Seed _cache for point 1 via update() — _last_publish stays empty
        vc.update(1, 100)
        # Now call should_publish: point IS in _cache so line 194 is False.
        # Point NOT in _last_publish so line 199 is False → 199→203 branch.
        # Value hasn't changed beyond threshold=5 → should suppress.
        result = vc.should_publish(1, 102, threshold=5, min_interval=30)
        self.assertFalse(result, "change below threshold should be suppressed")

    def test_update_before_first_publish_publishes_on_large_change(self):
        """Same 199→203 path — but with a threshold-exceeding change."""
        from nibe_caching import ValueCache

        vc = ValueCache()
        vc.update(1, 100)
        result = vc.should_publish(1, 110, threshold=5, min_interval=30)
        self.assertTrue(result, "change exceeding threshold must publish")
