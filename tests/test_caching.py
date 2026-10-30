"""
test_caching.py
================
Tests for LRUCache (app/nibe_caching.py) — a generic, self-contained
caching primitive with no dependency on EntityManager.
"""

import unittest
from typing import ClassVar

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
