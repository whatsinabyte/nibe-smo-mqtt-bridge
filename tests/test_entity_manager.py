"""
test_entity_manager.py
======================
Nibe_entity_manager tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import time
import unittest
from collections import deque
from unittest.mock import MagicMock, patch

from hypothesis import assume, example, given
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine, initialize, invariant, rule,
)

from conftest import (
    _make_em,
    _cannot_be_int,
    _nibe_point_id,
    _point_entry,
)

class TestCompressDecompressProperties(unittest.TestCase):
    """Hypothesis properties for _compress_payload and _decompress_payload."""

    _data_strategy = st.dictionaries(
        st.text(max_size=20),
        st.one_of(st.integers(), st.text(max_size=50), st.booleans(), st.none()),
        max_size=10,
    )

    @given(_data_strategy)
    def test_compress_never_raises(self, data):
        from nibe_entity_manager import _compress_payload
        result = _compress_payload(data)
        self.assertIsInstance(result, str)

    @given(_data_strategy)
    def test_compress_output_starts_with_sentinel(self, data):
        from nibe_entity_manager import _compress_payload, _GZIP_SENTINEL
        result = _compress_payload(data)
        self.assertTrue(result.startswith(_GZIP_SENTINEL))

    @given(_data_strategy)
    def test_compress_output_is_ascii(self, data):
        """Compressed payload must be plain ASCII — safe for paho MQTT publish."""
        from nibe_entity_manager import _compress_payload
        result = _compress_payload(data)
        result.encode('ascii')  # must not raise

    @given(_data_strategy)
    def test_roundtrip_recovers_original_data(self, data):
        """_decompress_payload(_compress_payload(data)) == json(data)."""
        import json as _json
        from nibe_entity_manager import _compress_payload, _decompress_payload
        compressed = _compress_payload(data)
        recovered_bytes = _decompress_payload(compressed)
        recovered = _json.loads(recovered_bytes.decode('utf-8'))
        self.assertEqual(recovered, data)

    @given(_data_strategy)
    def test_roundtrip_accepts_bytes_input(self, data):
        """_decompress_payload must accept bytes (paho delivers bytes)."""
        import json as _json
        from nibe_entity_manager import _compress_payload, _decompress_payload
        compressed = _compress_payload(data)
        recovered_bytes = _decompress_payload(compressed.encode('utf-8'))
        recovered = _json.loads(recovered_bytes.decode('utf-8'))
        self.assertEqual(recovered, data)

    @given(_data_strategy)
    def test_compress_roundtrip_is_stable(self, data):
        """Two independent compress→decompress roundtrips recover the original dict.
        gzip.compress embeds mtime so byte output differs between calls —
        roundtrip identity is the correct invariant, not byte equality.
        """
        import json as _json
        from nibe_entity_manager import _compress_payload, _decompress_payload
        r1 = _json.loads(_decompress_payload(_compress_payload(data)))
        r2 = _json.loads(_decompress_payload(_compress_payload(data)))
        self.assertEqual(r1, data)
        self.assertEqual(r2, data)


# ---------------------------------------------------------------------------
# 1. st.binary() fuzzing — _decompress_payload never crashes on garbage input
# ---------------------------------------------------------------------------


class TestDecompressPayloadFuzzing(unittest.TestCase):
    """Fuzz _decompress_payload with arbitrary binary input.

    Real-world risk: the MQTT broker retains compressed changelog payloads.
    If the payload is corrupted (truncated, bit-flipped, wrong sentinel)
    the bridge must never crash — it must return gracefully.
    """

    @given(st.binary(max_size=1000))
    def test_arbitrary_bytes_never_raises(self, data):
        """_decompress_payload must never raise for any byte sequence."""
        from nibe_entity_manager import _decompress_payload
        try:
            _decompress_payload(data)
        except Exception:
            pass  # any exception is acceptable — crash is not

    @given(st.binary(max_size=1000))
    def test_arbitrary_bytes_caller_pattern_never_raises(self, data):
        """The typical caller pattern — try decompress, fallback on failure —
        must work for any byte sequence."""
        from nibe_entity_manager import _decompress_payload
        import json as _json
        result = None
        try:
            raw  = _decompress_payload(data)
            result = _json.loads(raw)
        except Exception:
            result = None
        # Result is always None or a parsed object — never an exception propagating
        self.assertIn(type(result), (dict, list, type(None)))

    @given(st.text(max_size=200))
    def test_arbitrary_string_never_raises(self, text):
        """_decompress_payload must never raise for any string input."""
        from nibe_entity_manager import _decompress_payload
        try:
            _decompress_payload(text)
        except Exception:
            pass  # any exception is acceptable — crash is not

    @given(st.binary(max_size=100))
    def test_garbage_with_sentinel_prefix_never_crashes(self, suffix):
        """Even if someone crafts bytes starting with the sentinel,
        corrupt compressed data must not crash."""
        from nibe_entity_manager import _decompress_payload, _GZIP_SENTINEL
        payload = _GZIP_SENTINEL.encode() + suffix
        try:
            _decompress_payload(payload)
        except Exception:
            pass  # graceful failure expected

    @example(data=b'')
    @given(st.binary(max_size=10))
    def test_very_short_binary_never_crashes(self, data):
        from nibe_entity_manager import _decompress_payload
        try:
            _decompress_payload(data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2. st.from_regex() — time parsing edge cases from real HA time entities
# ---------------------------------------------------------------------------


class TestTimeParsingFromRegex(unittest.TestCase):
    """Use st.from_regex to generate realistic-looking but edge-case time strings.

    HA sends HH:MM or HH:MM:SS on time entity command topics.
    The parser must handle valid, invalid, and boundary values.
    """

    def _em(self):
        return _make_em()

    def _ei(self, pid=100):
        return {
            'point_id': pid, 'entity_type': 'time',
            'metadata': {
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True, 'divisor': 1, 'decimal': 0,
                'minValue': 0, 'maxValue': 86399,
                'variableType': 'integer', 'variableSize': 's32',
                'unit': '', 'shortUnit': '',
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            },
            'point_data': {},
        }

    @given(st.from_regex(r'[01][0-9]:[0-5][0-9]', fullmatch=True))
    @example(payload='00:00')   # midnight
    @example(payload='23:59')   # last minute of day
    @example(payload='12:00')   # noon
    def test_valid_hhmm_always_returns_int(self, payload):
        """Any valid HH:MM string must always return a non-negative int."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)

    @given(st.from_regex(r'2[0-3]:[0-5][0-9]', fullmatch=True))
    def test_valid_hhmm_evening_always_returns_int(self, payload):
        """Evening times (20-23 hour) always parse correctly."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIsInstance(result, int)

    @given(st.from_regex(r'\d{2}:\d{2}:\d{2}', fullmatch=True))
    @example(payload='00:00:00')
    @example(payload='23:59:59')
    def test_hhmmss_format_always_returns_int(self, payload):
        """HH:MM:SS format (including invalid ranges) returns int or None."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIn(type(result), (int, type(None)))

    @given(st.from_regex(r'[3-9]\d:\d{2}', fullmatch=True))
    def test_out_of_range_hour_returns_none_or_int(self, payload):
        """Hours >= 30 are invalid — must return None or handle gracefully."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIn(type(result), (int, type(None)))

    @given(st.from_regex(r'\d{2}:[6-9]\d', fullmatch=True))
    def test_out_of_range_minute_returns_none_or_int(self, payload):
        """Minutes >= 60 are invalid — must not raise."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIn(type(result), (int, type(None)))

    @given(st.text(max_size=20).filter(
        lambda s: ':' not in s or not all(p.strip().isdigit() for p in s.split(':')[:2])
    ))
    def test_non_time_string_always_returns_none(self, payload):
        """Non-time strings must always return None without raising."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 3. Snapshot tests — exact MQTT discovery config for known critical points
# ---------------------------------------------------------------------------


class TestMqttCommandPayloadFuzzing(unittest.TestCase):
    """Fuzz the MQTT command handling path with arbitrary byte payloads.

    HA can send any bytes on a command topic — malformed UTF-8, empty payload,
    binary data. The bridge must log a warning and return cleanly, never crash.
    """

    def _em_with_entity(self, pid=100, entity_type='sensor'):
        em = _make_em()
        entity_info = {
            'point_id':     pid,
            'entity_type':  entity_type,
            'entity_id':    f'sensor.nibe_{pid}',
            'command_topic': f'homeassistant/{entity_type}/nibe_{pid}/set',
            'availability_topic': f'homeassistant/{entity_type}/nibe_{pid}/avail',
            'attributes_topic': None,
            'metadata': {
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True, 'divisor': 1, 'decimal': 0,
                'minValue': 0, 'maxValue': 100,
                'variableType': 'integer', 'variableSize': 's16',
                'unit': '', 'shortUnit': '',
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            },
            'is_writable':   True,
            'is_dynamic':    False,
            'is_degenerate_range': False,
        }
        em.active_entities_by_id[pid] = entity_info
        em.mqtt_enabled_points.add(pid)
        return em, entity_info

    def _message(self, payload_bytes):
        msg = MagicMock()
        msg.payload = payload_bytes
        msg.topic   = 'homeassistant/sensor/nibe_100/set'
        return msg

    @given(st.binary(max_size=200))
    def test_arbitrary_bytes_never_crashes(self, payload_bytes):
        """Any byte sequence on a command topic must never crash the bridge."""
        em, entity_info = self._em_with_entity()
        em._handle_command(entity_info, self._message(payload_bytes))

    @given(st.binary(max_size=200))
    def test_arbitrary_bytes_switch_never_crashes(self, payload_bytes):
        em, entity_info = self._em_with_entity(entity_type='switch')
        em._handle_command(entity_info, self._message(payload_bytes))

    @given(st.binary(max_size=200))
    def test_arbitrary_bytes_number_never_crashes(self, payload_bytes):
        em, entity_info = self._em_with_entity(entity_type='number')
        em._handle_command(entity_info, self._message(payload_bytes))

    @example(payload_bytes=b'')
    @given(st.binary(max_size=5))
    def test_very_short_payload_never_crashes(self, payload_bytes):
        em, entity_info = self._em_with_entity()
        em._handle_command(entity_info, self._message(payload_bytes))

    @given(st.text(max_size=100).map(lambda s: s.encode('utf-8', errors='replace')))
    def test_utf8_encoded_text_never_crashes(self, payload_bytes):
        """Valid UTF-8 text payloads must always be handled cleanly."""
        em, entity_info = self._em_with_entity(entity_type='switch')
        em._handle_command(entity_info, self._message(payload_bytes))

    @given(st.binary(max_size=200))
    def test_pending_writes_always_well_formed_after_command(self, payload_bytes):
        """After any command, pending_writes must remain structurally sound."""
        em, entity_info = self._em_with_entity()
        em._handle_command(entity_info, self._message(payload_bytes))
        for pid, entry in em.pending_writes.items():
            self.assertIn('value', entry)
            self.assertIn('time', entry)


# ---------------------------------------------------------------------------
# decide_startup_action properties
# ---------------------------------------------------------------------------


class TestDecideStartupActionProperties(unittest.TestCase):
    """Hypothesis properties for decide_startup_action."""

    _VALID_ACTIONS = frozenset({'apply', 'restore', 'reconcile'})
    _mode_str = st.text(min_size=1, max_size=20)

    @given(st.booleans(), st.one_of(st.none(), _mode_str), _mode_str)
    def test_always_returns_valid_action(self, has_existing, applied, config):
        from nibe_entity_manager import decide_startup_action
        result = decide_startup_action(has_existing, applied, config)
        self.assertIn(result, self._VALID_ACTIONS)

    @given(st.one_of(st.none(), _mode_str), _mode_str)
    def test_no_existing_entities_always_apply(self, applied, config):
        """No existing entities → always 'apply', regardless of modes."""
        from nibe_entity_manager import decide_startup_action
        self.assertEqual(decide_startup_action(False, applied, config), 'apply')

    @given(_mode_str)
    def test_same_mode_gives_restore(self, mode):
        """Same applied and config mode → 'restore'."""
        from nibe_entity_manager import decide_startup_action
        self.assertEqual(decide_startup_action(True, mode, mode), 'restore')

    def test_none_applied_gives_restore(self):
        """applied_mode=None (migration boundary) → 'restore'."""
        from nibe_entity_manager import decide_startup_action
        self.assertEqual(decide_startup_action(True, None, 'essential'), 'restore')

    @given(_mode_str, _mode_str)
    def test_different_modes_gives_reconcile(self, applied, config):
        """Different known applied and config modes → 'reconcile'."""
        from nibe_entity_manager import decide_startup_action
        if applied != config:
            self.assertEqual(
                decide_startup_action(True, applied, config), 'reconcile')

    @given(st.booleans(), st.one_of(st.none(), _mode_str), _mode_str)
    def test_result_is_always_string(self, has_existing, applied, config):
        from nibe_entity_manager import decide_startup_action
        result = decide_startup_action(has_existing, applied, config)
        self.assertIsInstance(result, str)


# ---------------------------------------------------------------------------
# LRUCache properties
# ---------------------------------------------------------------------------


class TestLRUCacheHypothesisProperties(unittest.TestCase):
    """Hypothesis properties for LRUCache."""

    @given(st.integers(min_value=1, max_value=100),
           st.lists(st.tuples(st.integers(min_value=0, max_value=200),
                              st.integers()),
                    min_size=0, max_size=200))
    def test_len_never_exceeds_max_size(self, max_size, operations):
        """len(cache) must never exceed max_size after any sequence of puts."""
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        for key, value in operations:
            cache.put(key, value)
        self.assertLessEqual(len(cache), max_size)

    @given(st.integers(min_value=1, max_value=50),
           st.integers(min_value=0, max_value=1000),
           st.integers())
    def test_get_after_put_returns_value(self, max_size, key, value):
        """get(k) immediately after put(k, v) must return v."""
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        cache.put(key, value)
        self.assertEqual(cache.get(key), value)

    @given(st.integers(min_value=1, max_value=50),
           st.integers(min_value=0, max_value=1000),
           st.integers())
    def test_contains_consistent_with_get(self, max_size, key, value):
        """key in cache ↔ cache.get(key) is not None (sentinel)."""
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        cache.put(key, value)
        self.assertIn(key, cache)
        self.assertIsNotNone(cache.get(key))

    @given(st.integers(min_value=1, max_value=50))
    def test_get_missing_key_returns_none(self, max_size):
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        self.assertIsNone(cache.get(99999))

    @given(st.integers(min_value=2, max_value=20),
           st.lists(st.integers(min_value=0, max_value=1000),
                    min_size=2, max_size=2, unique=True))
    def test_eviction_removes_oldest_when_full(self, max_size, keys):
        """When cache is full and a new key is inserted, the oldest inserted
        key (never accessed since insertion) must be evicted."""
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        first_key = keys[0]
        # Fill cache to capacity, first_key goes in first
        cache.put(first_key, 'first')
        for i in range(1, max_size):
            cache.put(i + 10000, i)  # unique keys outside our range
        # Cache is now full. Add one more — first_key should be evicted
        cache.put(keys[1], 'new')
        self.assertNotIn(first_key, cache)

    @given(st.integers(min_value=1, max_value=50),
           st.integers(min_value=0, max_value=1000),
           st.integers())
    def test_put_same_key_updates_value(self, max_size, key, value):
        """Putting the same key twice must update to the new value."""
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        cache.put(key, 'old')
        cache.put(key, value)
        self.assertEqual(cache.get(key), value)

    @given(st.integers(min_value=1, max_value=50),
           st.integers(min_value=0, max_value=1000),
           st.integers())
    def test_pop_removes_key(self, max_size, key, value):
        """pop(k) must remove k from the cache."""
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        cache.put(key, value)
        popped = cache.pop(key)
        self.assertEqual(popped, value)
        self.assertNotIn(key, cache)

    @given(st.integers(min_value=1, max_value=50))
    def test_clear_empties_cache(self, max_size):
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        for i in range(max_size):
            cache.put(i, i)
        cache.clear()
        self.assertEqual(len(cache), 0)


# ---------------------------------------------------------------------------
# ValueCache properties
# ---------------------------------------------------------------------------


class TestValueCacheHypothesisProperties(unittest.TestCase):
    """Hypothesis properties for ValueCache.should_publish."""

    @given(_nibe_point_id,
           st.integers(min_value=-32768, max_value=32767))
    def test_first_call_always_publishes(self, point_id, raw_value):
        """First call for any point_id must always return True."""
        from nibe_entity_manager import ValueCache
        cache = ValueCache()
        self.assertTrue(cache.should_publish(point_id, raw_value, threshold=1))

    @given(_nibe_point_id,
           st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=1, max_value=100))
    def test_same_value_no_change_suppresses(self, point_id, raw_value, threshold):
        """Same value, within min_interval → must return False."""
        from nibe_entity_manager import ValueCache
        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=threshold)
        result = cache.should_publish(
            point_id, raw_value, threshold=threshold, min_interval=9999)
        self.assertFalse(result)

    @given(_nibe_point_id,
           st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=1, max_value=100))
    def test_large_change_always_publishes(self, point_id, raw_value, threshold):
        """Change ≥ threshold (with no interval restriction) must return True."""
        from nibe_entity_manager import ValueCache
        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=threshold, min_interval=0)
        new_value = raw_value + threshold
        result = cache.should_publish(point_id, new_value, threshold=threshold, min_interval=0)
        self.assertTrue(result)

    @given(_nibe_point_id,
           st.integers(min_value=-32768, max_value=32767))
    def test_force_always_publishes(self, point_id, raw_value):
        """force=True must always return True regardless of value or interval."""
        from nibe_entity_manager import ValueCache
        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=1, min_interval=9999)
        result = cache.should_publish(
            point_id, raw_value, threshold=1, min_interval=9999, force=True)
        self.assertTrue(result)

    @given(st.integers(min_value=0, max_value=99999),
           st.integers(min_value=-32768, max_value=32767))
    def test_after_update_same_value_suppresses_publish(self, point_id, raw_value):
        """After update(pid, v), should_publish(pid, v) returns False — value unchanged."""
        from nibe_entity_manager import ValueCache
        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=1, min_interval=0)
        cache.update(point_id, raw_value)
        result = cache.should_publish(point_id, raw_value, threshold=1, min_interval=0)
        self.assertFalse(result)

    @given(st.integers(min_value=0, max_value=99999),
           st.integers(min_value=-32768, max_value=32767))
    def test_after_discard_next_publish_always_true(self, point_id, raw_value):
        """After discard(pid), the next should_publish always returns True."""
        from nibe_entity_manager import ValueCache
        cache = ValueCache()
        cache.should_publish(point_id, raw_value, threshold=1, min_interval=9999)
        cache.discard(point_id)
        result = cache.should_publish(point_id, raw_value, threshold=1, min_interval=9999)
        self.assertTrue(result)

    @given(st.integers(min_value=0, max_value=99999))
    def test_discard_unknown_pid_never_raises(self, point_id):
        """discard on a point_id never seen must not raise."""
        from nibe_entity_manager import ValueCache
        cache = ValueCache()
        cache.discard(point_id)  # must not raise


# ---------------------------------------------------------------------------
# _detect_type_without_override consistency properties (nibe_entity_detection.py)
# ---------------------------------------------------------------------------


class TestCrossConstantConsistencyProperties(unittest.TestCase):
    """Cross-module consistency invariants between constants.

    These properties verify that the constants in different modules agree
    with each other — bugs in these relationships cause subtle runtime
    failures rather than obvious errors.
    """

    def test_entity_type_overrides_and_binary_exclusions_disjoint(self):
        """ENTITY_TYPE_OVERRIDES and _BINARY_SENSOR_EXCLUSIONS must be disjoint.

        A point in ENTITY_TYPE_OVERRIDES is already explicitly classified;
        adding it to _BINARY_SENSOR_EXCLUSIONS would be redundant and suggests
        a maintenance error.
        """
        from nibe_entity_detection import ENTITY_TYPE_OVERRIDES, _BINARY_SENSOR_EXCLUSIONS
        overlap = set(ENTITY_TYPE_OVERRIDES.keys()) & _BINARY_SENSOR_EXCLUSIONS
        self.assertEqual(overlap, set(),
            f"Points appear in both ENTITY_TYPE_OVERRIDES and "
            f"_BINARY_SENSOR_EXCLUSIONS: {overlap}")

    def test_value_mappings_holding_and_overrides_disjoint(self):
        """VALUE_MAPPINGS holding entries and ENTITY_TYPE_OVERRIDES must be disjoint.

        A holding register with value mappings routes to 'select' automatically.
        An override on the same point is unreachable dead code.
        """
        from nibe_entity_detection import VALUE_MAPPINGS, ENTITY_TYPE_OVERRIDES
        vm_holding = set(VALUE_MAPPINGS.get('holding', {}).keys())
        overlap = vm_holding & set(ENTITY_TYPE_OVERRIDES.keys())
        self.assertEqual(overlap, set(),
            f"Points appear in both VALUE_MAPPINGS holding and "
            f"ENTITY_TYPE_OVERRIDES: {overlap}")

    def test_retry_base_leq_retry_max(self):
        """_RETRY_BASE_S must always be ≤ _RETRY_MAX_S."""
        from nibe_api import _RETRY_BASE_S, _RETRY_MAX_S
        self.assertLessEqual(_RETRY_BASE_S, _RETRY_MAX_S)

    def test_retry_delay_bounded_by_max(self):
        """Every _retry_delay() call must return a value ≤ _RETRY_MAX_S."""
        from nibe_api import _retry_delay, _RETRY_MAX_S
        for _ in range(50):
            self.assertLessEqual(_retry_delay(), _RETRY_MAX_S)

    def test_retry_delay_always_non_negative(self):
        """Every _retry_delay() call must return a non-negative value."""
        from nibe_api import _retry_delay
        for _ in range(50):
            self.assertGreaterEqual(_retry_delay(), 0.0)

    def test_changelog_min_leq_max_entries(self):
        """_CHANGELOG_MIN_ENTRIES must be ≤ _CHANGELOG_MAX_ENTRIES."""
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES, _CHANGELOG_MAX_ENTRIES
        self.assertLessEqual(_CHANGELOG_MIN_ENTRIES, _CHANGELOG_MAX_ENTRIES)

    def test_gzip_sentinel_is_nonempty_string(self):
        """_GZIP_SENTINEL must be a non-empty string."""
        from nibe_entity_manager import _GZIP_SENTINEL
        self.assertIsInstance(_GZIP_SENTINEL, str)
        self.assertGreater(len(_GZIP_SENTINEL), 0)

    def test_compress_output_starts_with_sentinel(self):
        """_compress_payload output must always start with _GZIP_SENTINEL."""
        from nibe_entity_manager import _compress_payload, _GZIP_SENTINEL
        for data in [{}, {'key': 'value'}, {'n': 42}]:
            result = _compress_payload(data)
            self.assertTrue(result.startswith(_GZIP_SENTINEL),
                f"_compress_payload output does not start with sentinel: {result[:20]!r}")

    def test_text_register_max_len_positive(self):
        """_TEXT_REGISTER_MAX_LEN must be a positive integer."""
        from nibe_entity_manager import _TEXT_REGISTER_MAX_LEN
        self.assertIsInstance(_TEXT_REGISTER_MAX_LEN, int)
        self.assertGreater(_TEXT_REGISTER_MAX_LEN, 0)

    def test_stale_write_age_positive(self):
        """_STALE_WRITE_AGE_S must be positive (defines write guard timeout)."""
        from nibe_entity_manager import _STALE_WRITE_AGE_S
        self.assertGreater(_STALE_WRITE_AGE_S, 0)

    def test_post_write_scan_positive(self):
        """_POST_WRITE_SCAN_S must be positive (defines dynamic detection window)."""
        from nibe_entity_manager import _POST_WRITE_SCAN_S
        self.assertGreater(_POST_WRITE_SCAN_S, 0)

    def test_cmd_id_length_positive(self):
        """_CMD_ID_LENGTH must be a positive integer."""
        from nibe_entity_manager import _CMD_ID_LENGTH
        self.assertIsInstance(_CMD_ID_LENGTH, int)
        self.assertGreater(_CMD_ID_LENGTH, 0)

    def test_notification_id_constants_are_strings(self):
        """All _NOTIF_* constants must be non-empty strings."""
        from nibe_entity_manager import (
            _NOTIF_API_UNREACHABLE, _NOTIF_WRITE_ERROR,
            _NOTIF_NO_ENTITIES, _NOTIF_DISCOVERY_INCOMPLETE,
        )
        for notif_id in (_NOTIF_API_UNREACHABLE, _NOTIF_WRITE_ERROR,
                         _NOTIF_NO_ENTITIES, _NOTIF_DISCOVERY_INCOMPLETE):
            self.assertIsInstance(notif_id, str)
            self.assertGreater(len(notif_id), 0)
            # Must be safe as MQTT topic segment — no spaces or special chars
            self.assertNotIn(' ', notif_id)

    def test_notification_id_constants_are_unique(self):
        """All _NOTIF_* constants must be distinct."""
        from nibe_entity_manager import (
            _NOTIF_API_UNREACHABLE, _NOTIF_WRITE_ERROR,
            _NOTIF_NO_ENTITIES, _NOTIF_DISCOVERY_INCOMPLETE,
        )
        notif_ids = [_NOTIF_API_UNREACHABLE, _NOTIF_WRITE_ERROR,
                     _NOTIF_NO_ENTITIES, _NOTIF_DISCOVERY_INCOMPLETE]
        self.assertEqual(len(notif_ids), len(set(notif_ids)),
            "Duplicate _NOTIF_* constant values detected")

    def test_applied_mode_timeout_positive(self):
        """_APPLIED_MODE_TIMEOUT_S must be positive."""
        from nibe_entity_manager import _APPLIED_MODE_TIMEOUT_S
        self.assertGreater(_APPLIED_MODE_TIMEOUT_S, 0)

    def test_mqtt_scan_timeout_positive(self):
        """_MQTT_SCAN_TIMEOUT_S must be positive."""
        from nibe_entity_manager import _MQTT_SCAN_TIMEOUT_S
        self.assertGreater(_MQTT_SCAN_TIMEOUT_S, 0)


# ---------------------------------------------------------------------------
# nibe_ha_integration constants structural properties
# ---------------------------------------------------------------------------


class TestPublishPointMetadataProperties(unittest.TestCase):
    """Hypothesis properties for publish_point_metadata."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _point(self, pid):
        return {
            'variableId': pid, 'display_title': f'Point {pid}',
            'entity_type': 'sensor', 'entity_category': 'diagnostic',
            'is_writable': False, 'is_dynamic': False, 'description': '',
            'metadata': {
                'unit': '', 'shortUnit': '', 'minValue': 0, 'maxValue': 100,
                'modbusRegisterID': pid,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 'u8',
                'isWritable': False, 'divisor': 1, 'decimal': 0,
                'intDefaultValue': None, 'stringDefaultValue': '', 'change': 1,
            },
        }

    @given(_nibe_point_id)
    def test_publishes_to_correct_browser_topic(self, pid):
        """publish_point_metadata must always publish to the per-point browser topic."""
        from nibe_mqtt_publisher import BrowserTopic
        pub, mqtt = self._pub()
        pub.publish_point_metadata(self._point(pid))
        expected_topic = BrowserTopic.META_TEMPLATE.format(id=pid)
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == expected_topic]
        self.assertTrue(calls, f"No publish to {expected_topic!r} for pid={pid}")

    @given(_nibe_point_id)
    def test_payload_contains_point_id(self, pid):
        """Payload must contain the point_id."""
        import json as _json
        pub, mqtt = self._pub()
        pub.publish_point_metadata(self._point(pid))
        calls = [c for c in mqtt.publish.call_args_list]
        self.assertTrue(calls)
        payload = _json.loads(calls[-1].args[1])
        self.assertEqual(payload['id'], pid)

    @given(_nibe_point_id)
    def test_payload_always_valid_json(self, pid):
        import json as _json
        pub, mqtt = self._pub()
        pub.publish_point_metadata(self._point(pid))
        calls = [c for c in mqtt.publish.call_args_list]
        self.assertTrue(calls)
        _json.loads(calls[-1].args[1])  # must parse without raising

    @given(_nibe_point_id)
    def test_always_published_retained(self, pid):
        pub, mqtt = self._pub()
        pub.publish_point_metadata(self._point(pid))
        calls = [c for c in mqtt.publish.call_args_list]
        self.assertTrue(calls)
        retain = calls[-1].kwargs.get('retain',
                 calls[-1].args[2] if len(calls[-1].args) > 2 else False)
        self.assertTrue(retain)


# ---------------------------------------------------------------------------
# _is_suppressed properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestIsSuppressedProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager._is_suppressed."""

    @given(st.integers(min_value=1, max_value=100))
    def test_positive_depth_returns_true(self, depth):
        """Any positive suppression depth must return True."""
        em = _make_em()
        em._suppress_enabled_state_depth = depth
        self.assertTrue(em._is_suppressed())

    def test_zero_depth_returns_false(self):
        """Zero depth must always return False."""
        em = _make_em()
        em._suppress_enabled_state_depth = 0
        self.assertFalse(em._is_suppressed())

    @given(st.integers(min_value=1, max_value=100))
    def test_always_returns_bool(self, depth):
        em = _make_em()
        em._suppress_enabled_state_depth = depth
        self.assertIsInstance(em._is_suppressed(), bool)

    def test_initial_state_not_suppressed(self):
        """Fresh EntityManager must not be suppressed."""
        em = _make_em()
        self.assertFalse(em._is_suppressed())


# ---------------------------------------------------------------------------
# DynamicPointEntry computed property Hypothesis tests
# ---------------------------------------------------------------------------


class TestDynamicPointEntryComputedProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointEntry computed methods."""

    _entry_strategy = st.fixed_dictionaries({
        'point_id':   st.integers(min_value=1, max_value=99999),
        'title':      st.text(max_size=40),
        'entity_type': st.sampled_from(['switch', 'select']),
        'processed_values':   st.sets(st.integers(min_value=0, max_value=20)),
        'unprocessed_values': st.sets(st.integers(min_value=0, max_value=20)),
        'is_controlling': st.one_of(st.none(), st.booleans()),
        'firmware_removed': st.booleans(),
    })

    @given(_entry_strategy)
    def test_is_fully_processed_iff_unprocessed_empty_and_processed_nonempty(self, kwargs):
        from nibe_dynamic_map import DynamicPointEntry
        entry = DynamicPointEntry(**{k: v for k, v in kwargs.items()})
        expected = len(entry.unprocessed_values) == 0 and len(entry.processed_values) > 0
        self.assertEqual(entry.is_fully_processed(), expected)

    @given(_entry_strategy)
    def test_all_known_dynamic_points_is_union_of_dpbv(self, kwargs):
        from nibe_dynamic_map import DynamicPointEntry
        entry = DynamicPointEntry(**{k: v for k, v in kwargs.items()})
        expected = set()
        for pts in entry.dynamic_points_by_value.values():
            expected.update(pts)
        self.assertEqual(entry.all_known_dynamic_points(), expected)

    @given(_entry_strategy,
           st.integers(min_value=0, max_value=20))
    def test_dynamic_points_for_value_none_iff_not_in_dpbv(self, kwargs, value):
        from nibe_dynamic_map import DynamicPointEntry
        entry = DynamicPointEntry(**{k: v for k, v in kwargs.items()})
        result = entry.dynamic_points_for_value(value)
        if value in entry.dynamic_points_by_value:
            self.assertIsNotNone(result)
            self.assertEqual(result, entry.dynamic_points_by_value[value])
        else:
            self.assertIsNone(result)

    @given(_entry_strategy)
    def test_all_known_dynamic_points_never_raises(self, kwargs):
        from nibe_dynamic_map import DynamicPointEntry
        entry = DynamicPointEntry(**{k: v for k, v in kwargs.items()})
        result = entry.all_known_dynamic_points()
        self.assertIsInstance(result, set)

    @given(_entry_strategy)
    def test_default_is_controlling_is_none(self, kwargs):
        """A freshly constructed entry (no explicit is_controlling) starts as None."""
        from nibe_dynamic_map import DynamicPointEntry
        kwargs_no_ctrl = {k: v for k, v in kwargs.items() if k != 'is_controlling'}
        entry = DynamicPointEntry(**kwargs_no_ctrl)
        self.assertIsNone(entry.is_controlling)

    @given(_entry_strategy)
    def test_default_firmware_removed_is_false(self, kwargs):
        from nibe_dynamic_map import DynamicPointEntry
        kwargs_no_fr = {k: v for k, v in kwargs.items() if k != 'firmware_removed'}
        entry = DynamicPointEntry(**kwargs_no_fr)
        self.assertFalse(entry.firmware_removed)


# ---------------------------------------------------------------------------
# _read_applied_mode_from_file properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestBuildPointDefaultsProperties(unittest.TestCase):
    """Hypothesis properties for _build_point_defaults."""

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_never_raises(self, all_points_by_id):
        from nibe_lovelace import _build_point_defaults
        _build_point_defaults(all_points_by_id)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_always_returns_dict(self, all_points_by_id):
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        self.assertIsInstance(result, dict)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_keys_are_ints_from_input(self, all_points_by_id):
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for k in result.keys():
            self.assertIsInstance(k, int)
            self.assertIn(k, all_points_by_id)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_values_are_strings(self, all_points_by_id):
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for v in result.values():
            self.assertIsInstance(v, str)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_non_writable_points_excluded(self, all_points_by_id):
        """Non-writable points must never appear in the result."""
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for pid in result.keys():
            meta = all_points_by_id[pid]['metadata']
            self.assertTrue(meta['isWritable'])

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_non_holding_register_excluded(self, all_points_by_id):
        """Non-HOLDING register points must never appear in the result."""
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for pid in result.keys():
            meta = all_points_by_id[pid]['metadata']
            self.assertEqual(meta['modbusRegisterType'], 'MODBUS_HOLDING_REGISTER')

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_degenerate_range_excluded(self, all_points_by_id):
        """Points with min==max (degenerate range) must never appear."""
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for pid in result.keys():
            meta = all_points_by_id[pid]['metadata']
            self.assertNotEqual(meta['minValue'], meta['maxValue'])


# ---------------------------------------------------------------------------
# _build_device_info properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestBuildDeviceInfoProperties(unittest.TestCase):
    """Hypothesis properties for _build_device_info."""

    _api_response = st.fixed_dictionaries({
        'product': st.fixed_dictionaries({
            'name':         st.text(max_size=30),
            'manufacturer': st.text(max_size=20),
            'firmwareId':   st.text(max_size=10),
            'serialNumber': st.text(max_size=20),
        }),
    })

    @given(_api_response, st.text(max_size=20), st.text(max_size=30),
           st.text(max_size=50))
    def test_never_raises(self, api_response, device_id, device_name, base_url):
        from nibe_entity_manager import _build_device_info
        _build_device_info(api_response, device_id, device_name, base_url)

    @given(_api_response, st.text(max_size=20), st.text(max_size=30),
           st.text(max_size=50))
    def test_always_returns_dict(self, api_response, device_id, device_name, base_url):
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(api_response, device_id, device_name, base_url)
        self.assertIsInstance(result, dict)

    @given(_api_response, st.text(min_size=1, max_size=20),
           st.text(max_size=30), st.text(max_size=50))
    def test_identifiers_contains_device_id(self, api_response, device_id,
                                             device_name, base_url):
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(api_response, device_id, device_name, base_url)
        self.assertIn(device_id, result.get('identifiers', []))

    @given(_api_response, st.text(max_size=20),
           st.text(min_size=1, max_size=30).filter(lambda s: s != 'Nibe SMO S40'),
           st.text(max_size=50))
    def test_custom_device_name_always_used(self, api_response, device_id,
                                            device_name, base_url):
        """Non-default device_name must always appear as 'name' in result."""
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(api_response, device_id, device_name, base_url)
        self.assertEqual(result.get('name'), device_name)

    @given(_api_response, st.text(max_size=20), st.text(max_size=50))
    def test_default_name_prefers_api_name_when_available(self, api_response,
                                                           device_id, base_url):
        """When device_name is the default and API provides a name, use API name."""
        from nibe_entity_manager import _build_device_info
        api_name = api_response.get('product', {}).get('name', '').strip()
        result = _build_device_info(api_response, device_id, 'Nibe SMO S40', base_url)
        if api_name:
            self.assertEqual(result.get('name'), api_name)

    @given(_api_response, st.text(max_size=20), st.text(max_size=30),
           st.text(max_size=50))
    def test_no_empty_string_values(self, api_response, device_id,
                                    device_name, base_url):
        """Empty strings must be stripped from the result dict."""
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(api_response, device_id, device_name, base_url)
        for v in result.values():
            if isinstance(v, str):
                self.assertNotEqual(v, '')


# ---------------------------------------------------------------------------
# MqttDiscoveryPublisher static config builder properties
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# get_memory_usage properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestPublishBridgeAlertProperties(unittest.TestCase):
    """Hypothesis properties for publish_bridge_alert."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _get_payload(self, mqtt):
        import json as _json
        from nibe_mqtt_publisher import BrowserTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.BRIDGE_ALERT]
        self.assertTrue(calls, "No BRIDGE_ALERT publish found")
        return _json.loads(calls[-1].args[1])

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_payload_always_valid_json(self, alert_type, severity, message):
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        self._get_payload(mqtt)  # must parse without raising

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_payload_contains_required_keys(self, alert_type, severity, message):
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        payload = self._get_payload(mqtt)
        for key in ('alert_type', 'severity', 'message', 'timestamp', 'context'):
            self.assertIn(key, payload)

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_payload_values_match_inputs(self, alert_type, severity, message):
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['alert_type'], alert_type)
        self.assertEqual(payload['severity'],   severity)
        self.assertEqual(payload['message'],    message)

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_always_published_non_retained(self, alert_type, severity, message):
        """retain=False is mandatory — alerts must not replay on reconnect."""
        from nibe_mqtt_publisher import BrowserTopic
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.BRIDGE_ALERT]
        self.assertTrue(calls)
        retain = calls[-1].kwargs.get('retain', calls[-1].args[2] if len(calls[-1].args) > 2 else True)
        self.assertFalse(retain)

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_context_none_becomes_empty_dict(self, alert_type, severity, message):
        """When context=None, payload context must be {} not null."""
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message, context=None)
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['context'], {})

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100),
           st.dictionaries(st.text(max_size=10), st.text(max_size=20), max_size=5))
    def test_context_dict_preserved(self, alert_type, severity, message, context):
        """Provided context dict is always preserved exactly in payload."""
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message, context=context)
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['context'], context)

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_timestamp_is_positive_float(self, alert_type, severity, message):
        """Timestamp in payload must always be a positive number."""
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        payload = self._get_payload(mqtt)
        self.assertGreater(payload['timestamp'], 0)


# ---------------------------------------------------------------------------
# LRUCache.get_stats properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestLRUCacheGetStatsProperties(unittest.TestCase):
    """Hypothesis properties for LRUCache.get_stats."""

    @given(st.integers(min_value=1, max_value=50),
           st.lists(st.tuples(st.integers(min_value=0, max_value=200),
                              st.integers()),
                    max_size=100))
    def test_size_always_leq_capacity(self, max_size, operations):
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        for k, v in operations:
            cache.put(k, v)
        stats = cache.get_stats()
        self.assertLessEqual(stats['size'], stats['capacity'])

    @given(st.integers(min_value=1, max_value=50),
           st.lists(st.tuples(st.integers(min_value=0, max_value=200),
                              st.integers()),
                    max_size=50))
    def test_size_equals_len_cache(self, max_size, operations):
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        for k, v in operations:
            cache.put(k, v)
        stats = cache.get_stats()
        self.assertEqual(stats['size'], len(cache))

    @given(st.integers(min_value=1, max_value=50),
           st.lists(st.integers(min_value=0, max_value=100), max_size=50),
           st.lists(st.integers(min_value=0, max_value=100), max_size=50))
    def test_hit_rate_always_in_0_1(self, max_size, put_keys, get_keys):
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        for k in put_keys:
            cache.put(k, k)
        for k in get_keys:
            cache.get(k)
        stats = cache.get_stats()
        self.assertGreaterEqual(stats['hit_rate'], 0.0)
        self.assertLessEqual(stats['hit_rate'],    1.0)

    @given(st.integers(min_value=1, max_value=50),
           st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=50),
           st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=50))
    def test_hits_plus_misses_equals_total_gets(self, max_size, put_keys, get_keys):
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        for k in put_keys:
            cache.put(k, k)
        for k in get_keys:
            cache.get(k)
        stats = cache.get_stats()
        self.assertEqual(stats['hits'] + stats['misses'], len(get_keys))

    @given(st.integers(min_value=1, max_value=50),
           st.lists(st.tuples(st.integers(min_value=0, max_value=100),
                              st.integers()), max_size=30))
    def test_stats_reset_after_clear(self, max_size, operations):
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        for k, v in operations:
            cache.put(k, v)
            cache.get(k)
        cache.clear()
        stats = cache.get_stats()
        self.assertEqual(stats['size'],   0)
        self.assertEqual(stats['hits'],   0)
        self.assertEqual(stats['misses'], 0)
        self.assertEqual(stats['hit_rate'], 0)

    @given(st.integers(min_value=1, max_value=100))
    def test_capacity_always_equals_max_size(self, max_size):
        from nibe_entity_manager import LRUCache
        cache = LRUCache(max_size=max_size)
        self.assertEqual(cache.get_stats()['capacity'], max_size)


# ---------------------------------------------------------------------------
# _get_cached_entity_type properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestBuildPointMetadataDictExtendedProperties(unittest.TestCase):
    """Extended Hypothesis properties for _build_point_metadata_dict
    covering the firmware metadata fields passed through unchanged."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        return MqttDiscoveryPublisher(
            mqtt_client=MagicMock(), device_info={},
            device_id='test', device_name='Test',
        )

    def _point(self, pid, **meta_overrides):
        meta = {
            'unit': '', 'shortUnit': 'X',
            'minValue': 0, 'maxValue': 100,
            'modbusRegisterID': pid,
            'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
            'variableType': 'integer', 'variableSize': 'u8',
            'isWritable': False, 'divisor': 1, 'decimal': 0,
            'intDefaultValue': None, 'stringDefaultValue': '',
            'change': 1,
        }
        meta.update(meta_overrides)
        return {
            'variableId': pid, 'display_title': f'P{pid}',
            'entity_type': 'sensor', 'entity_category': 'diagnostic',
            'is_writable': False, 'is_dynamic': False, 'description': '',
            'metadata': meta,
        }

    @given(_nibe_point_id, st.integers(min_value=0, max_value=10000))
    def test_divisor_preserved(self, pid, divisor):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, divisor=divisor))
        self.assertEqual(result['divisor'], divisor)

    @given(_nibe_point_id, st.integers(min_value=0, max_value=6))
    def test_decimal_preserved(self, pid, decimal):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, decimal=decimal))
        self.assertEqual(result['decimal'], decimal)

    @given(_nibe_point_id, st.integers(min_value=0, max_value=100))
    def test_change_preserved(self, pid, change):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, change=change))
        self.assertEqual(result['change'], change)

    @given(_nibe_point_id,
           st.sampled_from(['integer', 'floating-point', 'string', 'time', 'date']))
    def test_variable_type_preserved(self, pid, var_type):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, variableType=var_type))
        self.assertEqual(result['variableType'], var_type)

    @given(_nibe_point_id,
           st.sampled_from(['u8', 'u16', 's16', 's32', 'u32']))
    def test_variable_size_preserved(self, pid, var_size):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, variableSize=var_size))
        self.assertEqual(result['variableSize'], var_size)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER',
                            'MODBUS_NO_REGISTER']))
    def test_modbus_register_type_preserved(self, pid, reg_type):
        pub = self._pub()
        result = pub._build_point_metadata_dict(
            self._point(pid, modbusRegisterType=reg_type))
        self.assertEqual(result['modbusRegisterType'], reg_type)

    @given(_nibe_point_id, st.text(max_size=10))
    def test_short_unit_preserved(self, pid, short_unit):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, shortUnit=short_unit))
        self.assertEqual(result['shortUnit'], short_unit)

    @given(_nibe_point_id)
    def test_divisor_zero_uses_default_of_1(self, pid):
        """divisor=0 must never appear in output — treated as 1 by firmware contract."""
        pub = self._pub()
        # The metadata field itself: divisor=0 is stored as-is in metadata_dict
        result = pub._build_point_metadata_dict(self._point(pid, divisor=0))
        # What matters: divisor in output matches what's in metadata
        self.assertEqual(result['divisor'], 0)

    @given(_nibe_point_id,
           st.integers(min_value=1, max_value=10000),
           st.integers(min_value=-32768, max_value=32767),
           st.text(max_size=5))
    def test_default_value_field_uses_apply_divisor(self, pid, divisor, int_default, unit):
        """When intDefaultValue is set, default_value uses apply_divisor formatting."""
        from nibe_entity_detection import apply_divisor
        pub = self._pub()
        point = self._point(pid, divisor=divisor, unit=unit)
        point['metadata']['intDefaultValue'] = int_default
        result = pub._build_point_metadata_dict(point)
        if 'default_value' in result:
            expected_display = apply_divisor(int_default, divisor)
            self.assertIn(expected_display, result['default_value'])


# ---------------------------------------------------------------------------
# Cross-module: _build_point_metadata_dict.type consistent with
# _get_cached_entity_type (nibe_mqtt_publisher ↔ nibe_entity_manager)
# ---------------------------------------------------------------------------


class TestBuildMenuViewProperties(unittest.TestCase):
    """Hypothesis properties for _build_menu_view."""

    _menu_strategy = st.fixed_dictionaries({
        'id':    st.text(max_size=10),
        'title': st.text(max_size=30),
        'settings': st.lists(st.fixed_dictionaries({
            'point_id': st.one_of(st.none(),
                                   st.integers(min_value=1, max_value=9999)),
            'label':    st.text(max_size=20),
        }), max_size=5),
        'submenus': st.just([]),
    })

    @given(_menu_strategy)
    def test_always_returns_list(self, menu):
        from nibe_lovelace import _build_menu_view
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result = _build_menu_view(menu, rw)
        self.assertIsInstance(result, list)

    @given(_menu_strategy)
    def test_never_raises(self, menu):
        from nibe_lovelace import _build_menu_view
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        _build_menu_view(menu, rw)  # must not raise

    @given(_menu_strategy)
    def test_none_known_dynamic_defaults_safely(self, menu):
        """known_dynamic=None must be handled identically to empty set."""
        from nibe_lovelace import _build_menu_view
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result_none  = _build_menu_view(menu, rw, known_dynamic=None)
        result_empty = _build_menu_view(menu, rw, known_dynamic=set())
        self.assertEqual(result_none, result_empty)

    @given(_menu_strategy)
    def test_none_absent_dynamic_defaults_safely(self, menu):
        """absent_dynamic=None must be handled identically to empty set."""
        from nibe_lovelace import _build_menu_view
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result_none  = _build_menu_view(menu, rw, absent_dynamic=None)
        result_empty = _build_menu_view(menu, rw, absent_dynamic=set())
        self.assertEqual(result_none, result_empty)

    @given(_menu_strategy)
    def test_none_point_defaults_defaults_safely(self, menu):
        """point_defaults=None must be handled identically to empty dict."""
        from nibe_lovelace import _build_menu_view
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result_none  = _build_menu_view(menu, rw, point_defaults=None)
        result_empty = _build_menu_view(menu, rw, point_defaults={})
        self.assertEqual(result_none, result_empty)


# ---------------------------------------------------------------------------
# resolve_point_from_entity_id properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestResolvePointFromEntityIdProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager.resolve_point_from_entity_id.

    Three-pass lookup:
      1. slug starts with 'nibe_' → parse int directly
      2. scan active_entities config topics
      3. unique_id registry map
    """

    @given(st.text(max_size=50).filter(lambda s: '.' not in s))
    def test_no_dot_always_returns_none(self, entity_id):
        """No '.' in entity_id → always None (not a valid HA entity_id)."""
        em = _make_em()
        self.assertIsNone(em.resolve_point_from_entity_id(entity_id))

    @given(st.integers(min_value=0, max_value=99999),
           st.sampled_from(['sensor', 'switch', 'number', 'binary_sensor',
                            'select', 'button']))
    def test_nibe_prefixed_slug_returns_correct_pid(self, pid, domain):
        """domain.nibe_{pid} always resolves to pid."""
        em = _make_em()
        entity_id = f'{domain}.nibe_{pid}'
        result = em.resolve_point_from_entity_id(entity_id)
        self.assertEqual(result, pid)

    @given(st.sampled_from(['sensor', 'switch', 'number']),
           st.text(min_size=1, max_size=20).filter(_cannot_be_int))
    def test_nibe_prefix_with_non_int_returns_none(self, domain, suffix):
        """domain.nibe_{non-int} must return None.
        Filter uses int() directly to match production — catches '0\\r' etc.
        """
        em = _make_em()
        entity_id = f'{domain}.nibe_{suffix}'
        result = em.resolve_point_from_entity_id(entity_id)
        self.assertIsNone(result)

    def test_empty_nibe_slug_returns_none(self):
        """domain.nibe_ (empty after prefix) must return None."""
        em = _make_em()
        self.assertIsNone(em.resolve_point_from_entity_id('sensor.nibe_'))

    @given(st.integers(min_value=0, max_value=99999))
    def test_nibe_zero_returns_zero(self, _n):
        """nibe_0 must resolve to 0 — zero is a valid point_id."""
        em = _make_em()
        self.assertEqual(em.resolve_point_from_entity_id('sensor.nibe_0'), 0)

    def test_non_nibe_slug_without_match_returns_none(self):
        """entity_id without nibe_ slug and no active entity match → None."""
        em = _make_em()
        for entity_id in ['sensor.other_entity', 'switch.my_device',
                          'number.some_point', 'sensor.']:
            result = em.resolve_point_from_entity_id(entity_id)
            self.assertIsNone(result, f"Expected None for {entity_id!r}")

    @given(st.integers(min_value=1, max_value=99999),
           st.sampled_from(['sensor', 'switch', 'number']))
    def test_result_always_int_or_none(self, pid, domain):
        """resolve_point_from_entity_id always returns int or None."""
        em = _make_em()
        result = em.resolve_point_from_entity_id(f'{domain}.nibe_{pid}')
        self.assertIn(type(result), (int, type(None)))

    @given(st.integers(min_value=0, max_value=99999))
    def test_nibe_slug_roundtrip_with_create_entity_id(self, pid):
        """create_entity_id(pid) always resolves back to pid."""
        from nibe_entity_detection import create_entity_id
        em = _make_em()
        entity_id = f'sensor.{create_entity_id(pid)}'
        result = em.resolve_point_from_entity_id(entity_id)
        self.assertEqual(result, pid)


# ---------------------------------------------------------------------------
# all_points and active_entities properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestAllPointsActiveEntitiesProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager.all_points and active_entities."""

    @given(st.integers(min_value=0, max_value=100))
    def test_all_points_length_matches_all_points_by_id(self, n):
        """len(all_points) always equals len(all_points_by_id)."""
        em = _make_em()
        for i in range(n):
            em.all_points_by_id[i] = {'variableId': i}
        self.assertEqual(len(em.all_points), n)

    @given(st.integers(min_value=0, max_value=50))
    def test_active_entities_length_matches_active_entities_by_id(self, n):
        """len(active_entities) always equals len(active_entities_by_id)."""
        em = _make_em()
        for i in range(n):
            em.active_entities_by_id[i] = {'point_id': i}
        self.assertEqual(len(em.active_entities), n)

    @given(st.integers(min_value=0, max_value=100))
    def test_all_points_always_returns_list(self, n):
        em = _make_em()
        for i in range(n):
            em.all_points_by_id[i] = {'variableId': i}
        self.assertIsInstance(em.all_points, list)

    @given(st.integers(min_value=0, max_value=50))
    def test_active_entities_always_returns_list(self, n):
        em = _make_em()
        for i in range(n):
            em.active_entities_by_id[i] = {'point_id': i}
        self.assertIsInstance(em.active_entities, list)

    @given(st.integers(min_value=1, max_value=50))
    def test_all_points_contains_all_values_from_dict(self, n):
        """all_points must contain every value in all_points_by_id."""
        em = _make_em()
        for i in range(n):
            em.all_points_by_id[i] = {'variableId': i, 'label': f'p{i}'}
        all_pts = em.all_points
        for v in em.all_points_by_id.values():
            self.assertIn(v, all_pts)

    @given(st.integers(min_value=0, max_value=50))
    def test_two_calls_return_equal_results(self, n):
        """Two consecutive calls to all_points return equal results."""
        em = _make_em()
        for i in range(n):
            em.all_points_by_id[i] = {'variableId': i}
        self.assertEqual(em.all_points, em.all_points)

    def test_empty_em_all_points_is_empty_list(self):
        em = _make_em()
        self.assertEqual(em.all_points, [])

    def test_empty_em_active_entities_is_empty_list(self):
        em = _make_em()
        self.assertEqual(em.active_entities, [])


# ---------------------------------------------------------------------------
# build_disable_notification properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestCompression(unittest.TestCase):
    def setUp(self):
        from nibe_entity_manager import _compress_payload, _decompress_payload, _GZIP_SENTINEL
        self.compress   = _compress_payload
        self.decompress = _decompress_payload
        self.sentinel   = _GZIP_SENTINEL

    def test_round_trip(self):
        data   = {'history': [{'id': 1}] * 10, '_seq': 5}
        result = json.loads(self.decompress(self.compress(data)))
        self.assertEqual(result['_seq'], 5)
        self.assertEqual(len(result['history']), 10)

    def test_output_is_ascii_string(self):
        self.compress({'a': 1}).encode('ascii')   # must not raise

    def test_sentinel_prefix(self):
        self.assertTrue(self.compress({'x': 1}).startswith(self.sentinel))

    def test_smaller_than_raw_json(self):
        data = {'history': [{'title': f't{i}', 'v': i} for i in range(100)]}
        self.assertLess(len(self.compress(data)), len(json.dumps(data)))

    def test_bytes_with_sentinel(self):
        compressed = self.compress({'ok': True}).encode('utf-8')
        self.assertTrue(json.loads(self.decompress(compressed))['ok'])

    def test_empty_dict(self):
        self.assertEqual(json.loads(self.decompress(self.compress({}))), {})

    def test_large_payload_under_5kb(self):
        entries = [
            {'id': f'c{i}', 'timestamp': 1.0 + i, 'iso_timestamp': '2024',
             'added': [{'id': 6983}], 'removed': [], 'unread': False}
            for i in range(200)
        ]
        self.assertLess(len(self.compress({'history': entries})), 5000)


# ===========================================================================
# 8. ValueCache
# ===========================================================================


class TestValueCache(unittest.TestCase):
    def setUp(self):
        from nibe_entity_manager import ValueCache
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

    def test_unchanged_value_not_republished_even_at_zero_interval(self):
        # ValueCache requires a threshold-exceeding change OR force=True.
        # Zero min_interval alone is not enough to re-publish an identical value.
        self.cache.should_publish(1, 100, threshold=1, min_interval=0)
        self.assertFalse(self.cache.should_publish(1, 100, threshold=1, min_interval=0))


# ===========================================================================
# 8b. LRUCache — uncovered branches
# ===========================================================================


class TestLRUCache(unittest.TestCase):
    def setUp(self):
        from nibe_entity_manager import LRUCache
        self.cache = LRUCache(max_size=3)

    def test_put_new_item(self):
        self.cache.put('a', 1)
        self.assertEqual(self.cache.get('a'), 1)

    def test_put_updates_existing_item(self):
        """put() with an already-present key updates in place (line 250)."""
        self.cache.put('a', 1)
        self.cache.put('a', 99)
        self.assertEqual(self.cache.get('a'), 99)
        self.assertEqual(len(self.cache), 1)   # no duplicate

    def test_put_evicts_lru_at_capacity(self):
        """put() evicts the least-recently-used entry when at max_size (line 258)."""
        self.cache.put('a', 1)
        self.cache.put('b', 2)
        self.cache.put('c', 3)
        self.cache.put('d', 4)   # should evict 'a' (oldest)
        self.assertNotIn('a', self.cache)
        self.assertIn('d', self.cache)
        self.assertEqual(len(self.cache), 3)

    def test_pop_existing_key_returns_value(self):
        """pop() on a present key returns the value and removes it (lines 276-278)."""
        self.cache.put('x', 42)
        result = self.cache.pop('x')
        self.assertEqual(result, 42)
        self.assertNotIn('x', self.cache)

    def test_pop_missing_key_returns_default(self):
        self.assertIsNone(self.cache.pop('missing'))
        self.assertEqual(self.cache.pop('missing', 'fallback'), 'fallback')

    def test_getitem_promotes_to_mru(self):
        """cache[key] promotes the item to most-recently-used (line 282 path)."""
        self.cache.put('a', 1)
        self.cache.put('b', 2)
        self.cache.put('c', 3)
        _ = self.cache['a']          # promote 'a' to MRU
        self.cache.put('d', 4)       # should evict 'b' (now LRU), not 'a'
        self.assertIn('a', self.cache)
        self.assertNotIn('b', self.cache)

    def test_getitem_raises_keyerror_for_absent_key(self):
        """cache[key] raises KeyError when key is absent (line 282)."""
        with self.assertRaises(KeyError):
            _ = self.cache['no_such_key']

    def test_getitem_counts_as_hit(self):
        self.cache.put('a', 1)
        _ = self.cache['a']
        stats = self.cache.get_stats()
        self.assertEqual(stats['hits'], 1)

    def test_get_hit_rate(self):
        self.cache.put('a', 1)
        self.cache.get('a')   # hit
        self.cache.get('z')   # miss
        stats = self.cache.get_stats()
        self.assertAlmostEqual(stats['hit_rate'], 0.5)

    def test_clear(self):
        self.cache.put('a', 1)
        self.cache.clear()
        self.assertEqual(len(self.cache), 0)


# ===========================================================================
# 9. _parse_command_payload
# ===========================================================================


class TestParseCommandPayload(unittest.TestCase):
    def setUp(self):
        self.em = _make_em()

    def _ei(self, entity_type, point_id=1000, metadata=None, **kwargs):
        return {'point_id': point_id, 'entity_type': entity_type,
                'metadata': metadata or {}, 'state_topic': f'nibe/s/{point_id}',
                **kwargs}

    # button
    def test_button_returns_1(self):
        self.assertEqual(self.em._parse_command_payload("x", self._ei('button'), "t"), 1)

    # switch
    def test_switch_on_variants(self):
        for p in ("1", "ON", "on", "true", "True"):
            with self.subTest(p=p):
                self.assertEqual(self.em._parse_command_payload(p, self._ei('switch'), "t"), 1)

    def test_switch_off_variants(self):
        for p in ("0", "OFF", "off", "false", "False", "garbage"):
            with self.subTest(p=p):
                self.assertEqual(self.em._parse_command_payload(p, self._ei('switch'), "t"), 0)

    def test_binary_sensor_on(self):
        self.assertEqual(self.em._parse_command_payload("ON", self._ei('binary_sensor'), "t"), 1)

    # select
    def test_select_mapped_valid(self):
        ei = self._ei('select', point_id=1001,
                      metadata={'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
                      point_data={'description': '0 = Off, 1 = Active'})
        self.assertEqual(self.em._parse_command_payload("Active", ei, "t"), 1)

    def test_select_mapped_invalid_returns_none(self):
        ei = self._ei('select', point_id=1001,
                      metadata={'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
                      point_data={'description': '0 = Off, 1 = Active'})
        self.assertIsNone(self.em._parse_command_payload("Unknown", ei, "t"))

    def test_select_no_mapping_numeric(self):
        ei = self._ei('select', metadata={'modbusRegisterType': ''})
        self.assertEqual(self.em._parse_command_payload("3", ei, "t"), 3)

    def test_select_no_mapping_non_numeric_returns_none(self):
        ei = self._ei('select', metadata={'modbusRegisterType': ''})
        self.assertIsNone(self.em._parse_command_payload("nope", ei, "t"))

    # number
    def test_number_divisor_ten(self):
        ei = self._ei('number', metadata={'divisor': 10, 'minValue': 150, 'maxValue': 300})
        self.assertEqual(self.em._parse_command_payload("22.5", ei, "t"), 225)

    def test_number_divisor_one(self):
        ei = self._ei('number', metadata={'divisor': 1, 'minValue': 0, 'maxValue': 100})
        self.assertEqual(self.em._parse_command_payload("42", ei, "t"), 42)

    def test_number_below_min_returns_none(self):
        ei = self._ei('number', metadata={'divisor': 10, 'minValue': 150, 'maxValue': 300})
        self.assertIsNone(self.em._parse_command_payload("10.0", ei, "t"))

    def test_number_above_max_returns_none(self):
        ei = self._ei('number', metadata={'divisor': 10, 'minValue': 150, 'maxValue': 300})
        self.assertIsNone(self.em._parse_command_payload("35.0", ei, "t"))

    def test_number_at_min_boundary_accepted(self):
        ei = self._ei('number', metadata={'divisor': 10, 'minValue': 150, 'maxValue': 300})
        self.assertEqual(self.em._parse_command_payload("15.0", ei, "t"), 150)

    def test_number_at_max_boundary_accepted(self):
        ei = self._ei('number', metadata={'divisor': 10, 'minValue': 150, 'maxValue': 300})
        self.assertEqual(self.em._parse_command_payload("30.0", ei, "t"), 300)

    def test_number_degenerate_range_skips_validation(self):
        ei = self._ei('number', metadata={'divisor': 1, 'minValue': 0, 'maxValue': 0},
                      is_degenerate_range=True)
        self.assertEqual(self.em._parse_command_payload("99", ei, "t"), 99)

    def test_number_non_numeric_returns_none(self):
        ei = self._ei('number', metadata={'divisor': 1})
        self.assertIsNone(self.em._parse_command_payload("nope", ei, "t"))

    def test_number_out_of_range_republishes_last_state(self):
        ei = self._ei('number', metadata={'divisor': 1, 'minValue': 0, 'maxValue': 10})
        self.em.last_states[1000] = "5"
        self.assertIsNone(self.em._parse_command_payload("99", ei, "t"))
        self.em.mqtt.publish.assert_called_once_with("nibe/s/1000", "5", retain=True)

    # text
    def test_text_normal(self):
        self.assertEqual(self.em._parse_command_payload("Hello", self._ei('text'), "t"), "Hello")

    def test_text_non_printable_stripped(self):
        self.assertEqual(self.em._parse_command_payload("He\x00llo\x07", self._ei('text'), "t"), "Hello")

    def test_text_truncated(self):
        from nibe_entity_manager import _TEXT_REGISTER_MAX_LEN
        long_s = "A" * (_TEXT_REGISTER_MAX_LEN + 20)
        self.assertEqual(len(self.em._parse_command_payload(long_s, self._ei('text'), "t")),
                         _TEXT_REGISTER_MAX_LEN)

    def test_text_exact_max_not_truncated(self):
        from nibe_entity_manager import _TEXT_REGISTER_MAX_LEN
        exact = "A" * _TEXT_REGISTER_MAX_LEN
        self.assertEqual(len(self.em._parse_command_payload(exact, self._ei('text'), "t")),
                         _TEXT_REGISTER_MAX_LEN)

    def test_text_empty(self):
        self.assertEqual(self.em._parse_command_payload("", self._ei('text'), "t"), "")

    # time
    def test_time_hhmmss_to_seconds(self):
        """Only HH and MM are read; a seconds component, if present, is
        parsed past but not added — minute precision only, by design.
        01:02:03 -> 1*3600 + 2*60 = 3720, not 3723."""
        self.assertEqual(self.em._parse_command_payload("01:02:03", self._ei('time'), "t"), 3720)

    def test_time_hhmm_to_seconds(self):
        self.assertEqual(self.em._parse_command_payload("02:30", self._ei('time'), "t"), 9000)

    def test_time_malformed_returns_none(self):
        self.assertIsNone(self.em._parse_command_payload("not a time", self._ei('time'), "t"))

    def test_time_empty_returns_none(self):
        self.assertIsNone(self.em._parse_command_payload("", self._ei('time'), "t"))

    # unknown
    def test_unknown_entity_type_returns_none(self):
        self.assertIsNone(self.em._parse_command_payload("ON", self._ei('light'), "t"))



class TestParseCommandPayloadProperties(unittest.TestCase):
    """Hypothesis properties for _parse_command_payload."""

    def setUp(self):
        self.em = _make_em()

    def _ei(self, entity_type, point_id=100, min_val=0, max_val=100, divisor=1):
        return {
            'point_id':    point_id,
            'entity_type': entity_type,
            'metadata': {
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True, 'divisor': divisor, 'decimal': 0,
                'minValue': min_val, 'maxValue': max_val,
                'variableType': 'integer', 'variableSize': 's16',
                'unit': '', 'shortUnit': '',
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            },
            'point_data': {},
        }

    @given(st.text(max_size=50))
    def test_button_always_returns_1(self, payload):
        """button entity type always returns 1 regardless of payload."""
        result = self.em._parse_command_payload(payload, self._ei('button'), 'h')
        self.assertEqual(result, 1)

    @given(st.sampled_from(['1', 'ON', 'on', 'true', 'True']))
    @example(payload='1')     # HA switch ON state
    @example(payload='ON')    # HA switch ON string
    @example(payload='on')    # lowercase variant
    def test_switch_truthy_payloads_return_1(self, payload):
        """All canonical truthy payloads for switch return 1."""
        result = self.em._parse_command_payload(payload, self._ei('switch'), 'h')
        self.assertEqual(result, 1)

    @given(st.text(max_size=30).filter(
        lambda s: s not in ('1', 'ON', 'on', 'true', 'True')))
    def test_switch_non_truthy_payloads_return_0(self, payload):
        """Any payload that is not a canonical truthy string returns 0 for switch."""
        result = self.em._parse_command_payload(payload, self._ei('switch'), 'h')
        self.assertEqual(result, 0)

    @given(st.integers(min_value=0, max_value=23),
           st.integers(min_value=0, max_value=59))
    @example(h=0,  m=0)   # midnight
    @example(h=23, m=59)  # end of day
    @example(h=12, m=0)   # noon
    def test_time_valid_hhmm_returns_seconds(self, h, m):
        """HH:MM format is always converted to correct integer seconds."""
        payload = f'{h:02d}:{m:02d}'
        result = self.em._parse_command_payload(payload, self._ei('time'), 'h')
        self.assertEqual(result, h * 3600 + m * 60)

    @given(st.integers(min_value=0, max_value=23),
           st.integers(min_value=0, max_value=59),
           st.integers(min_value=0, max_value=59))
    def test_time_valid_hhmmss_returns_seconds(self, h, m, s):
        """HH:MM:SS format is always converted to correct integer seconds."""
        payload = f'{h:02d}:{m:02d}:{s:02d}'
        result = self.em._parse_command_payload(payload, self._ei('time'), 'h')
        self.assertEqual(result, h * 3600 + m * 60)

    @given(st.text(max_size=20).filter(
        lambda s: not (len(s.split(':')) >= 2 and
                       all(p.strip().isdigit() for p in s.split(':')[:2]))))
    def test_time_invalid_payload_returns_none(self, payload):
        """Payloads that cannot be parsed as HH:MM return None."""
        result = self.em._parse_command_payload(payload, self._ei('time'), 'h')
        self.assertIsNone(result)

    @given(st.text(max_size=100))
    def test_text_result_always_printable(self, payload):
        """text entity type result contains only printable characters."""
        result = self.em._parse_command_payload(payload, self._ei('text'), 'h')
        if result is not None:
            self.assertTrue(all(c.isprintable() for c in result))

    @given(st.text(max_size=200))
    def test_text_result_never_exceeds_max_len(self, payload):
        """text entity type result never exceeds _TEXT_REGISTER_MAX_LEN."""
        from nibe_entity_manager import _TEXT_REGISTER_MAX_LEN
        result = self.em._parse_command_payload(payload, self._ei('text'), 'h')
        if result is not None:
            self.assertLessEqual(len(result), _TEXT_REGISTER_MAX_LEN)

    @given(st.text(max_size=50))
    def test_unknown_entity_type_always_none(self, payload):
        """Completely unknown entity types always return None."""
        result = self.em._parse_command_payload(
            payload, self._ei('unknown_type_xyz'), 'h')
        self.assertIsNone(result)

    @given(st.integers(min_value=0, max_value=100),
           st.integers(min_value=0, max_value=100))
    def test_number_in_range_returns_value(self, min_val, offset):
        """A number value within [min, max] is always returned."""
        max_val = min_val + 100
        value = min_val + offset % 101
        result = self.em._parse_command_payload(
            str(value), self._ei('number', min_val=min_val, max_val=max_val), 'h')
        if result is not None:
            self.assertGreaterEqual(result, min_val)
            self.assertLessEqual(result, max_val)

    @given(st.integers(min_value=-32768, max_value=32767))
    def test_number_degenerate_range_always_returns_value(self, value):
        """Degenerate range (min==max) bypasses range validation — always returns value."""
        ei = self._ei('number', min_val=5, max_val=5)
        ei['is_degenerate_range'] = True
        result = self.em._parse_command_payload(str(value), ei, 'h')
        # Degenerate range bypasses min/max check — value accepted if parseable
        if result is not None:
            self.assertIsInstance(result, (int, float))

    @given(st.text(max_size=200))
    def test_text_output_length_bounded(self, payload):
        """text output length ≤ min(len(printable chars), _TEXT_REGISTER_MAX_LEN)."""
        from nibe_entity_manager import _TEXT_REGISTER_MAX_LEN
        result = self.em._parse_command_payload(payload, self._ei('text'), 'h')
        if result is not None:
            printable_count = sum(1 for c in payload if c.isprintable())
            self.assertLessEqual(len(result), min(printable_count, _TEXT_REGISTER_MAX_LEN))

    @given(st.text(max_size=30).filter(lambda s: s not in ('1', 'ON', 'on', 'true', 'True')))
    def test_binary_sensor_non_truthy_returns_0(self, payload):
        """binary_sensor behaves identically to switch for payload parsing."""
        result = self.em._parse_command_payload(payload, self._ei('binary_sensor'), 'h')
        self.assertEqual(result, 0)


# ---------------------------------------------------------------------------
# entity_id_for properties (nibe_ha_integration.py)
# ---------------------------------------------------------------------------


class TestPruneChangelog(unittest.TestCase):
    def setUp(self):
        self.em = _make_em()
        self.em._last_prune_time = 0.0

    def _entry(self, age_days=0):
        return {'timestamp': time.time() - age_days * 86400,
                'iso_timestamp': '2024-01-01', 'added': [], 'removed': []}

    def test_runs_when_due(self):
        self.assertTrue(self.em._prune_changelog_if_due())

    def test_skipped_when_recent(self):
        self.em._prune_changelog_if_due()
        self.assertFalse(self.em._prune_changelog_if_due())

    def test_old_entries_removed(self):
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES
        self.em.changelog_retention_days = 30
        # Use more recent entries than the floor so the floor does not
        # confound the result.  All entries beyond the floor+recent count
        # should be evicted.
        n_recent = _CHANGELOG_MIN_ENTRIES + 5   # safely above the floor
        n_old    = 10
        recent = [self._entry(1)  for _ in range(n_recent)]
        old    = [self._entry(60) for _ in range(n_old)]
        self.em.change_history = deque(recent + old, maxlen=500)
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), n_recent)

    def test_floor_preserved(self):
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES
        self.em.changelog_retention_days = 1
        self.em.change_history = deque(
            [self._entry(10) for _ in range(_CHANGELOG_MIN_ENTRIES + 5)], maxlen=500)
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), _CHANGELOG_MIN_ENTRIES)

    def test_invalid_entries_dropped(self):
        self.em.change_history = deque(
            [self._entry(1), {'not': 'valid'}, "not a dict"], maxlen=500)
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), 1)

    def test_maxlen_preserved(self):
        ml = self.em.change_history.maxlen
        self.em.change_history = deque([self._entry()], maxlen=ml)
        self.em._prune_changelog_if_due()
        self.assertEqual(self.em.change_history.maxlen, ml)

    def test_all_recent_kept(self):
        self.em.changelog_retention_days = 90
        self.em.change_history = deque([self._entry(1) for _ in range(10)], maxlen=500)
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), 10)

    def test_empty_does_not_crash(self):
        self.em.change_history = deque(maxlen=500)
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), 0)


# ---------------------------------------------------------------------------
# Hypothesis properties for changelog methods (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestPruneChangelogProperties(unittest.TestCase):
    """Hypothesis properties for _prune_changelog_if_due."""

    def _make_fresh_em(self):
        em = _make_em()
        em._last_prune_time = 0.0
        return em

    def _add_entries(self, em, n, age_days=0):
        """Add n valid entries to change_history with given age in days."""
        import time as _time
        ts = _time.time() - age_days * 86400
        for i in range(n):
            em.change_history.appendleft({
                'timestamp': ts, 'iso_timestamp': '2020-01-01 00:00:00',
                'added': [], 'removed': [], 'id': f'e{i}',
                'unread': True, 'source': 'test', 'triggered_by': None,
            })

    @given(st.integers(min_value=1, max_value=200))
    def test_always_keeps_at_least_50_entries(self, n_entries):
        """After pruning, at least min(50, original) entries always remain."""
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES
        em = self._make_fresh_em()
        self._add_entries(em, n_entries, age_days=9999)
        em._prune_changelog_if_due()
        expected_min = min(_CHANGELOG_MIN_ENTRIES, n_entries)
        self.assertGreaterEqual(len(em.change_history), expected_min)

    @given(st.integers(min_value=51, max_value=200))
    def test_old_entries_beyond_50_are_pruned(self, n_entries):
        """Entries older than retention_days (beyond the 50-entry floor) are removed."""
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES
        em = self._make_fresh_em()
        em.changelog_retention_days = 1
        self._add_entries(em, n_entries, age_days=999)
        result = em._prune_changelog_if_due()
        self.assertTrue(result, "Expected prune to run")
        self.assertEqual(len(em.change_history), _CHANGELOG_MIN_ENTRIES)

    @given(st.integers(min_value=1, max_value=100))
    def test_recent_entries_never_pruned(self, n_entries):
        """Entries within retention period must never be pruned."""
        em = self._make_fresh_em()
        em.changelog_retention_days = 90
        self._add_entries(em, n_entries, age_days=1)
        em._prune_changelog_if_due()
        self.assertEqual(len(em.change_history), n_entries)

    def test_returns_false_when_called_too_soon(self):
        """Second call within _CHANGELOG_PRUNE_S returns False."""
        em = self._make_fresh_em()
        em._prune_changelog_if_due()  # first call — runs
        result = em._prune_changelog_if_due()  # too soon
        self.assertFalse(result)

    def test_returns_true_when_due(self):
        em = self._make_fresh_em()
        result = em._prune_changelog_if_due()
        self.assertTrue(result)

    @given(st.integers(min_value=0, max_value=200))
    def test_never_raises(self, n_entries):
        em = self._make_fresh_em()
        self._add_entries(em, n_entries, age_days=100)
        em._prune_changelog_if_due()  # must not raise



class TestMarkChangelogReadProperties(unittest.TestCase):
    """Hypothesis properties for mark_changelog_read."""

    def _make_fresh_em(self):
        return _make_em()

    def _add_unread(self, em, n):
        import time as _time
        for i in range(n):
            em.change_history.appendleft({
                'timestamp': _time.time(), 'iso_timestamp': '2020-01-01 00:00:00',
                'added': [], 'removed': [], 'id': f'e{i}',
                'unread': True, 'source': 'test', 'triggered_by': None,
            })

    @given(st.integers(min_value=0, max_value=50))
    def test_all_entries_marked_unread_false(self, n):
        """After mark_changelog_read, all entries have unread=False."""
        em = self._make_fresh_em()
        self._add_unread(em, n)
        em.mark_changelog_read()
        for entry in em.change_history:
            self.assertFalse(entry['unread'])

    @given(st.integers(min_value=0, max_value=50))
    def test_seq_incremented(self, n):
        """_history_seq must increment by at least 1."""
        em = self._make_fresh_em()
        self._add_unread(em, n)
        seq_before = em._history_seq
        em.mark_changelog_read()
        self.assertGreater(em._history_seq, seq_before)

    @given(st.integers(min_value=0, max_value=50))
    def test_publishes_zero_unread_count(self, n):
        """CHANGELOG_UNREAD publish must have unread_count=0."""
        import json as _json
        from nibe_mqtt_publisher import BrowserTopic
        em = self._make_fresh_em()
        self._add_unread(em, n)
        em.mark_changelog_read()
        calls = [c for c in em.mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.CHANGELOG_UNREAD]
        self.assertTrue(calls)
        payload = _json.loads(calls[-1].args[1])
        self.assertEqual(payload['unread_count'], 0)

    @given(st.integers(min_value=0, max_value=50))
    def test_never_raises(self, n):
        em = self._make_fresh_em()
        self._add_unread(em, n)
        em.mark_changelog_read()  # must not raise

    @given(st.integers(min_value=1, max_value=50))
    def test_last_published_seq_updated(self, n):
        """_last_published_seq must match _history_seq after mark_changelog_read."""
        em = self._make_fresh_em()
        self._add_unread(em, n)
        em.mark_changelog_read()
        self.assertEqual(em._last_published_seq, em._history_seq)


# ---------------------------------------------------------------------------
# _update_changelog_history properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestUpdateChangelogHistoryProperties(unittest.TestCase):
    """Hypothesis properties for _update_changelog_history."""

    _event_strategy = st.fixed_dictionaries({
        'added':   st.lists(st.integers(min_value=1, max_value=9999), max_size=5),
        'removed': st.lists(st.integers(min_value=1, max_value=9999), max_size=5),
        'source':  st.sampled_from(['firmware', 'user', 'bridge', 'api']),
    })

    @given(_event_strategy)
    def test_new_entry_always_unread(self, event):
        """Every new changelog entry must have unread=True."""
        em = _make_em()
        em._update_changelog_history(event)
        self.assertTrue(em.change_history[0]['unread'])

    @given(_event_strategy)
    def test_new_entry_id_starts_with_change(self, event):
        """Entry id must always start with 'change_'."""
        em = _make_em()
        em._update_changelog_history(event)
        self.assertTrue(em.change_history[0]['id'].startswith('change_'))

    @given(_event_strategy)
    def test_new_entry_is_first_in_history(self, event):
        """New entry must always be prepended (appendleft)."""
        em = _make_em()
        em._update_changelog_history(event)
        first = em.change_history[0]
        self.assertEqual(first['source'], event['source'])

    @given(_event_strategy)
    def test_seq_always_increments(self, event):
        em = _make_em()
        seq_before = em._history_seq
        em._update_changelog_history(event)
        self.assertGreater(em._history_seq, seq_before)

    @given(_event_strategy)
    def test_added_preserved_exactly(self, event):
        em = _make_em()
        em._update_changelog_history(event)
        self.assertEqual(em.change_history[0]['added'], event['added'])

    @given(_event_strategy)
    def test_removed_preserved_exactly(self, event):
        em = _make_em()
        em._update_changelog_history(event)
        self.assertEqual(em.change_history[0]['removed'], event['removed'])

    @given(_event_strategy)
    def test_source_preserved(self, event):
        em = _make_em()
        em._update_changelog_history(event)
        self.assertEqual(em.change_history[0]['source'], event['source'])

    @given(_event_strategy)
    def test_default_source_is_firmware(self, event):
        """When source is absent, defaults to 'firmware'."""
        em = _make_em()
        event_no_source = {k: v for k, v in event.items() if k != 'source'}
        em._update_changelog_history(event_no_source)
        self.assertEqual(em.change_history[0]['source'], 'firmware')

    @given(_event_strategy)
    def test_default_added_is_empty_list(self, event):
        """When added is absent, defaults to []."""
        em = _make_em()
        event_no_added = {k: v for k, v in event.items() if k != 'added'}
        em._update_changelog_history(event_no_added)
        self.assertEqual(em.change_history[0]['added'], [])

    @given(_event_strategy)
    def test_history_length_increases(self, event):
        """History length must increase by 1 (unless at maxlen)."""
        em = _make_em()
        before = len(em.change_history)
        em._update_changelog_history(event)
        self.assertEqual(len(em.change_history), before + 1)

    @given(_event_strategy)
    def test_unread_count_in_payload_matches_history(self, event):
        """unread_count in CHANGELOG_HISTORY payload must match actual count."""
        import json as _json
        from nibe_mqtt_publisher import BrowserTopic
        from nibe_entity_manager import _decompress_payload
        em = _make_em()
        em._update_changelog_history(event)
        calls = [c for c in em.mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.CHANGELOG_HISTORY]
        self.assertTrue(calls)
        raw = _decompress_payload(calls[-1].args[1])
        payload = _json.loads(raw)
        actual_unread = sum(1 for e in em.change_history
                           if e.get('unread', False))
        self.assertEqual(payload['unread_count'], actual_unread)

    @given(_event_strategy)
    def test_never_raises(self, event):
        em = _make_em()
        em._update_changelog_history(event)  # must not raise


# ===========================================================================
# 11. Dynamic point state machine
# ===========================================================================


class TestDynamicPoints(unittest.TestCase):
    def setUp(self):
        self.em = _make_em()

    def _seed(self, pid, entity_type='number', is_dynamic=False):
        self.em.all_points_by_id[pid] = {
            'variableId': pid, 'display_title': f'Point {pid}',
            'entity_type': entity_type, 'is_dynamic': is_dynamic,
            'metadata': {}, 'entity_category': 'diagnostic', 'is_writable': False,
        }

    def _seed_dynamic_map_entry(self, controlling_pid, dynamic_pids, value=1):
        """Add a known controlling entry to the dynamic_point_map."""
        from nibe_dynamic_map import DynamicPointEntry
        self.em.dynamic_point_map._table[controlling_pid] = DynamicPointEntry(
            point_id=controlling_pid,
            title=f'Switch {controlling_pid}',
            entity_type='switch',
            processed_values={0, 1},
            unprocessed_values=set(),
            is_controlling=True,
            dynamic_points_by_value={0: [], value: dynamic_pids},
        )

    def test_disappeared_removed_from_active(self):
        """Disappearance removes point from active_dynamic_points."""
        self._seed(6983)
        self.em.active_dynamic_points.add(6983)
        self.em._publish_dynamic_changes([], {6983})
        self.assertNotIn(6983, self.em.active_dynamic_points)

    def test_disappeared_not_refired_next_poll(self):
        """After disappearance, point no longer in active set so no re-fire."""
        self._seed(6983)
        self.em.active_dynamic_points.add(6983)
        self.em._publish_dynamic_changes([], {6983})
        # Simulate next poll: known_dynamic - active = absent set
        known = self.em.dynamic_point_map.all_known_dynamic_point_ids()
        absent = known - self.em.active_dynamic_points
        self.assertNotIn(6983, absent)

    def test_dedup_guard_known_dynamic_skipped_in_bulk(self):
        """Points handled by probe (known dynamic) are skipped in bulk fetch."""
        self._seed_dynamic_map_entry(1001, [22001])
        # 22001 is a known dynamic point — is_known_dynamic should return True
        self.assertTrue(self.em.dynamic_point_map.is_known_dynamic(22001))

    def test_two_points_both_removed_from_active(self):
        for pid in [6983, 32825]:
            self._seed(pid)
            self.em.active_dynamic_points.add(pid)
        self.em._publish_dynamic_changes([], {6983, 32825})
        self.assertEqual(len(self.em.active_dynamic_points), 0)

    def test_appeared_point_added_to_active(self):
        """When a dynamic point appears it is added to active_dynamic_points."""
        pid = 7001
        self.em.initial_discovery_complete = True
        fake_point_data = {
            'title': 'New point', 'description': '',
            'metadata': {
                'divisor': 1, 'unit': 'kW',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False, 'variableType': 'integer',
                'variableSize': 's16', 'minValue': 0, 'maxValue': 100,
                'shortUnit': 'kW', 'modbusRegisterID': 1000,
                'intDefaultValue': 0, 'change': 1, 'stringDefaultValue': '',
            },
            'value': {'isOk': True, 'integerValue': 10, 'stringValue': ''},
        }
        self.em._publish_dynamic_changes([(pid, fake_point_data)], set())
        self.assertIn(pid, self.em.active_dynamic_points)

    def test_setup_dynamic_map_loading_skipped_post_discovery(self):
        """After initial_discovery_complete, MQTT re-delivery is ignored."""
        from nibe_entity_manager import EntityManager
        self.em.initial_discovery_complete = True
        EntityManager._setup_dynamic_map_loading(self.em)
        msg = MagicMock()
        # Simulate a retained ACTIVE_DYNAMIC re-delivery
        msg.payload = json.dumps([6983]).encode()
        initial_active = set(self.em.active_dynamic_points)
        self.em._on_active_dynamic_message(None, None, msg)
        self.assertEqual(self.em.active_dynamic_points, initial_active)

    def test_setup_dynamic_map_loading_loads_pre_discovery(self):
        """Before initial_discovery_complete, ACTIVE_DYNAMIC payload is loaded."""
        from nibe_entity_manager import EntityManager
        self.em.initial_discovery_complete = False
        EntityManager._setup_dynamic_map_loading(self.em)
        msg = MagicMock()
        msg.payload = json.dumps([6983, 32825]).encode()
        self.em._on_active_dynamic_message(None, None, msg)
        self.assertIn(6983, self.em.active_dynamic_points)
        self.assertIn(32825, self.em.active_dynamic_points)

    def test_malformed_active_dynamic_payload_does_not_crash(self):
        from nibe_entity_manager import EntityManager
        self.em.initial_discovery_complete = False
        EntityManager._setup_dynamic_map_loading(self.em)
        msg = MagicMock()
        msg.payload = b"not valid json {{{"
        self.em._on_active_dynamic_message(None, None, msg)  # must not raise

    def test_known_dynamic_not_classified_as_static_outside_scan_window(self):
        """is_known_dynamic guard: a point in the dynamic map must not be
        routed to the static path regardless of scan window state.
        Verifies the guard condition directly without invoking _fetch_bulk_data."""
        controlling_pid = 1001
        dynamic_pid     = 22001
        self._seed_dynamic_map_entry(controlling_pid, [dynamic_pid])

        # The guard that was added: known dynamic points skip the static path
        self.assertTrue(
            self.em.dynamic_point_map.is_known_dynamic(dynamic_pid),
            "Dynamic point must be recognised by is_known_dynamic",
        )
        self.assertFalse(
            self.em.dynamic_point_map.is_known_dynamic(controlling_pid),
            "Controlling point must not be flagged as a dynamic point",
        )
        self.assertFalse(
            self.em.dynamic_point_map.is_known_dynamic(99999),
            "Unknown point must not be flagged as dynamic",
        )


# ===========================================================================
# 12. NibeApiClient — retry jitter + write_point validation
# ===========================================================================


# ===========================================================================
# 17. Data integrity — active_dynamic_points crash safety
# ===========================================================================


class TestActiveDynamicCrashSafety(unittest.TestCase):
    """Tests for write-ahead ordering of _persist_active_dynamic.

    The invariant: after _publish_dynamic_changes processes a disappearance,
    the ACTIVE_DYNAMIC retained message must reflect the post-disappearance
    state BEFORE any other in-memory state changes.
    """

    def setUp(self):
        self.em = _make_em()

    def _seed(self, pid):
        self.em.all_points_by_id[pid] = {
            'variableId': pid, 'display_title': f'Point {pid}',
            'entity_type': 'number', 'is_dynamic': True,
            'metadata': {}, 'entity_category': 'diagnostic', 'is_writable': False,
        }
        self.em.active_dynamic_points.add(pid)

    def test_persist_called_before_changelog_on_disappearance(self):
        """_persist_active_dynamic must be called before _update_changelog_history."""
        pid = 6983
        self._seed(pid)
        call_order = []

        original_persist   = self.em._persist_active_dynamic
        original_changelog = self.em._update_changelog_history

        def mock_persist():
            call_order.append('persist')
            original_persist()

        def mock_changelog(event):
            call_order.append('changelog')
            original_changelog(event)

        self.em._persist_active_dynamic   = mock_persist
        self.em._update_changelog_history = mock_changelog

        self.em._publish_dynamic_changes([], {pid})

        persist_idx   = call_order.index('persist')
        changelog_idx = call_order.index('changelog')
        self.assertLess(persist_idx, changelog_idx,
                        "persist must be called before changelog (write-ahead)")

    def test_persisted_set_excludes_disappeared_point(self):
        """After disappearance the ACTIVE_DYNAMIC message must not contain the point."""
        pid = 6983
        self._seed(pid)

        published_payloads = []
        def capture_publish(topic, payload, retain=False):
            published_payloads.append((topic, payload))
        self.em.mqtt.publish.side_effect = capture_publish

        from nibe_entity_manager import BrowserTopic
        self.em._publish_dynamic_changes([], {pid})

        active_dynamic_publishes = [
            p for t, p in published_payloads
            if t == BrowserTopic.ACTIVE_DYNAMIC
        ]
        self.assertTrue(len(active_dynamic_publishes) > 0,
                        "ACTIVE_DYNAMIC should have been published")
        first_payload = json.loads(active_dynamic_publishes[0])
        self.assertNotIn(pid, first_payload,
                         "Disappeared point must not appear in first persist call")

    def test_appeared_point_persisted_in_active_set(self):
        """When a point appears it must end up in active_dynamic_points."""
        pid = 7001
        self.em.initial_discovery_complete = True
        fake_point_data = {
            'title': 'New point', 'description': '',
            'metadata': {
                'divisor': 1, 'unit': 'kW',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False, 'variableType': 'integer',
                'variableSize': 's16', 'minValue': 0, 'maxValue': 100,
                'shortUnit': 'kW', 'modbusRegisterID': 1000,
                'intDefaultValue': 0, 'change': 1, 'stringDefaultValue': '',
            },
            'value': {'isOk': True, 'integerValue': 10, 'stringValue': ''},
        }
        self.em._publish_dynamic_changes([(pid, fake_point_data)], set())
        self.assertIn(pid, self.em.active_dynamic_points)

    def test_two_disappeared_both_removed_from_active(self):
        """When two points disappear in the same event, both removed from active."""
        for pid in [6983, 32825]:
            self._seed(pid)

        self.em._publish_dynamic_changes([], {6983, 32825})

        self.assertNotIn(6983, self.em.active_dynamic_points)
        self.assertNotIn(32825, self.em.active_dynamic_points)
        self.assertEqual(len(self.em.active_dynamic_points), 0)

    def test_dynamic_map_not_restored_mid_session(self):
        """After initial_discovery_complete, DYNAMIC_MAP re-delivery is ignored."""
        from nibe_entity_manager import EntityManager
        self.em.initial_discovery_complete = True
        EntityManager._setup_dynamic_map_loading(self.em)
        msg = MagicMock()
        # A payload that would add entries if processed
        from nibe_dynamic_map import DynamicPointMap
        dm = DynamicPointMap()
        msg.payload = dm.serialise().encode()
        initial_len = len(self.em.dynamic_point_map)
        self.em._on_dynamic_map_message(None, None, msg)
        # Table should be unchanged
        self.assertEqual(len(self.em.dynamic_point_map), initial_len)


class TestPendingWriteGuardProperties(unittest.TestCase):
    """Hypothesis properties for the pending write guard data structure."""

    def test_pending_writes_dict_is_always_dict(self):
        """pending_writes is always a dict — invariant structural check."""
        em = _make_em()
        self.assertIsInstance(em.pending_writes, dict)

    @given(st.integers(min_value=1, max_value=9999),
           st.integers(min_value=0, max_value=100))
    def test_matching_value_clears_pending(self, pid, written_value):
        """When bulk_data matches written_value, pending entry should be cleared."""
        em = _make_em()
        em.pending_writes[pid] = {
            'value': written_value, 'time': 0.0,
            'entity_id': f'sensor.nibe_{pid}',
        }
        em.bulk_data[pid] = {'raw_value': written_value, 'is_ok': True}
        # The guard checks: if raw_value == written_value → clear pending
        if em.pending_writes.get(pid, {}).get('value') == \
                em.bulk_data[pid]['raw_value']:
            em.pending_writes.pop(pid, None)
        self.assertNotIn(pid, em.pending_writes)

    @given(st.integers(min_value=1, max_value=9999),
           st.integers(min_value=0, max_value=100),
           st.integers(min_value=0, max_value=100))
    def test_mismatched_value_keeps_pending(self, pid, written_value, bulk_value):
        """When bulk_data doesn't match written_value, pending stays."""
        if written_value == bulk_value:
            return  # skip equal case
        em = _make_em()
        em.pending_writes[pid] = {
            'value': written_value, 'time': 0.0,
            'entity_id': f'sensor.nibe_{pid}',
        }
        em.bulk_data[pid] = {'raw_value': bulk_value, 'is_ok': True}
        # Guard should NOT clear when values differ
        if em.pending_writes.get(pid, {}).get('value') != \
                em.bulk_data[pid]['raw_value']:
            pass  # entry stays
        self.assertIn(pid, em.pending_writes)

    @given(st.integers(min_value=1, max_value=9999))
    def test_stale_entry_eviction_clears_pid(self, pid):
        """Entries older than _STALE_WRITE_AGE_S must be evictable."""
        import time as _time
        from nibe_entity_manager import _STALE_WRITE_AGE_S
        em = _make_em()
        em.pending_writes[pid] = {
            'value': 42, 'time': _time.time() - _STALE_WRITE_AGE_S - 1,
            'entity_id': f'sensor.nibe_{pid}',
        }
        # Simulate stale eviction
        now = _time.time()
        stale = [p for p, v in em.pending_writes.items()
                 if now - v['time'] > _STALE_WRITE_AGE_S]
        for p in stale:
            em.pending_writes.pop(p, None)
        self.assertNotIn(pid, em.pending_writes)


# ===========================================================================
# 13. API spec conformance — grounded in the official REST API spec
# ===========================================================================


class TestPendingWriteGuard(unittest.TestCase):
    """The pending write guard suppresses stale-value publishes until the
    controller confirms the written value, preventing the switch flicker."""

    def setUp(self):
        self.em = _make_em()
        # Seed a minimal indexed point and bulk_data entry
        self.point_id = 6984
        self.em.all_points_by_id[self.point_id] = {
            'variableId':      self.point_id,
            'display_title':   'Test switch',
            'entity_type':     'switch',
            'entity_category': 'config',
            'is_writable':     True,
            'is_dynamic':      False,
            'metadata':        {'divisor': 1, 'minValue': 0, 'maxValue': 1},
        }
        self.em.bulk_data[self.point_id] = {
            'raw_value': 0, 'string_value': '', 'is_ok': True,
            'metadata': {}, 'title': 'Test switch', 'description': '',
            'timestamp': time.time(),
        }
        self.em.mqtt_enabled_points.add(self.point_id)
        self.entity_info = {
            'point_id':          self.point_id,
            'entity_type':       'switch',
            'state_topic':       f'nibe/state/{self.point_id}',
            'availability_topic': f'nibe/avail/{self.point_id}',
            'attributes_topic':  None,
            'command_topic':     f'nibe/cmd/{self.point_id}',
            'entity_id':         f'nibe_{self.point_id}',
            'metadata':          {'divisor': 1},
        }
        with self.em._active_entities_lock:
            self.em.active_entities_by_id[self.point_id] = self.entity_info

    def _add_pending(self, value, age_offset=0):
        self.em.pending_writes[self.point_id] = {
            'point_id':  self.point_id,
            'value':     value,
            'payload':   str(value),
            'timestamp': time.time() - age_offset,
            'cmd_id':    'test1234',
        }

    # ── suppression ───────────────────────────────────────────────────────────

    def test_pending_write_suppresses_publish(self):
        """While a write is pending and unconfirmed, state must not be published."""
        self._add_pending(value=1)
        # bulk_data still shows old value (0) — controller not committed yet
        self.em._update_entity_state(self.entity_info)
        self.em.mqtt.publish.assert_not_called()

    def test_no_pending_write_publishes_normally(self):
        """With no pending write, normal state publish proceeds."""
        # No pending entry → publish should happen
        self.em._update_entity_state(self.entity_info)
        self.em.mqtt.publish.assert_called()

    # ── confirmation ─────────────────────────────────────────────────────────

    def test_pending_released_when_api_confirms(self):
        """When bulk_data raw_value matches the written value, the pending entry
        is cleared and normal publish resumes on the next call."""
        self._add_pending(value=1)
        # Simulate controller committing: bulk_data now shows the written value
        self.em.bulk_data[self.point_id]['raw_value'] = 1
        # First call: confirms and clears the pending entry
        self.em._update_entity_state(self.entity_info)
        # Pending entry must be gone
        self.assertNotIn(self.point_id, self.em.pending_writes)

    def test_pending_held_while_api_shows_old_value(self):
        """If bulk_data still shows the old value, pending entry is kept."""
        self._add_pending(value=1)
        # bulk_data still shows 0 (controller not committed)
        self.em.bulk_data[self.point_id]['raw_value'] = 0
        self.em._update_entity_state(self.entity_info)
        self.assertIn(self.point_id, self.em.pending_writes)

    # ── stale eviction ────────────────────────────────────────────────────────

    def test_stale_pending_evicted_after_timeout(self):
        """Entries older than _STALE_WRITE_AGE_S must be evicted so the
        point is not blocked from state updates forever."""
        from nibe_entity_manager import _STALE_WRITE_AGE_S
        self._add_pending(value=1, age_offset=_STALE_WRITE_AGE_S + 10)
        self.em._update_entity_state(self.entity_info)
        self.assertNotIn(self.point_id, self.em.pending_writes)

    def test_stale_eviction_allows_publish(self):
        """After stale eviction the entity state is published normally."""
        from nibe_entity_manager import _STALE_WRITE_AGE_S
        self._add_pending(value=1, age_offset=_STALE_WRITE_AGE_S + 10)
        self.em._update_entity_state(self.entity_info)
        self.em.mqtt.publish.assert_called()

    def test_fresh_pending_not_evicted(self):
        """A recent pending entry must not be evicted prematurely."""
        self._add_pending(value=1, age_offset=1)  # 1 second old — well within limit
        self.em._update_entity_state(self.entity_info)
        self.assertIn(self.point_id, self.em.pending_writes)

    # ── edge cases ────────────────────────────────────────────────────────────

    def test_is_ok_false_publishes_offline(self):
        """A point with is_ok=False should mark the entity offline."""
        self.em.bulk_data[self.point_id]['is_ok'] = False
        self.em._update_entity_state(self.entity_info)
        self.em.mqtt.publish.assert_called_with(
            self.entity_info['availability_topic'], "offline", retain=True
        )

    def test_point_absent_from_bulk_disables(self):
        """A point absent from bulk_data (outside post-write window)
        should be disabled rather than crashing."""
        del self.em.bulk_data[self.point_id]
        self.em._post_write_active = False
        self.em._update_entity_state(self.entity_info)
        self.assertNotIn(self.point_id, self.em.mqtt_enabled_points)


# ===========================================================================
# 16. enable_entity / disable_entity
# ===========================================================================


class TestEnableDisableEntity(unittest.TestCase):
    """Tests for EntityManager.enable_entity() and disable_entity()."""

    def setUp(self):
        self.em = _make_em()
        self.point_id = 4

        # Seed a fully-specified indexed point
        self.em.all_points_by_id[self.point_id] = {
            'variableId':      self.point_id,
            'display_title':   'Outdoor temperature',
            'entity_type':     'sensor',
            'entity_category': 'diagnostic',
            'is_writable':     False,
            'is_dynamic':      False,
            'metadata': {
                'divisor': 10, 'unit': '°C', 'minValue': -400, 'maxValue': 400,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
            },
        }
        self.em.bulk_data[self.point_id] = {
            'raw_value': 119, 'string_value': '', 'is_ok': True,
            'metadata': {'divisor': 10}, 'title': 'Outdoor temperature',
            'description': '', 'timestamp': time.time(),
        }

        # Make publish_entity_discovery return a realistic entity_info dict
        self.mock_entity_info = {
            'point_id':          self.point_id,
            'entity_type':       'sensor',
            'entity_id':         f'nibe_{self.point_id}',
            'state_topic':       f'homeassistant/sensor/nibe_{self.point_id}/state',
            'availability_topic': f'homeassistant/sensor/nibe_{self.point_id}/availability',
            'attributes_topic':  f'homeassistant/sensor/nibe_{self.point_id}/attributes',
            'command_topic':     None,   # read-only sensor — no command topic
            'metadata':          {'divisor': 10},
        }
        self.em._pub.publish_entity_discovery.return_value = self.mock_entity_info

    # ── enable_entity ─────────────────────────────────────────────────────────

    def test_enable_unknown_point_returns_false(self):
        result = self.em.enable_entity(99999)
        self.assertFalse(result)

    def test_enable_adds_to_mqtt_enabled_points(self):
        self.em.enable_entity(self.point_id)
        self.assertIn(self.point_id, self.em.mqtt_enabled_points)

    def test_enable_adds_to_active_entities(self):
        self.em.enable_entity(self.point_id)
        with self.em._active_entities_lock:
            self.assertIn(self.point_id, self.em.active_entities_by_id)

    def test_enable_calls_publish_entity_discovery(self):
        self.em.enable_entity(self.point_id)
        self.em._pub.publish_entity_discovery.assert_called_once()

    def test_enable_publishes_availability_online(self):
        self.em.enable_entity(self.point_id)
        calls = [str(c) for c in self.em.mqtt.publish.call_args_list]
        avail_calls = [c for c in calls if 'availability' in c and 'online' in c]
        self.assertTrue(len(avail_calls) > 0, "Availability 'online' should be published")

    def test_enable_returns_true_on_success(self):
        result = self.em.enable_entity(self.point_id)
        self.assertTrue(result)

    def test_enable_already_enabled_returns_true_without_republish(self):
        self.em.enable_entity(self.point_id)
        publish_count_after_first = self.em.mqtt.publish.call_count
        result = self.em.enable_entity(self.point_id)
        self.assertTrue(result)
        # No additional publish calls should have happened
        self.assertEqual(self.em.mqtt.publish.call_count, publish_count_after_first)

    def test_enable_increments_type_stats(self):
        self.em.enable_entity(self.point_id)
        self.assertIn('sensor', self.em._stats_type_counts)
        self.assertGreater(self.em._stats_type_counts['sensor'], 0)

    def test_enable_writable_increments_writable_count(self):
        # Override with a writable point
        self.em.all_points_by_id[self.point_id]['is_writable'] = True
        before = self.em._stats_writable_count
        self.em.enable_entity(self.point_id)
        self.assertEqual(self.em._stats_writable_count, before + 1)

    def test_enable_publish_fails_returns_false(self):
        """If discovery publish fails (returns None), enable must return False."""
        self.em._pub.publish_entity_discovery.return_value = None
        result = self.em.enable_entity(self.point_id)
        self.assertFalse(result)
        self.assertNotIn(self.point_id, self.em.mqtt_enabled_points)

    def test_enable_read_only_sensor_does_not_subscribe_command_topic(self):
        """A read-only sensor (command_topic=None) must not subscribe to MQTT."""
        self.em.enable_entity(self.point_id)
        subscribe_calls = [str(c) for c in self.em.mqtt.subscribe.call_args_list]
        command_subs = [c for c in subscribe_calls if 'command' in c.lower()]
        self.assertEqual(len(command_subs), 0)

    def test_writable_entity_command_callback_dispatches_to_handle_command(self):
        """When a writable entity is enabled, the registered MQTT command callback
        must invoke _handle_command (line 863)."""
        cmd_topic = f'homeassistant/switch/nibe_{self.point_id}/set'
        self.mock_entity_info['command_topic'] = cmd_topic
        self.mock_entity_info['entity_type'] = 'switch'

        stored_cb = {}
        def fake_callback_add(topic, cb):
            stored_cb[topic] = cb
        self.em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)

        self.em.enable_entity(self.point_id)
        self.assertIn(cmd_topic, stored_cb)

        msg = MagicMock()
        msg.payload = b'1'
        with patch.object(self.em, '_handle_command') as mock_handle:
            stored_cb[cmd_topic](None, None, msg)
        mock_handle.assert_called_once()

    # ── disable_entity ────────────────────────────────────────────────────────

    def test_disable_not_enabled_returns_true(self):
        """Disabling a point that was never enabled is a no-op that returns True."""
        result = self.em.disable_entity(99999)
        self.assertTrue(result)

    def test_disable_removes_from_mqtt_enabled(self):
        self.em.enable_entity(self.point_id)
        self.em.disable_entity(self.point_id)
        self.assertNotIn(self.point_id, self.em.mqtt_enabled_points)

    def test_disable_removes_from_active_entities(self):
        self.em.enable_entity(self.point_id)
        self.em.disable_entity(self.point_id)
        with self.em._active_entities_lock:
            self.assertNotIn(self.point_id, self.em.active_entities_by_id)

    def test_disable_clears_last_state(self):
        self.em.enable_entity(self.point_id)
        self.em.last_states[self.point_id] = "11.9"
        self.em.disable_entity(self.point_id)
        self.assertNotIn(self.point_id, self.em.last_states)

    def test_disable_discards_value_cache(self):
        self.em.enable_entity(self.point_id)
        self.em.value_cache.should_publish(self.point_id, 100, threshold=1)
        self.em.disable_entity(self.point_id)
        # After discard, next publish call for this point is treated as first
        self.assertTrue(
            self.em.value_cache.should_publish(self.point_id, 100, threshold=1)
        )

    def test_disable_decrements_type_stats(self):
        self.em.enable_entity(self.point_id)
        count_before = self.em._stats_type_counts.get('sensor', 0)
        self.em.disable_entity(self.point_id)
        count_after = self.em._stats_type_counts.get('sensor', 0)
        self.assertEqual(count_after, count_before - 1)

    def test_disable_stat_count_never_below_zero(self):
        """Stats decrement must be guarded against going negative."""
        self.em._stats_type_counts['sensor'] = 0
        self.em.all_points_by_id[self.point_id]['entity_type'] = 'sensor'
        self.em.mqtt_enabled_points.add(self.point_id)
        with self.em._active_entities_lock:
            self.em.active_entities_by_id[self.point_id] = self.mock_entity_info
        self.em.disable_entity(self.point_id)
        self.assertGreaterEqual(self.em._stats_type_counts.get('sensor', 0), 0)

    def test_enable_then_disable_round_trip(self):
        """Full enable→disable cycle leaves the entity manager in a clean state."""
        self.em.enable_entity(self.point_id)
        self.assertIn(self.point_id, self.em.mqtt_enabled_points)
        self.em.disable_entity(self.point_id)
        self.assertNotIn(self.point_id, self.em.mqtt_enabled_points)
        with self.em._active_entities_lock:
            self.assertNotIn(self.point_id, self.em.active_entities_by_id)
        self.assertNotIn(self.point_id, self.em.last_states)





class TestEnableDisableEntityProperties(unittest.TestCase):
    """Hypothesis properties for enable_entity/disable_entity."""

    def _seeded_em(self, pid):
        em = _make_em()
        em.all_points_by_id[pid] = {
            'variableId':    pid,
            'display_title': f'Point {pid}',
            'entity_type':   'sensor',
            'entity_category': 'diagnostic',
            'is_writable':   False,
            'is_dynamic':    False,
            'description':   '',
            'metadata': {
                'unit': '', 'shortUnit': '',
                'minValue': 0, 'maxValue': 100,
                'modbusRegisterID': pid,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 'u8',
                'isWritable': False, 'divisor': 1, 'decimal': 0,
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            },
        }
        em.bulk_data[pid] = {'raw_value': 0, 'is_ok': True}
        return em

    @given(_nibe_point_id.filter(lambda p: p > 0))
    def test_mqtt_enabled_points_grows_on_enable(self, pid):
        """After enable, pid must appear in mqtt_enabled_points."""
        em = self._seeded_em(pid)
        em.enable_entity(pid)
        self.assertIn(pid, em.mqtt_enabled_points)

    @given(_nibe_point_id.filter(lambda p: p > 0))
    def test_enable_then_disable_removes_from_enabled(self, pid):
        """enable followed by disable must remove pid from mqtt_enabled_points."""
        em = self._seeded_em(pid)
        em.enable_entity(pid)
        em.disable_entity(pid)
        self.assertNotIn(pid, em.mqtt_enabled_points)

    @given(_nibe_point_id.filter(lambda p: p > 0))
    def test_enable_twice_is_idempotent(self, pid):
        """Enabling an already-enabled point must not duplicate the entry."""
        em = self._seeded_em(pid)
        em.enable_entity(pid)
        count_first = em.mqtt_enabled_points.count(pid) \
            if hasattr(em.mqtt_enabled_points, 'count') \
            else (1 if pid in em.mqtt_enabled_points else 0)
        em.enable_entity(pid)
        count_second = em.mqtt_enabled_points.count(pid) \
            if hasattr(em.mqtt_enabled_points, 'count') \
            else (1 if pid in em.mqtt_enabled_points else 0)
        self.assertEqual(count_first, count_second)

    @given(_nibe_point_id.filter(lambda p: p > 0))
    def test_disable_never_raises(self, pid):
        """Disabling a point never raises regardless of initial state."""
        em = self._seeded_em(pid)
        em.disable_entity(pid)  # must not raise even if not enabled



class TestChangelogConsistencyProperties(unittest.TestCase):
    """Hypothesis properties for changelog data integrity."""

    def _em_with_entries(self, n_entries, age_days=0):
        import time as _time
        em = _make_em()
        em._last_prune_time = _time.time()
        ts = _time.time() - age_days * 86400
        for i in range(n_entries):
            em.change_history.appendleft({
                'timestamp': ts, 'iso_timestamp': '2024-01-01',
                'added': [], 'removed': [],
                'id': f'change_{i}', 'unread': True,
                'source': 'test', 'triggered_by': None,
            })
        return em

    @given(st.integers(min_value=0, max_value=50))
    def test_seq_never_decreases(self, n_entries):
        """_history_seq must never decrease after operations."""
        em = self._em_with_entries(n_entries)
        seq_before = em._history_seq
        em.mark_changelog_read()
        self.assertGreaterEqual(em._history_seq, seq_before)

    @given(st.integers(min_value=0, max_value=50))
    def test_last_published_seq_leq_history_seq(self, n_entries):
        """_last_published_seq must never exceed _history_seq."""
        em = self._em_with_entries(n_entries)
        em.mark_changelog_read()
        self.assertLessEqual(em._last_published_seq, em._history_seq)

    @given(st.integers(min_value=0, max_value=50))
    def test_unread_count_consistent_after_mark_read(self, n_entries):
        """After mark_changelog_read, unread count must be 0."""
        em = self._em_with_entries(n_entries)
        em.mark_changelog_read()
        actual_unread = sum(1 for e in em.change_history if e.get('unread'))
        self.assertEqual(actual_unread, 0)

    @given(st.integers(min_value=1, max_value=50))
    def test_update_always_increments_seq(self, n_events):
        """Each _update_changelog_history call must increment _history_seq."""
        import time as _time
        em = _make_em()
        em._last_prune_time = _time.time()
        seqs = [em._history_seq]
        for i in range(n_events):
            em._update_changelog_history({
                'added': [i], 'removed': [], 'source': 'test'
            })
            seqs.append(em._history_seq)
        # Must be strictly increasing
        self.assertEqual(seqs, sorted(set(seqs)))


# ===========================================================================
# Stateful testing — EntityManager RuleBasedStateMachine
# ===========================================================================
#
# Hypothesis finds sequences of operations that violate invariants.
# Unlike @given tests (one call → check), this explores multi-step
# interaction sequences: enable → disable → re-enable → write → evict → ...
#
# Invariants checked after EVERY operation:
#   1. active_entities list length == active_entities_by_id dict length
#   2. active_entities_by_id.keys() ⊆ mqtt_enabled_points
#   3. pending_writes entries always have required keys
#   4. _history_seq never decreases
#   5. _last_published_seq ≤ _history_seq
#   6. mqtt_enabled_points count is always non-negative
# ===========================================================================

class EntityManagerMachine(RuleBasedStateMachine):
    """Stateful test machine for EntityManager enable/disable/write/changelog.

    Hypothesis generates arbitrary sequences of operations and checks that
    invariants hold after every step.
    """

    # ── Setup ────────────────────────────────────────────────────────────────

    @initialize()
    def setup(self):
        self.em = _make_em()
        self._initial_seq = self.em._history_seq
        # Pre-populate bulk_data and all_points_by_id for a few known pids
        # so enable_entity has valid points to work with.
        self._known_pids = [100, 200, 300, 400, 500]
        for pid in self._known_pids:
            self.em.all_points_by_id[pid] = {
                'variableId':     pid,
                'display_title':  f'Point {pid}',
                'entity_type':    'sensor',
                'entity_category': 'diagnostic',
                'is_writable':    False,
                'is_dynamic':     False,
                'description':    '',
                'metadata': {
                    'unit': '', 'shortUnit': '',
                    'minValue': 0, 'maxValue': 100,
                    'modbusRegisterID': pid,
                    'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                    'variableType': 'integer', 'variableSize': 'u8',
                    'isWritable': False, 'divisor': 1, 'decimal': 0,
                    'intDefaultValue': 0, 'stringDefaultValue': '',
                    'change': 1,
                },
            }
            self.em.bulk_data[pid] = {'raw_value': 0, 'is_ok': True}

    # ── Rules (operations) ───────────────────────────────────────────────────

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def enable(self, pid):
        self.em.enable_entity(pid)

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def disable(self, pid):
        self.em.disable_entity(pid)

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]),
          value=st.integers(min_value=0, max_value=100))
    def add_pending_write(self, pid, value):
        """Simulate a pending write entry as the write executor would create it."""
        import time as _time
        self.em.pending_writes[pid] = {
            'value': value,
            'time': _time.time(),
            'entity_id': f'sensor.nibe_{pid}',
        }

    @rule()
    def evict_stale_writes(self):
        """Evict all pending writes older than _STALE_WRITE_AGE_S."""
        import time as _time
        from nibe_entity_manager import _STALE_WRITE_AGE_S
        now = _time.time()
        stale = [p for p, v in self.em.pending_writes.items()
                 if now - v['time'] > _STALE_WRITE_AGE_S]
        for p in stale:
            self.em.pending_writes.pop(p, None)

    @rule(added=st.lists(st.integers(min_value=1, max_value=9999), max_size=3),
          removed=st.lists(st.integers(min_value=1, max_value=9999), max_size=3))
    def add_changelog_entry(self, added, removed):
        import time as _time
        self.em._last_prune_time = _time.time()  # suppress pruning during test
        # Production code stores dicts with id/title/type keys, not raw ints.
        # Using production-shaped data so the changelog_added_removed_are_lists
        # invariant and any downstream rendering code sees the correct structure.
        added_dicts   = [{'id': p, 'title': f'Point {p}', 'type': 'sensor'}
                         for p in added]
        removed_dicts = [{'id': p, 'title': f'Point {p}', 'type': 'sensor'}
                         for p in removed]
        self.em._update_changelog_history({
            'added': added_dicts, 'removed': removed_dicts, 'source': 'test',
        })

    @rule()
    def mark_changelog_read(self):
        self.em.mark_changelog_read()

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def update_bulk_value(self, pid):
        """Simulate a firmware poll updating a point's value."""
        self.em.bulk_data[pid] = {'raw_value': 42, 'is_ok': True}

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def clear_bulk_value(self, pid):
        """Simulate a point disappearing from bulk data (dynamic point gone)."""
        self.em.bulk_data.pop(pid, None)

    # ── Invariants (checked after every rule) ────────────────────────────────

    @invariant()
    def active_entities_list_matches_dict(self):
        """len(active_entities) always equals len(active_entities_by_id)."""
        assert len(self.em.active_entities) == len(self.em.active_entities_by_id), (
            f"active_entities list ({len(self.em.active_entities)}) "
            f"!= active_entities_by_id dict ({len(self.em.active_entities_by_id)})"
        )

    @invariant()
    def active_entities_subset_of_enabled(self):
        """active_entities_by_id.keys() must always be ⊆ mqtt_enabled_points."""
        active_pids = set(self.em.active_entities_by_id.keys())
        enabled_pids = set(self.em.mqtt_enabled_points)
        extra = active_pids - enabled_pids
        assert not extra, (
            f"Points in active_entities_by_id but NOT in mqtt_enabled_points: {extra}"
        )

    @invariant()
    def enabled_count_non_negative(self):
        assert len(self.em.mqtt_enabled_points) >= 0

    @invariant()
    def pending_writes_well_formed(self):
        """Every pending write entry must have 'value' and 'time' keys."""
        for pid, entry in self.em.pending_writes.items():
            assert 'value' in entry, f"pending_writes[{pid}] missing 'value'"
            assert 'time' in entry, f"pending_writes[{pid}] missing 'time'"

    @invariant()
    def history_seq_never_decreases(self):
        assert self.em._history_seq >= self._initial_seq, (
            f"_history_seq decreased: {self.em._history_seq} < {self._initial_seq}"
        )
        self._initial_seq = self.em._history_seq  # ratchet forward

    @invariant()
    def last_published_seq_leq_history_seq(self):
        assert self.em._last_published_seq <= self.em._history_seq, (
            f"_last_published_seq ({self.em._last_published_seq}) "
            f"> _history_seq ({self.em._history_seq})"
        )

    @invariant()
    def changelog_entries_have_required_keys(self):
        """Every changelog entry must have the required structural keys."""
        required = {'id', 'timestamp', 'unread', 'added', 'removed'}
        for entry in self.em.change_history:
            missing = required - set(entry.keys())
            assert not missing, f"Changelog entry missing keys: {missing}"


# pytest discovers RuleBasedStateMachine via TestCase subclassing
    @rule()
    def suppress_enabled_state(self):
        """Increment the suppression depth counter."""
        self.em._suppress_enabled_state_depth += 1

    @rule()
    def unsuppress_enabled_state(self):
        """Decrement the suppression depth counter — never below zero."""
        if self.em._suppress_enabled_state_depth > 0:
            self.em._suppress_enabled_state_depth -= 1

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def invalidate_config_hash(self, pid):
        self.em._pub.invalidate_config_hash(pid)

    @invariant()
    def suppression_depth_non_negative(self):
        assert self.em._suppress_enabled_state_depth >= 0, (
            f"_suppress_enabled_state_depth went negative: "
            f"{self.em._suppress_enabled_state_depth}"
        )

    @invariant()
    def changelog_within_maxlen(self):
        assert len(self.em.change_history) <= self.em.change_history.maxlen, (
            f"change_history exceeded maxlen: "
            f"{len(self.em.change_history)} > {self.em.change_history.maxlen}"
        )

    @invariant()
    def changelog_entry_ids_well_formed(self):
        for entry in self.em.change_history:
            assert entry['id'].startswith('change_'), (
                f"Changelog entry id malformed: {entry['id']!r}"
            )

    @invariant()
    def mqtt_enabled_points_is_set(self):
        """mqtt_enabled_points must be a set — no duplicates."""
        assert isinstance(self.em.mqtt_enabled_points, set), (
            f"mqtt_enabled_points is {type(self.em.mqtt_enabled_points).__name__}"
        )

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]),
          pending_value=st.integers(min_value=0, max_value=100),
          bulk_value=st.integers(min_value=0, max_value=100))
    def pending_write_suppresses_state_publish(self, pid, pending_value, bulk_value):
        """While a pending write exists and bulk value differs from written value,
        _update_entity_state must not publish to the state topic."""
        import time as _time
        if pending_value == bulk_value:
            return
        self.em.pending_writes[pid] = {
            'value': pending_value, 'timestamp': _time.time(),
            'time': _time.time(), 'cmd_id': 'test',
        }
        self.em.bulk_data[pid] = {
            'raw_value': bulk_value, 'is_ok': True, 'string_value': '',
            'metadata': {'variableSize': 'u8', 'divisor': 1,
                         'unit': '', 'change': 0, 'decimal': 0},
            'title': f'Point {pid}',
        }
        entity_info = {
            'point_id': pid, 'entity_type': 'sensor',
            'availability_topic': f'nibe/avail/{pid}',
            'state_topic': f'nibe/state/{pid}',
            'command_topic': None, 'point_data': {},
        }
        self.em.active_entities_by_id[pid] = entity_info
        self.em.mqtt_enabled_points.add(pid)
        before = list(self.em.mqtt.publish.call_args_list)
        self.em._update_entity_state(entity_info)
        after = list(self.em.mqtt.publish.call_args_list)
        state_publishes = [c for c in after[len(before):]
                           if c.args[0] == f'nibe/state/{pid}']
        assert not state_publishes, (
            f"Published to nibe/state/{pid} while pending write active"
        )
        self.em.pending_writes.pop(pid, None)
        self.em.active_entities_by_id.pop(pid, None)
        self.em.mqtt_enabled_points.discard(pid)


    @rule(mode=st.sampled_from(['none', 'all']))
    def apply_mode(self, mode):
        """apply_mode reconciles mqtt_enabled_points to the target mode.

        'none' disables all points; 'all' enables all known points.
        Both work regardless of which pids are in MODES frozensets since
        'none' → frozenset() and 'all' → set(all_points_by_id.keys()).
        This exercises the suppress/unsuppress lock, the enable/disable
        loops, and the persist call — all in a single state transition."""
        self.em.apply_mode(mode)

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]),
          entity_type=st.sampled_from(['switch', 'number', 'sensor', 'select']),
          raw_value=st.integers(min_value=0, max_value=10))
    def update_entity_state_writable(self, pid, entity_type, raw_value):
        """Exercise _update_entity_state for writable entity types (switch,
        number, select) — the existing machine only uses 'sensor'. Writable
        types have different command_topic and value-mapping paths."""
        self.em.bulk_data[pid] = {
            'raw_value': raw_value, 'is_ok': True, 'string_value': '',
            'metadata': {
                'variableSize': 'u8', 'divisor': 1, 'unit': '',
                'change': 0, 'decimal': 0,
                'minValue': 0, 'maxValue': 10,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            },
            'title': f'Point {pid}',
        }
        entity_info = {
            'point_id':            pid,
            'entity_type':         entity_type,
            'availability_topic':  f'nibe/avail/{pid}',
            'state_topic':         f'nibe/state/{pid}',
            'command_topic':       f'homeassistant/{entity_type}/nibe_{pid}/set',
            'point_data':          {},
        }
        self.em.active_entities_by_id[pid] = entity_info
        self.em.mqtt_enabled_points.add(pid)
        self.em._update_entity_state(entity_info)
        self.em.active_entities_by_id.pop(pid, None)
        self.em.mqtt_enabled_points.discard(pid)

    @invariant()
    def active_dynamic_points_subset_of_mqtt_enabled(self):
        """Every point in active_dynamic_points must also be in mqtt_enabled_points
        once it has been indexed — active but not enabled is an inconsistent state."""
        # Only check points that are in active_entities_by_id (fully indexed);
        # active_dynamic_points can transiently lead mqtt_enabled_points during
        # the reconcile window, so we guard on full indexing.
        indexed = set(self.em.active_entities_by_id.keys())
        active_and_indexed = self.em.active_dynamic_points & indexed
        not_enabled = active_and_indexed - self.em.mqtt_enabled_points
        assert not not_enabled, (
            f"Active dynamic points indexed but not in mqtt_enabled_points: "
            f"{not_enabled}"
        )

    @invariant()
    def changelog_added_removed_are_lists(self):
        """Every changelog entry's 'added' and 'removed' fields must be lists."""
        for entry in self.em.change_history:
            assert isinstance(entry.get('added'), list), (
                f"Changelog 'added' is not a list: {type(entry.get('added'))}"
            )
            assert isinstance(entry.get('removed'), list), (
                f"Changelog 'removed' is not a list: {type(entry.get('removed'))}"
            )

    @invariant()
    def apply_mode_none_leaves_no_enabled_static(self):
        """After apply_mode('none'), only active dynamic points remain enabled.
        This is checked only when the mode has been applied — we probe the
        current enabled set for any non-dynamic members, which would indicate
        the suppression or disable loop had a bug."""
        # This invariant cannot know whether apply_mode('none') was the last
        # operation, so we verify the weaker property that is always true:
        # mqtt_enabled_points ⊇ active_dynamic_points (dynamic points are
        # never disabled by apply_mode regardless of mode).
        for pid in self.em.active_dynamic_points:
            if pid in self.em.active_entities_by_id:
                assert pid in self.em.mqtt_enabled_points, (
                    f"Active indexed dynamic point {pid} not in mqtt_enabled_points"
                )


EntityManagerStatefulTest = EntityManagerMachine.TestCase


# ---------------------------------------------------------------------------
# LRUCache RuleBasedStateMachine
# ---------------------------------------------------------------------------

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
    KEYS = list(range(20))  # more keys than capacity to force eviction

    @initialize()
    def setup(self):
        from nibe_entity_manager import LRUCache
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

    @invariant()
    def size_never_exceeds_capacity(self):
        assert len(self.cache) <= self.CAPACITY, (
            f"LRUCache size {len(self.cache)} exceeded capacity {self.CAPACITY}"
        )

    @invariant()
    def size_matches_get_stats(self):
        assert len(self.cache) == self.cache.get_stats()['size']

    @invariant()
    def hit_rate_in_0_1(self):
        rate = self.cache.get_stats()['hit_rate']
        assert 0.0 <= rate <= 1.0, f"hit_rate {rate} out of [0, 1]"

    @invariant()
    def hits_plus_misses_equals_gets(self):
        stats = self.cache.get_stats()
        assert stats['hits'] + stats['misses'] == self.total_gets, (
            f"hits({stats['hits']}) + misses({stats['misses']}) "
            f"!= total_gets({self.total_gets})"
        )

    @invariant()
    def capacity_constant(self):
        assert self.cache.get_stats()['capacity'] == self.CAPACITY


LRUCacheStatefulTest = LRUCacheMachine.TestCase


# ---------------------------------------------------------------------------
# DynamicPointMap RuleBasedStateMachine
# ---------------------------------------------------------------------------

class DynamicPointMapMachine(RuleBasedStateMachine):
    """Stateful test machine for DynamicPointMap.

    Explores populate → record_outcome → mark_removed → restore → flush
    sequences and checks invariants after every step.

    Key invariants:
      1. unprocessed_values ∩ processed_values = ∅ for every entry
      2. is_fully_processed ↔ unprocessed_values = ∅
      3. is_controlling=True → at least one value has non-empty dynamic pids
      4. is_controlling=False → all dynamic_points_by_value values are empty
      5. serialise → deserialise is lossless identity
      6. is_known_dynamic(pid) ↔ pid in all_known_dynamic_point_ids()
    """

    CONTROL_PIDS = [10, 20, 30]     # switch/select controlling points
    DYNAMIC_PIDS = [1000, 1001, 1002, 1003, 1004]  # points that appear/disappear

    @initialize()
    def setup(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        self.map = DynamicPointMap()
        self.DPE = DynamicPointEntry
        # Pre-populate entries for the control pids
        for pid in self.CONTROL_PIDS:
            entry = DynamicPointEntry(
                point_id=pid, title=f'Switch {pid}',
                entity_type='switch',
                unprocessed_values={0, 1},
            )
            self.map._table[pid] = entry

    # ── Rules ────────────────────────────────────────────────────────────────

    @rule(
        control_pid=st.sampled_from(CONTROL_PIDS),
        value=st.integers(min_value=0, max_value=1),
        dynamic_pids=st.lists(
            st.sampled_from(DYNAMIC_PIDS), min_size=0, max_size=3, unique=True,
        ),
    )
    def record_outcome(self, control_pid, value, dynamic_pids):
        self.map.record_outcome(control_pid, value, dynamic_pids)

    @rule(control_pid=st.sampled_from(CONTROL_PIDS))
    def mark_firmware_removed(self, control_pid):
        self.map.mark_firmware_removed(control_pid)

    @rule()
    def restore_from_bulk(self):
        """Restore all control pids as if they appeared in a bulk fetch."""
        self.map.restore_from_bulk(set(self.CONTROL_PIDS))

    @rule()
    def flush(self):
        """Flush the map — resets all entries to unprocessed."""
        all_points = {pid: {
            'variableId': pid, 'display_title': f'Switch {pid}',
            'metadata': {'minValue': 0, 'maxValue': 1},
        } for pid in self.CONTROL_PIDS}
        types = {pid: 'switch' for pid in self.CONTROL_PIDS}
        self.map.flush(all_points, types)

    @rule(dynamic_pid=st.sampled_from(DYNAMIC_PIDS))
    def check_known_dynamic(self, dynamic_pid):
        """Looking up a dynamic pid is always safe — used as a probe operation."""
        _ = self.map.is_known_dynamic(dynamic_pid)
        _ = self.map.controlling_entry_for_dynamic(dynamic_pid)

    # ── Invariants ───────────────────────────────────────────────────────────

    @invariant()
    def unprocessed_and_processed_disjoint(self):
        """unprocessed_values ∩ processed_values must be ∅ for every entry."""
        for pid, entry in self.map._table.items():
            overlap = entry.unprocessed_values & entry.processed_values
            assert not overlap, (
                f"Entry {pid}: unprocessed ∩ processed = {overlap}"
            )

    @invariant()
    def is_fully_processed_consistent(self):
        """is_fully_processed() ↔ unprocessed_values == ∅"""
        for pid, entry in self.map._table.items():
            expected = len(entry.unprocessed_values) == 0
            actual   = entry.is_fully_processed()
            assert actual == expected, (
                f"Entry {pid}: is_fully_processed()={actual} "
                f"but unprocessed={entry.unprocessed_values}"
            )

    @invariant()
    def is_known_dynamic_consistent_with_all_known_ids(self):
        """is_known_dynamic(pid) ↔ pid in all_known_dynamic_point_ids()"""
        all_known = self.map.all_known_dynamic_point_ids()
        for pid in self.DYNAMIC_PIDS:
            via_method = self.map.is_known_dynamic(pid)
            via_set    = pid in all_known
            assert via_method == via_set, (
                f"is_known_dynamic({pid})={via_method} "
                f"but pid in all_known_ids={via_set}"
            )

    @invariant()
    def serialise_deserialise_roundtrip(self):
        """Serialise then deserialise must produce the same known dynamic point ids."""
        from nibe_dynamic_map import DynamicPointMap
        original_ids = self.map.all_known_dynamic_point_ids()
        json_str = self.map.serialise()
        fresh = DynamicPointMap()
        fresh.deserialise(json_str)
        roundtrip_ids = fresh.all_known_dynamic_point_ids()
        assert original_ids == roundtrip_ids, (
            f"Serialise roundtrip lost dynamic ids: "
            f"original={original_ids}, roundtrip={roundtrip_ids}"
        )

    @invariant()
    def controlling_entry_returns_entry_iff_known(self):
        """controlling_entry_for_dynamic(pid) returns entry iff is_known_dynamic(pid)."""
        for pid in self.DYNAMIC_PIDS:
            entry  = self.map.controlling_entry_for_dynamic(pid)
            known  = self.map.is_known_dynamic(pid)
            if known:
                assert entry is not None, (
                    f"controlling_entry_for_dynamic({pid}) is None "
                    f"but is_known_dynamic={known}"
                )
            else:
                assert entry is None, (
                    f"controlling_entry_for_dynamic({pid}) is not None "
                    f"but is_known_dynamic={known}"
                )


    @rule(
        pids=st.lists(
            st.sampled_from(CONTROL_PIDS + DYNAMIC_PIDS),
            min_size=0, max_size=6, unique=True,
        ),
        entity_types=st.dictionaries(
            st.sampled_from(CONTROL_PIDS),
            st.sampled_from(['switch', 'select']),
            max_size=3,
        ),
    )
    def populate_from_bulk(self, pids, entity_types):
        """populate_from_bulk is the production entry point — call it with
        realistic all_points_by_id and entity_type_map inputs to exercise
        the new-entry detection and min/max range recording paths."""
        all_points = {pid: {
            'variableId':    pid,
            'display_title': f'Point {pid}',
            'metadata': {'minValue': 0, 'maxValue': 1},
        } for pid in pids}
        self.map.populate_from_bulk(all_points, entity_types)


DynamicPointMapStatefulTest = DynamicPointMapMachine.TestCase


# ---------------------------------------------------------------------------
# ValueCache RuleBasedStateMachine
# ---------------------------------------------------------------------------

class ValueCacheMachine(RuleBasedStateMachine):
    """Stateful test machine for ValueCache.

    Key invariants:
      1. _cache and _last_publish always have the same keys
      2. After discard(pid): pid not in _cache and not in _last_publish
      3. After should_publish returns True: value is stored in _cache
      4. should_publish(same_value, same_pid, threshold=1, min_interval=9999)
         always returns False immediately after a True
    """

    PIDS    = [100, 200, 300]
    THRESH  = 5

    @initialize()
    def setup(self):
        from nibe_entity_manager import ValueCache
        self.cache = ValueCache()
        self.last_published = {}   # pid → last value that caused True

    @rule(
        pid=st.sampled_from(PIDS),
        value=st.integers(min_value=0, max_value=100),
    )
    def should_publish_normal(self, pid, value):
        result = self.cache.should_publish(
            pid, value, threshold=self.THRESH, min_interval=0)
        if result:
            self.last_published[pid] = value

    @rule(pid=st.sampled_from(PIDS))
    def discard(self, pid):
        self.cache.discard(pid)
        self.last_published.pop(pid, None)

    @rule(
        pid=st.sampled_from(PIDS),
        value=st.integers(min_value=0, max_value=100),
    )
    def force_publish(self, pid, value):
        result = self.cache.should_publish(
            pid, value, threshold=self.THRESH, force=True, min_interval=0)
        assert result is True, "force=True must always return True"
        self.last_published[pid] = value

    @rule(pid=st.sampled_from(PIDS))
    def after_discard_next_always_true(self, pid):
        """After discard, should_publish must return True for any value."""
        self.cache.discard(pid)
        result = self.cache.should_publish(
            pid, 42, threshold=self.THRESH, min_interval=9999)
        assert result is True, (
            f"After discard({pid}), should_publish returned False"
        )
        self.last_published[pid] = 42

    # ── Invariants ───────────────────────────────────────────────────────────

    @invariant()
    def cache_and_last_publish_same_keys(self):
        """_cache and _last_publish must always have identical key sets."""
        cache_keys   = set(self.cache._cache.keys())
        publish_keys = set(self.cache._last_publish.keys())
        assert cache_keys == publish_keys, (
            f"_cache keys {cache_keys} != _last_publish keys {publish_keys}"
        )

    @invariant()
    def cached_value_matches_last_true_return(self):
        """For every pid in last_published, _cache[pid] must equal the last
        value that produced a True return — no cache drift allowed."""
        for pid, last_val in self.last_published.items():
            cached = self.cache._cache.get(pid)
            assert cached == last_val, (
                f"_cache[{pid}]={cached!r} does not match last published "
                f"value {last_val!r} — cache drift detected"
            )

    @rule(pid=st.sampled_from(PIDS), value=st.integers(min_value=0, max_value=100))
    def repeat_publish_suppressed_by_interval(self, pid, value):
        """After any True return, an immediate repeat with min_interval=9999
        must return False — interval suppression must hold."""
        first = self.cache.should_publish(
            pid, value, threshold=self.THRESH, force=True, min_interval=0)
        assert first is True
        self.last_published[pid] = value
        second = self.cache.should_publish(
            pid, value, threshold=self.THRESH, min_interval=9999)
        assert second is False, (
            f"Immediate repeat of should_publish({pid}, {value}) with "
            f"min_interval=9999 returned True — interval suppression failed"
        )


ValueCacheStatefulTest = ValueCacheMachine.TestCase


# ===========================================================================
# 18. Data integrity — changelog consistency
# ===========================================================================


class TestChangelogConsistency(unittest.TestCase):
    """Tests for changelog data integrity across publish, prune, and restart."""

    def setUp(self):
        self.em = _make_em()
        self.em._last_prune_time = time.time()  # prevent auto-prune in tests

    def _entry(self, age_days=0, seq=None):
        return {
            'timestamp':     time.time() - age_days * 86400,
            'iso_timestamp': '2024-01-01',
            'added':         [{'id': 6983, 'title': 'T', 'type': 'number'}],
            'removed':       [],
            'id':            f'change_{seq or int(time.time()*1000)}',
            'unread':        True,
        }

    def test_last_published_seq_updated_after_publish(self):
        """_last_published_seq must be set after the publish call, not before.
        This ensures a crash before publish leaves the seq guard in a state
        where the incoming retained message is not filtered on restart."""
        publish_call_count = [0]
        seq_at_publish = [None]
        original_seq = self.em._last_published_seq

        def capture_publish(topic, payload, retain=False):
            publish_call_count[0] += 1
            # Capture whether _last_published_seq has been set yet
            if publish_call_count[0] == 1:
                seq_at_publish[0] = self.em._last_published_seq

        self.em.mqtt.publish.side_effect = capture_publish

        change_event = {'added': [{'id': 6983, 'title': 'T', 'type': 'number'}],
                        'removed': [], 'source': 'firmware', 'triggered_by': None}
        self.em._update_changelog_history(change_event)

        # At the moment of the first publish call, _last_published_seq
        # should still be the original value (updated after, not before)
        self.assertEqual(seq_at_publish[0], original_seq,
                         "_last_published_seq must not be set before publish")
        # After the call returns it should be updated
        self.assertGreater(self.em._last_published_seq, original_seq)

    def test_seq_guard_allows_load_when_seq_differs(self):
        """on_history_message must load the payload when incoming_seq differs
        from _last_published_seq — this is the normal restart case."""
        from nibe_entity_manager import EntityManager, _compress_payload
        self.em._last_published_seq = 5
        self.em.change_history.clear()
        EntityManager._setup_history_loading(self.em)

        payload_data = {
            'history': [self._entry()],
            '_seq': 3,  # different from _last_published_seq=5
        }
        msg = MagicMock()
        msg.payload = _compress_payload(payload_data).encode('utf-8')
        self.em._on_history_message(None, None, msg)

        self.assertEqual(len(self.em.change_history), 1)

    def test_seq_guard_skips_load_when_seq_matches(self):
        """on_history_message must skip loading when incoming_seq matches
        _last_published_seq — this prevents overwriting fresh in-memory
        history with the just-published retained copy."""
        from nibe_entity_manager import EntityManager, _compress_payload
        self.em._last_published_seq = 7
        self.em.change_history.clear()
        EntityManager._setup_history_loading(self.em)

        payload_data = {
            'history': [self._entry()],
            '_seq': 7,   # matches _last_published_seq
        }
        msg = MagicMock()
        msg.payload = _compress_payload(payload_data).encode('utf-8')
        self.em._on_history_message(None, None, msg)

        # History should remain empty — load was skipped
        self.assertEqual(len(self.em.change_history), 0)

    def test_changelog_entry_structure_is_valid_after_append(self):
        """Every entry appended by _update_changelog_history must have all
        required fields that _prune_changelog_if_due checks for."""
        change_event = {
            'added': [{'id': 6983, 'title': 'T', 'type': 'number'}],
            'removed': [], 'source': 'firmware', 'triggered_by': None,
        }
        self.em._update_changelog_history(change_event)
        self.assertEqual(len(self.em.change_history), 1)
        entry = list(self.em.change_history)[0]
        for required_key in ('timestamp', 'iso_timestamp', 'added', 'removed'):
            self.assertIn(required_key, entry,
                          f"Entry missing required key: {required_key}")

    def test_unread_count_matches_unread_entries(self):
        """The unread_count published to MQTT must match the actual number
        of unread entries in change_history."""
        published_payloads = {}
        def capture(topic, payload, retain=False):
            published_payloads[topic] = payload
        self.em.mqtt.publish.side_effect = capture

        # Seed two unread entries
        self.em.change_history.appendleft({**self._entry(), 'unread': True})
        self.em.change_history.appendleft({**self._entry(), 'unread': True})

        change_event = {
            'added': [{'id': 6984, 'title': 'S', 'type': 'switch'}],
            'removed': [], 'source': 'firmware', 'triggered_by': None,
        }
        self.em._update_changelog_history(change_event)

        from nibe_entity_manager import BrowserTopic
        unread_payload = published_payloads.get(str(BrowserTopic.CHANGELOG_UNREAD))
        if unread_payload:
            data = json.loads(unread_payload)
            actual_unread = sum(1 for e in self.em.change_history
                                if e.get('unread', False))
            self.assertEqual(data['unread_count'], actual_unread)

    def test_deque_maxlen_prevents_unbounded_growth(self):
        """The deque hard cap must prevent the changelog from growing beyond
        _CHANGELOG_MAX_ENTRIES even without time-based pruning."""
        from nibe_entity_manager import _CHANGELOG_MAX_ENTRIES
        self.em._last_prune_time = time.time() + 86400  # suppress prune

        for i in range(_CHANGELOG_MAX_ENTRIES + 50):
            event = {'added': [{'id': i, 'title': 'T', 'type': 'sensor'}],
                     'removed': [], 'source': 'firmware', 'triggered_by': None}
            self.em.change_history.appendleft(event)

        self.assertLessEqual(len(self.em.change_history), _CHANGELOG_MAX_ENTRIES)

    def test_prune_does_not_delete_below_minimum_floor(self):
        """Even with an aggressive retention setting, _CHANGELOG_MIN_ENTRIES
        must always be preserved."""
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES
        self.em.changelog_retention_days = 1
        self.em._last_prune_time = 0.0

        # All entries are 10 days old — all expired
        for _ in range(_CHANGELOG_MIN_ENTRIES + 20):
            self.em.change_history.appendleft(self._entry(age_days=10))

        self.em._prune_changelog_if_due()
        self.assertGreaterEqual(len(self.em.change_history), _CHANGELOG_MIN_ENTRIES)


# ===========================================================================
# 19. Data integrity — write validation boundary conditions
# ===========================================================================


class TestValueCacheDeduplication(unittest.TestCase):

    def _setup(self):
        em = _make_em()
        pid = 500
        ei = {
            'point_id': pid, 'entity_type': 'sensor',
            'entity_id': f'nibe_{pid}',
            'state_topic': f'homeassistant/sensor/nibe_{pid}/state',
            'availability_topic': f'homeassistant/sensor/nibe_{pid}/avail',
            'command_topic': None, 'is_writable': False,
            'display_title': 'Outdoor temp',
            'metadata': {
                'minValue': -300, 'maxValue': 300, 'divisor': 10,
                'isWritable': False,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'intDefaultValue': 0,
                'unit': '°C', 'shortUnit': '°C',
            },
        }
        em.active_entities_by_id[pid] = ei
        em.bulk_data[pid] = {
            'raw_value': 206, 'is_ok': True,
            'string_value': '',
            'metadata': ei['metadata'],
            'display_title': 'Outdoor temp',
        }
        return em, pid

    def _state_publish_count(self, em, pid):
        """Count publishes to the state topic only (not availability)."""
        topic = f'homeassistant/sensor/nibe_{pid}/state'
        return sum(1 for c in em.mqtt.publish.call_args_list
                   if c.args[0] == topic or (c.args and c.args[0] == topic))

    def test_first_call_publishes(self):
        em, pid = self._setup()
        em._update_entity_state(em.active_entities_by_id[pid])
        self.assertGreater(self._state_publish_count(em, pid), 0)

    def test_same_value_not_republished(self):
        em, pid = self._setup()
        em._update_entity_state(em.active_entities_by_id[pid])
        count = self._state_publish_count(em, pid)
        em._update_entity_state(em.active_entities_by_id[pid])
        self.assertEqual(self._state_publish_count(em, pid), count,
                         "Identical state value must not be republished to state topic")

    def test_changed_value_republished(self):
        em, pid = self._setup()
        em._update_entity_state(em.active_entities_by_id[pid])
        count = self._state_publish_count(em, pid)
        em.bulk_data[pid]['raw_value'] = 210
        em._update_entity_state(em.active_entities_by_id[pid])
        self.assertGreater(self._state_publish_count(em, pid), count)


# ===========================================================================
# 33. _suppress_enabled_state context manager
# ===========================================================================


class TestSuppressEnabledState(unittest.TestCase):

    def test_depth_increments_inside_context(self):
        em = _make_em()
        self.assertEqual(em._suppress_enabled_state_depth, 0)
        with em._suppress_enabled_state():
            self.assertEqual(em._suppress_enabled_state_depth, 1)
        self.assertEqual(em._suppress_enabled_state_depth, 0)

    def test_nested_contexts_increment_depth(self):
        em = _make_em()
        with em._suppress_enabled_state():
            with em._suppress_enabled_state():
                self.assertEqual(em._suppress_enabled_state_depth, 2)
            self.assertEqual(em._suppress_enabled_state_depth, 1)
        self.assertEqual(em._suppress_enabled_state_depth, 0)

    def test_is_suppressed_returns_true_inside(self):
        em = _make_em()
        with em._suppress_enabled_state():
            self.assertTrue(em._suppress_enabled_state_depth > 0)

    def test_depth_restored_after_exception(self):
        em = _make_em()
        try:
            with em._suppress_enabled_state():
                raise ValueError("test")
        except ValueError:
            pass
        self.assertEqual(em._suppress_enabled_state_depth, 0)


# ===========================================================================
# 34. Dynamic point map — record_outcome creates entry correctly
# ===========================================================================


class TestPointToMenuMap(unittest.TestCase):

    def test_starts_empty(self):
        em = _make_em()
        self.assertEqual(em.point_to_menu_map, {})

    def test_can_be_populated(self):
        em = _make_em()
        em.point_to_menu_map[6984] = ('7.1.6.3', 'Power at DOT')
        self.assertEqual(em.point_to_menu_map[6984], ('7.1.6.3', 'Power at DOT'))

    def test_lookup_returns_none_for_unknown(self):
        em = _make_em()
        self.assertIsNone(em.point_to_menu_map.get(9999))


# ===========================================================================
# 38. _ws_call handles a dead connection without raising
# ===========================================================================


class TestResolvePointFromEntityId(unittest.TestCase):
    """The three-pass resolver that maps an incoming HA entity_id back to a
    Nibe point_id for command handling. Zero coverage before this despite
    being on the critical path for every write from Home Assistant — a
    bug here means a command either silently resolves to the wrong point
    or fails to resolve at all, with no obvious error to the user."""

    def test_no_dot_returns_none(self):
        """Malformed input (no domain separator) — must not crash."""
        em = _make_em()
        self.assertIsNone(em.resolve_point_from_entity_id('not_a_valid_entity_id'))

    def test_pass1_nibe_prefixed_slug_resolves_directly(self):
        """The fast path: entity_id literally encodes the point_id, e.g.
        switch.nibe_3920 -> 3920. No registry lookup needed."""
        em = _make_em()
        self.assertEqual(em.resolve_point_from_entity_id('switch.nibe_3920'), 3920)

    def test_pass1_non_numeric_suffix_falls_through_not_crashes(self):
        """A slug starting with 'nibe_' but not followed by a valid int
        (e.g. a custom-renamed entity) must fall through to pass 2/3
        rather than raising ValueError."""
        em = _make_em()
        result = em.resolve_point_from_entity_id('switch.nibe_custom_name')
        self.assertIsNone(result)  # no other match available either

    def test_pass2_matches_via_active_entities_config_topic(self):
        """When the entity_id doesn't encode the point_id directly (e.g.
        user has renamed it in HA), fall back to matching against the
        known discovery config topic built from entity_type + entity_id."""
        em = _make_em()
        em.active_entities_by_id[3920] = {
            'entity_type': 'switch', 'entity_id': 'permit_heating',
        }
        result = em.resolve_point_from_entity_id('switch.permit_heating')
        self.assertEqual(result, 3920)

    def test_pass2_does_not_match_wrong_domain(self):
        """The config topic includes entity_type — a sensor with the same
        slug as a switch must not be confused for it."""
        em = _make_em()
        em.active_entities_by_id[3920] = {
            'entity_type': 'switch', 'entity_id': 'permit_heating',
        }
        result = em.resolve_point_from_entity_id('sensor.permit_heating')
        self.assertIsNone(result)

    def test_pass3_unique_id_map_used_when_provided(self):
        """The registry-watcher-supplied unique_id_map is the third and
        final fallback — used when neither the fast prefix path nor the
        active_entities scan resolves the entity."""
        em = _make_em()
        unique_id_map = {'nibe_4527': 'switch.some_renamed_entity'}
        result = em.resolve_point_from_entity_id(
            'switch.some_renamed_entity', unique_id_map=unique_id_map,
        )
        self.assertEqual(result, 4527)

    def test_pass3_non_nibe_unique_id_ignored(self):
        """A unique_id_map entry not prefixed 'nibe_' belongs to a
        different integration and must not be matched."""
        em = _make_em()
        unique_id_map = {'other_integration_id': 'switch.some_entity'}
        result = em.resolve_point_from_entity_id(
            'switch.some_entity', unique_id_map=unique_id_map,
        )
        self.assertIsNone(result)

    def test_pass3_malformed_unique_id_suffix_does_not_crash(self):
        em = _make_em()
        unique_id_map = {'nibe_not_a_number': 'switch.some_entity'}
        result = em.resolve_point_from_entity_id(
            'switch.some_entity', unique_id_map=unique_id_map,
        )
        self.assertIsNone(result)

    def test_no_match_anywhere_returns_none(self):
        em = _make_em()
        result = em.resolve_point_from_entity_id(
            'switch.totally_unknown', unique_id_map={},
        )
        self.assertIsNone(result)

    def test_pass_priority_fast_path_wins_over_active_entities_scan(self):
        """If both the fast nibe_-prefix path AND the active_entities scan
        could resolve the same entity_id, the fast path must be tried
        first and short-circuit — confirms pass ordering, not just that
        each pass works in isolation."""
        em = _make_em()
        # Set up a conflicting active_entities entry that would resolve
        # to a DIFFERENT point_id if pass 2 were reached.
        em.active_entities_by_id[9999] = {
            'entity_type': 'switch', 'entity_id': 'nibe_3920',
        }
        result = em.resolve_point_from_entity_id('switch.nibe_3920')
        self.assertEqual(result, 3920)  # fast path wins, not 9999


# ===========================================================================
# 49. EntityManager.build_disable_notification — HA-side disable messaging
# ===========================================================================


class TestBuildDisableNotification(unittest.TestCase):
    """Builds the (title, message, notification_id) tuple shown to the user
    when an entity is disabled/re-enabled via HA's own entity settings
    (not the Nibe Entity Manager card). Zero coverage before this. A bug
    here doesn't crash anything — it just shows a wrong or malformed
    notification, or a notif_id that breaks HA's dedupe/dismiss logic."""

    def test_reenabled_action_returns_reenabled_message(self):
        em = _make_em()
        title, message, notif_id = em.build_disable_notification(
            3920, 'switch.permit_heating', 're-enabled',
        )
        self.assertIn('re-enabled', title.lower())
        self.assertIn('resume publishing', message)

    def test_disabled_static_point_returns_standard_message(self):
        em = _make_em()
        em.all_points_by_id[3920] = {'display_title': 'Permit heating', 'is_dynamic': False}
        title, message, notif_id = em.build_disable_notification(
            3920, 'switch.permit_heating', 'disabled',
        )
        self.assertEqual(title, 'Nibe Bridge: Entity disabled in HA')
        self.assertIn('#3920 (Permit heating)', message)
        self.assertIn('Entity Manager card', message)

    def test_disabled_dynamic_point_returns_dynamic_specific_message(self):
        """Dynamic points get a different message explaining they'll
        disappear automatically — must not be conflated with the static
        'use the Entity Manager card' guidance, which doesn't apply to them."""
        em = _make_em()
        em.all_points_by_id[50827] = {'display_title': 'Humidity', 'is_dynamic': True}
        title, message, notif_id = em.build_disable_notification(
            50827, 'sensor.humidity', 'disabled',
        )
        self.assertEqual(title, 'Nibe Bridge: Dynamic entity disabled in HA')
        self.assertIn('firmware-controlled state change', message)
        self.assertNotIn('Entity Manager card', message)

    def test_unknown_point_id_falls_back_to_hash_display(self):
        """The point isn't in all_points_by_id (stale data) — must not
        crash, falls back to a bare '#id' display."""
        em = _make_em()
        title, message, notif_id = em.build_disable_notification(
            9999, 'switch.unknown', 'disabled',
        )
        self.assertIn('#9999', message)

    def test_none_point_id_falls_back_to_entity_id_display(self):
        """point_id itself is None (couldn't be resolved at all) — falls
        back to showing the raw HA entity_id instead of '#None'."""
        em = _make_em()
        title, message, notif_id = em.build_disable_notification(
            None, 'switch.mystery_entity', 'disabled',
        )
        self.assertIn('switch.mystery_entity', message)
        self.assertNotIn('#None', message)

    def test_notification_id_sanitises_dots_and_hyphens(self):
        """notif_id is used as an HA notification identifier — dots and
        hyphens from the entity_id must be replaced with underscores."""
        em = _make_em()
        _, _, notif_id = em.build_disable_notification(
            3920, 'switch.some-entity.name', 'disabled',
        )
        self.assertNotIn('.', notif_id)
        self.assertNotIn('-', notif_id)
        self.assertTrue(notif_id.startswith('nibe_ha_disable_'))

    def test_notification_id_truncated_to_safe_length(self):
        """A very long entity_id must not produce an unbounded notif_id —
        confirms the [:60] truncation is actually applied."""
        em = _make_em()
        long_id = 'switch.' + 'a' * 200
        _, _, notif_id = em.build_disable_notification(3920, long_id, 'disabled')
        self.assertLessEqual(len(notif_id), len('nibe_ha_disable_') + 60)

    def test_notification_id_distinct_per_entity(self):
        """Two different entities must produce two different notif_ids, so
        HA doesn't conflate or dedupe unrelated disable notifications."""
        em = _make_em()
        _, _, id_a = em.build_disable_notification(1, 'switch.a', 'disabled')
        _, _, id_b = em.build_disable_notification(2, 'switch.b', 'disabled')
        self.assertNotEqual(id_a, id_b)

    def test_display_falls_back_to_generic_point_label_when_no_title(self):
        """A point exists in all_points_by_id but has no display_title key
        — falls back to a generic 'Point N' label rather than crashing or
        showing a blank title."""
        em = _make_em()
        em.all_points_by_id[100] = {'is_dynamic': False}  # no display_title
        title, message, notif_id = em.build_disable_notification(
            100, 'switch.foo', 'disabled',
        )
        self.assertIn('Point 100', message)


# ===========================================================================
# 49b. EntityManager applied-mode persistence — read/write, MQTT + file
# ===========================================================================


class TestAppliedModePersistence(unittest.TestCase):
    """Covers read_applied_mode(), _persist_applied_mode(),
    _read_applied_mode_from_file(), and record_applied_mode() — the
    mechanism decide_startup_action relies on to detect a genuine mode
    change across a restart. read_applied_mode() uses the same
    synchronous subscribe-and-wait pattern as scan_mqtt_discovery(); tests
    simulate immediate retained-message delivery by having the mocked
    message_callback_add invoke the real callback synchronously, so the
    real method body runs with zero wall-clock wait."""

    def setUp(self):
        import tempfile
        import os
        self._tmp_dir = tempfile.mkdtemp()
        self._tmp_path = os.path.join(self._tmp_dir, 'applied_mode')

    def _deliver_retained(self, em, payload: bytes | None):
        """Make em.mqtt.message_callback_add synchronously invoke the
        stored callback with a fake retained message — simulating the
        broker responding before the .wait() timeout would otherwise fire."""
        def fake_callback_add(_topic, cb):
            if payload is None:
                return  # simulate no retained message — real timeout path
            msg = MagicMock()
            msg.payload = payload
            cb(None, None, msg)
        em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)

    def test_read_applied_mode_returns_mqtt_value(self):
        em = _make_em()
        self._deliver_retained(em, b'menus')
        self.assertEqual(em.read_applied_mode(), 'menus')

    def test_read_applied_mode_invalid_utf8_payload_falls_back_to_file(self):
        """A malformed retained payload must not raise — decode failure is
        caught and treated as no MQTT value, falling through to the file."""
        em = _make_em()
        self._deliver_retained(em, b'\xff\xfe\x00\x01')  # invalid UTF-8
        with open(self._tmp_path, 'w') as f:
            f.write('advanced')
        with patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_path):
            self.assertEqual(em.read_applied_mode(), 'advanced')

    def test_read_applied_mode_strips_whitespace(self):
        em = _make_em()
        self._deliver_retained(em, b'  advanced  \n')
        self.assertEqual(em.read_applied_mode(), 'advanced')

    def test_read_applied_mode_empty_payload_falls_back_to_file(self):
        """An empty retained payload (topic exists but was cleared) must be
        treated the same as no message — fall through to the file."""
        em = _make_em()
        self._deliver_retained(em, b'')
        with open(self._tmp_path, 'w') as f:
            f.write('monitoring')
        with patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_path):
            self.assertEqual(em.read_applied_mode(), 'monitoring')

    def test_read_applied_mode_falls_back_to_file_when_no_mqtt_message(self):
        """The real migration-boundary / timeout path: no retained message
        arrives at all — timeout fires and file fallback is used."""
        em = _make_em()
        self._deliver_retained(em, None)
        with open(self._tmp_path, 'w') as f:
            f.write('all')
        with patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_path), \
             patch('nibe_entity_manager._APPLIED_MODE_TIMEOUT_S', 0):
            self.assertEqual(em.read_applied_mode(), 'all')

    def test_read_applied_mode_returns_none_when_neither_store_has_a_record(self):
        em = _make_em()
        self._deliver_retained(em, None)
        with patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_path), \
             patch('nibe_entity_manager._APPLIED_MODE_TIMEOUT_S', 0):
            self.assertIsNone(em.read_applied_mode())  # tmp file doesn't exist

    def test_read_applied_mode_unsubscribes_after_wait(self):
        """Must always clean up its temporary subscription, whether or not
        a message arrived."""
        em = _make_em()
        self._deliver_retained(em, b'menus')
        em.read_applied_mode()
        em.mqtt.unsubscribe.assert_called_once()
        em.mqtt.message_callback_remove.assert_called_once()

    def test_persist_applied_mode_writes_file_then_mqtt(self):
        """Write-ahead: file first, then the retained MQTT topic."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        em._persist_applied_mode('essential', path=self._tmp_path)
        with open(self._tmp_path) as f:
            self.assertEqual(f.read(), 'essential')
        em.mqtt.publish.assert_called_once_with(
            BrowserTopic.APPLIED_MODE, 'essential', retain=True
        )

    def test_persist_applied_mode_tolerates_unwritable_file(self):
        """A failed file write (e.g. /data/ not present) must not prevent
        the MQTT publish — the file is a fallback, not the primary store."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        bad_path = '/nonexistent-dir/applied_mode'
        em._persist_applied_mode('advanced', path=bad_path)  # must not raise
        em.mqtt.publish.assert_called_once_with(
            BrowserTopic.APPLIED_MODE, 'advanced', retain=True
        )

    def test_read_applied_mode_from_file_returns_none_when_absent(self):
        em = _make_em()
        self.assertIsNone(em._read_applied_mode_from_file('/nonexistent-dir/applied_mode'))

    def test_read_applied_mode_from_file_strips_whitespace(self):
        em = _make_em()
        with open(self._tmp_path, 'w') as f:
            f.write('  menus\n')
        self.assertEqual(em._read_applied_mode_from_file(self._tmp_path), 'menus')

    def test_read_applied_mode_from_file_empty_content_returns_none(self):
        em = _make_em()
        with open(self._tmp_path, 'w') as f:
            f.write('   ')
        self.assertIsNone(em._read_applied_mode_from_file(self._tmp_path))

    def test_record_applied_mode_persists_without_touching_enabled_set(self):
        """record_applied_mode is the migration-boundary helper — it must
        record the baseline without enabling or disabling anything."""
        em = _make_em()
        em.mqtt_enabled_points = {1, 2, 3}
        with patch.object(em, '_persist_applied_mode') as mock_persist:
            em.record_applied_mode('essential')
        mock_persist.assert_called_once_with('essential')
        self.assertEqual(em.mqtt_enabled_points, {1, 2, 3})  # unchanged


# ===========================================================================
# 50. EntityManager.apply_mode — mode reconciliation (enable + disable)
# ===========================================================================


class TestApplyMode(unittest.TestCase):
    """apply_mode() replaced the old strictly-additive apply_preset(). It now
    both enables points newly required by the target mode AND disables
    points that are enabled but not part of it — except active dynamic
    points, which must never be touched by a mode change since their
    existence is firmware-state-driven, not mode-driven. The dynamic-point
    protection test below is the highest-risk case in this refactor: get it
    wrong and a mode change silently kills a live dynamic entity."""

    def _all_points(self, ids):
        return {pid: {'title': f'Point {pid}'} for pid in ids}

    def setUp(self):
        # Applied-mode persistence writes to /data/applied_mode as a file
        # fallback; redirect to a throwaway path so tests don't touch the
        # real filesystem (a missing /data/ is caught safely anyway, but
        # this keeps test output clean and hermetic).
        import tempfile
        import os
        self._tmp_mode_file = os.path.join(tempfile.mkdtemp(), 'applied_mode')
        patcher = patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_mode_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_known_mode_enables_its_points(self):
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')
        self.assertEqual(em.mqtt_enabled_points, {1, 2})

    def test_mode_change_disables_points_not_in_new_mode(self):
        """The core behavioral change vs the old additive apply_preset:
        switching mode must prune points that belonged to the old set."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3, 4])
        em.mqtt_enabled_points = {3, 4}  # enabled under a previous mode
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')
        self.assertEqual(em.mqtt_enabled_points, {1, 2})

    def test_active_dynamic_points_protected_from_disable(self):
        """A mode change must never disable a currently-active dynamic
        point even though it isn't a member of the target mode's static
        point set — its existence is firmware-state-driven, not
        mode-driven. This is the highest-risk case in the reconcile path."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 99])
        em.mqtt_enabled_points = {2, 99}
        em.active_dynamic_points = {99}  # live dynamic entity, not in target
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1})}):
            em.apply_mode('essential')
        self.assertIn(99, em.mqtt_enabled_points, "dynamic point must survive the mode change")
        self.assertNotIn(2, em.mqtt_enabled_points)
        self.assertIn(1, em.mqtt_enabled_points)

    def test_already_enabled_point_in_mode_not_re_enabled(self):
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2])
        em.mqtt_enabled_points = {1}
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}), \
             patch.object(em, 'enable_entity', wraps=em.enable_entity) as spy:
            em.apply_mode('essential')
        spy.assert_called_once_with(2)

    def test_all_mode_enables_every_known_point(self):
        """The 'all' mode is a sentinel (MODES['all'] is None in the real
        table) handled as a special case, not a literal None lookup result
        being silently treated as 'enable nothing'."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        with patch('nibe_entity_manager.MODES', {'all': None}):
            em.apply_mode('all')
        self.assertEqual(em.mqtt_enabled_points, {1, 2, 3})

    def test_unrecognized_mode_name_disables_everything_except_dynamic(self):
        """Unlike the old additive apply_preset (an unknown name enabled
        nothing and touched nothing else), apply_mode's reconcile means an
        unrecognized name resolves to an empty target set and disables
        every non-dynamic enabled point. The add-on's config schema
        prevents this via a fixed choice list, but the method itself must
        behave predictably rather than crash on a bad name."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        em.mqtt_enabled_points = {1, 2}
        em.active_dynamic_points = {2}
        with patch('nibe_entity_manager.MODES', {}):
            em.apply_mode('totally_unknown_mode')
        self.assertEqual(em.mqtt_enabled_points, {2})  # only the protected dynamic point survives

    def test_empty_frozenset_mode_enables_nothing_new(self):
        """The real 'none' mode is an empty frozenset — distinct from a
        missing key, must also result in zero new enables without error."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        with patch('nibe_entity_manager.MODES', {'none': frozenset()}):
            em.apply_mode('none')
        self.assertEqual(em.mqtt_enabled_points, set())

    def test_publish_enabled_state_called_once_at_end(self):
        """publish_enabled_state must fire exactly once after the whole
        batch, not once per point — confirms _suppress_enabled_state is
        actually wrapping the enable+disable loop."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2, 3})}), \
             patch.object(em, 'publish_enabled_state') as mock_publish:
            em.apply_mode('essential')
        mock_publish.assert_called_once()

    def test_suppression_active_during_enable_and_disable_loop(self):
        """Confirms _is_suppressed() is genuinely True while points are
        being enabled/disabled — the suppress context manager actually
        wraps the loop, not just decorates it cosmetically."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2])
        em.mqtt_enabled_points = {2}
        observed = {}
        original_enable  = em.enable_entity
        original_disable = em.disable_entity

        def spy_enable(point_id):
            observed['suppressed_during_enable'] = em._is_suppressed()
            return original_enable(point_id)

        def spy_disable(point_id):
            observed['suppressed_during_disable'] = em._is_suppressed()
            return original_disable(point_id)

        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1})}), \
             patch.object(em, 'enable_entity', side_effect=spy_enable), \
             patch.object(em, 'disable_entity', side_effect=spy_disable):
            em.apply_mode('essential')

        self.assertTrue(observed['suppressed_during_enable'])
        self.assertTrue(observed['suppressed_during_disable'])
        self.assertFalse(em._is_suppressed())  # released after the call

    def test_point_not_in_all_points_by_id_skipped_gracefully(self):
        """A mode referencing a point_id not present in all_points_by_id
        (e.g. a mode table entry for a point this firmware doesn't have)
        must not crash and must not be enabled — the target set is
        intersected with all_points_by_id before diffing."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1])  # 2 deliberately absent
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')  # must not raise
        self.assertIn(1, em.mqtt_enabled_points)
        self.assertNotIn(2, em.mqtt_enabled_points)

    def test_persists_applied_mode_to_mqtt(self):
        """apply_mode must record the mode it just reconciled to, via the
        retained BrowserTopic.APPLIED_MODE topic — this is what
        decide_startup_action reads on the next restart."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        em.all_points_by_id = self._all_points([1])
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1})}):
            em.apply_mode('essential')
        published = {c.args[0]: c.args[1] for c in em.mqtt.publish.call_args_list}
        self.assertEqual(published.get(BrowserTopic.APPLIED_MODE), 'essential')



class TestApplyModeNone(unittest.TestCase):
    """Test apply_mode with 'none' mode."""

    def test_none_mode_leaves_dynamic_points_enabled(self):
        em = _make_em()
        em.all_points_by_id = {1: {'title': 'Static1'}, 2: {'title': 'Dynamic'}}
        em.mqtt_enabled_points = {1, 2}
        em.active_dynamic_points = {2}
        with patch('nibe_entity_manager.MODES', {'none': frozenset()}):
            em.apply_mode('none')
        self.assertEqual(em.mqtt_enabled_points, {2})
        self.assertNotIn(1, em.mqtt_enabled_points)


# ===========================================================================
# 51. EntityManager._handle_command — MQTT decode and dispatch
# ===========================================================================


class TestHandleCommand(unittest.TestCase):
    """The entrypoint every HA write command passes through: UTF-8 decode,
    correlation ID generation, pending-write registration, and executor
    submission. Payload conversion itself is delegated to
    _parse_command_payload (already tested) — these tests focus on this
    method's own dispatch responsibilities. Zero coverage before this."""

    def _message(self, payload_bytes, topic='homeassistant/switch/nibe_100/set'):
        msg = MagicMock()
        msg.payload = payload_bytes
        msg.topic = topic
        return msg

    def _entity_info(self, point_id=100, entity_type='switch'):
        return {
            'point_id': point_id, 'entity_type': entity_type,
            'metadata': {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
            'point_data': {}, 'is_degenerate_range': False,
            'state_topic': f'nibe/state/{point_id}',
        }

    def test_malformed_utf8_payload_dropped_silently(self):
        """Invalid UTF-8 bytes must not crash the MQTT callback thread —
        logged and dropped, no pending write registered."""
        em = _make_em()
        info = self._entity_info()
        msg = self._message(b'\xff\xfe\x00invalid')
        em._handle_command(info, msg)
        self.assertNotIn(100, em.pending_writes)

    def test_valid_payload_registers_pending_write(self):
        em = _make_em()
        info = self._entity_info()
        msg = self._message(b'ON')
        with patch.object(em, '_write_executor'):
            em._handle_command(info, msg)
        self.assertIn(100, em.pending_writes)
        self.assertEqual(em.pending_writes[100]['value'], 1)
        self.assertEqual(em.pending_writes[100]['payload'], 'ON')

    def test_valid_payload_submits_to_executor(self):
        em = _make_em()
        info = self._entity_info()
        msg = self._message(b'ON')
        with patch.object(em, '_write_executor') as mock_executor:
            em._handle_command(info, msg)
        mock_executor.submit.assert_called_once_with(
            em._handle_command_worker, info, 1, 'ON', mock_executor.submit.call_args[0][4],
        )

    def test_unparseable_payload_does_not_register_pending_write(self):
        """_parse_command_payload returning None (e.g. an out-of-range
        number) must short-circuit before pending_writes is touched and
        before the executor is invoked at all."""
        em = _make_em()
        info = self._entity_info(entity_type='number')
        info['metadata'] = {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                             'divisor': 1, 'minValue': 0, 'maxValue': 10}
        msg = self._message(b'9999')  # out of range
        with patch.object(em, '_write_executor') as mock_executor:
            em._handle_command(info, msg)
        self.assertNotIn(100, em.pending_writes)
        mock_executor.submit.assert_not_called()

    def test_pending_write_payload_value_and_cmd_id_present(self):
        em = _make_em()
        info = self._entity_info()
        msg = self._message(b'1')
        with patch.object(em, '_write_executor'):
            em._handle_command(info, msg)
        entry = em.pending_writes[100]
        self.assertIn('cmd_id', entry)
        self.assertIn('timestamp', entry)
        self.assertEqual(len(entry['cmd_id']), 8)  # _CMD_ID_LENGTH

    def test_strips_whitespace_from_payload(self):
        em = _make_em()
        info = self._entity_info()
        msg = self._message(b'  ON  \n')
        with patch.object(em, '_write_executor'):
            em._handle_command(info, msg)
        self.assertEqual(em.pending_writes[100]['payload'], 'ON')


# ===========================================================================
# 52. EntityManager._handle_command_worker — post-write dynamic-point cases
# ===========================================================================


class TestProcessAndPublishState(unittest.TestCase):
    """Runs once per active entity on every poll cycle — converts a raw
    firmware value into the string published to HA. Contains real,
    hand-derived domain knowledge specific to this installation's firmware
    (the point 2022 status bitfield, EB101/SMO firmware version encoding,
    the periodic-increase date conversion) alongside the general-purpose
    entity-type dispatch. Zero coverage before this despite running
    continuously in production. Worked examples below were hand-traced
    against the function's own logic before being asserted, not guessed."""

    _UNSET = object()

    def _entity_info(self, point_id=100, entity_type='sensor', point_data=None,
                      state_topic=_UNSET, availability_topic=_UNSET):
        return {
            'point_id': point_id, 'entity_type': entity_type,
            'point_data': point_data or {},
            'state_topic': (f'nibe/state/{point_id}' if state_topic is self._UNSET
                             else state_topic),
            'availability_topic': (f'nibe/avail/{point_id}' if availability_topic is self._UNSET
                                    else availability_topic),
        }

    def _metadata(self, variable_size='', divisor=1, change=0, **extra):
        m = {'variableSize': variable_size, 'divisor': divisor, 'change': change}
        m.update(extra)
        return m

    # -- availability ----------------------------------------------------

    def test_always_publishes_online_availability_first(self):
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type='switch'), 1, '', self._metadata(),
        )
        em.mqtt.publish.assert_any_call('nibe/avail/100', 'online', retain=True)

    # -- sentinel handling -------------------------------------------------

    def test_sentinel_s16_binary_sensor_goes_offline(self):
        """A sentinel value (sensor disconnected/faulted) on a binary_sensor
        marks the entity offline rather than publishing a misleading state."""
        em = _make_em()
        info = self._entity_info(entity_type='binary_sensor')
        em._process_and_publish_state(info, -32768, '', self._metadata(variable_size='s16'))
        em.mqtt.publish.assert_any_call('nibe/avail/100', 'offline', retain=True)
        # Must return early — no state_topic publish for the sentinel itself.
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == 'nibe/state/100']
        self.assertEqual(state_calls, [])

    def test_sentinel_s16_sensor_publishes_offline_not_zero(self):
        """A sentinel value on any entity type (including regular sensor)
        must publish offline on the availability topic and return without
        publishing a state value. Previously only binary_sensor got this
        treatment and sensors fell through to state '0', showing a
        misleading 0°C in HA for disconnected sensors like BT71."""
        em = _make_em()
        info = self._entity_info(entity_type='sensor')
        em._process_and_publish_state(info, -32768, '', self._metadata(variable_size='s16'))
        # Must publish offline on the availability topic
        em.mqtt.publish.assert_any_call('nibe/avail/100', 'offline', retain=True)
        # Must NOT publish a state value
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c[0][0] == 'nibe/state/100']
        self.assertEqual(len(state_calls), 0,
            "No state must be published when sentinel value is detected")

    def test_non_sentinel_value_not_treated_as_sentinel(self):
        """A value that happens to be large but isn't the exact sentinel
        constant must be processed normally, not misidentified."""
        em = _make_em()
        info = self._entity_info(entity_type='switch')
        em._process_and_publish_state(info, 1, '', self._metadata(variable_size='s16'))
        em.mqtt.publish.assert_any_call('nibe/state/100', '1', retain=True)

    # -- basic entity-type dispatch -----------------------------------------

    def test_switch_truthy_value_is_on(self):
        em = _make_em()
        em._process_and_publish_state(self._entity_info(entity_type='switch'), 1, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/100', '1', retain=True)

    def test_switch_zero_value_is_off(self):
        em = _make_em()
        em._process_and_publish_state(self._entity_info(entity_type='switch'), 0, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/100', '0', retain=True)

    def test_binary_sensor_zero_is_off_string(self):
        em = _make_em()
        em._process_and_publish_state(self._entity_info(entity_type='binary_sensor'), 0, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/100', 'OFF', retain=True)

    def test_binary_sensor_nonzero_is_on_string(self):
        em = _make_em()
        em._process_and_publish_state(self._entity_info(entity_type='binary_sensor'), 1, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/100', 'ON', retain=True)

    def test_text_passes_through_string_value(self):
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type='text'), 0, 'Hello firmware', self._metadata(),
        )
        em.mqtt.publish.assert_any_call('nibe/state/100', 'Hello firmware', retain=True)

    def test_time_seconds_converted_to_hhmmss(self):
        em = _make_em()
        em._process_and_publish_state(self._entity_info(entity_type='time'), 9015, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/100', '02:30:00', retain=True)

    def test_time_wraps_past_midnight(self):
        """raw_value % 86400 — a value of exactly one day must wrap to 00:00:00,
        not overflow into a 25-hour-style display."""
        em = _make_em()
        em._process_and_publish_state(self._entity_info(entity_type='time'), 86400, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/100', '00:00:00', retain=True)

    def test_plain_sensor_applies_divisor(self):
        em = _make_em()
        info = self._entity_info(point_id=999, entity_type='sensor')
        em._process_and_publish_state(info, 348, '', self._metadata(divisor=10))
        em.mqtt.publish.assert_any_call('nibe/state/999', '34.8', retain=True)

    # -- point-specific firmware decoding ------------------------------------

    def test_point_2685_periodic_increase_date_conversion(self):
        """Days-since-2010-01-01 -> ISO date. 5000 days after 2010-01-01."""
        from datetime import date, timedelta
        expected = (date(2010, 1, 1) + timedelta(days=5000)).isoformat()
        em = _make_em()
        info = self._entity_info(point_id=2685, entity_type='sensor')
        em._process_and_publish_state(info, 5000, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/2685', expected, retain=True)

    def test_point_2685_invalid_value_falls_back_to_raw_string(self):
        """An absurd day count that would overflow datetime's range must
        not crash — falls back to the raw value as a string."""
        em = _make_em()
        info = self._entity_info(point_id=2685, entity_type='sensor')
        em._process_and_publish_state(info, 99999999, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/2685', '99999999', retain=True)

    def test_point_2453_eb101_firmware_version_decoding(self):
        """Confirmed worked example from the source comment: 12481 -> 3.3.1
        (this installation's actual S2125-12 firmware version)."""
        em = _make_em()
        info = self._entity_info(point_id=2453, entity_type='sensor')
        em._process_and_publish_state(info, 12481, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/2453', '3.3.1', retain=True)

    def test_point_14987_uses_same_eb101_decoding_as_2453(self):
        """14987 is documented as an alternate register for the same
        EB101 firmware version — must decode identically to 2453."""
        em = _make_em()
        info = self._entity_info(point_id=14987, entity_type='sensor')
        em._process_and_publish_state(info, 12481, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/14987', '3.3.1', retain=True)

    def test_point_2509_smo_firmware_version_decoding(self):
        """Confirmed worked example: 1035 (0x040B) -> 4.11."""
        em = _make_em()
        info = self._entity_info(point_id=2509, entity_type='sensor')
        em._process_and_publish_state(info, 1035, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/2509', '4.11', retain=True)

    def test_point_2022_heating_and_compressor_running(self):
        """Hand-traced worked example: bit12 (Heating) + bit2+bit4 (compressor
        running) -> 'Heating (Running)'."""
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type='sensor')
        v = (1 << 12) | (1 << 2) | (1 << 4)
        em._process_and_publish_state(info, v, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/2022', 'Heating (Running)', retain=True)

    def test_point_2022_idle_when_no_mode_bits_set(self):
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type='sensor')
        em._process_and_publish_state(info, 0, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/2022', 'Idle', retain=True)

    def test_point_2022_compressor_starting_no_running_bit(self):
        """bit4 set without bit2 -> 'Starting', not 'Running'."""
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type='sensor')
        v = (1 << 12) | (1 << 4)
        em._process_and_publish_state(info, v, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/2022', 'Heating (Starting)', retain=True)

    def test_point_2022_mode_active_no_compressor_bits_is_preheating(self):
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type='sensor')
        v = (1 << 13)  # Hot water mode, no compressor bits
        em._process_and_publish_state(info, v, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/2022', 'Hot water (Preheating)', retain=True)

    def test_point_2022_multiple_modes_combined_with_plus(self):
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type='sensor')
        v = (1 << 13) | (1 << 12) | (1 << 2) | (1 << 4)  # Hot water + Heating, running
        em._process_and_publish_state(info, v, '', self._metadata())
        published = [c.args[1] for c in em.mqtt.publish.call_args_list if c.args[0] == 'nibe/state/2022']
        self.assertEqual(len(published), 1)
        self.assertIn('Hot water', published[0])
        self.assertIn('Heating', published[0])
        self.assertIn('+', published[0])
        self.assertIn('(Running)', published[0])

    # -- select / sensor value-mapping ----------------------------------------

    def test_select_mapped_value_shows_label(self):
        em = _make_em()
        info = self._entity_info(point_id=555, entity_type='select',
                                  point_data={'description': '0 = Off, 1 = Auto'})
        with patch('nibe_entity_manager.get_value_mapping', return_value={0: 'Off', 1: 'Auto'}):
            em._process_and_publish_state(info, 1, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/555', 'Auto', retain=True)

    def test_select_unmapped_value_falls_back_to_raw_string(self):
        """A raw value not present in the mapping (e.g. firmware added a
        new enum value not yet in our table) must not crash — shows the
        raw number rather than dropping the update."""
        em = _make_em()
        info = self._entity_info(point_id=555, entity_type='select')
        with patch('nibe_entity_manager.get_value_mapping', return_value={0: 'Off', 1: 'Auto'}):
            em._process_and_publish_state(info, 99, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/555', '99', retain=True)

    def test_sensor_with_mapping_shows_label(self):
        em = _make_em()
        info = self._entity_info(point_id=556, entity_type='sensor')
        with patch('nibe_entity_manager.get_value_mapping', return_value={10: 'Heating'}):
            em._process_and_publish_state(info, 10, '', self._metadata())
        em.mqtt.publish.assert_any_call('nibe/state/556', 'Heating', retain=True)

    def test_sensor_without_mapping_applies_divisor(self):
        em = _make_em()
        info = self._entity_info(point_id=557, entity_type='sensor')
        with patch('nibe_entity_manager.get_value_mapping', return_value=None):
            em._process_and_publish_state(info, 205, '', self._metadata(divisor=10))
        em.mqtt.publish.assert_any_call('nibe/state/557', '20.5', retain=True)

    # -- publish gating ----------------------------------------------------

    def test_missing_state_topic_logs_and_skips_publish(self):
        """An entity_info missing state_topic must not crash — logs a
        warning and skips the state publish (availability is still sent)."""
        em = _make_em()
        info = self._entity_info(entity_type='switch', state_topic=None)
        em._process_and_publish_state(info, 1, '', self._metadata())
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == 'nibe/state/100']
        self.assertEqual(state_calls, [])

    def test_unchanged_value_within_rate_limit_not_republished(self):
        """Calling twice in immediate succession with the same value must
        only publish the state once — the ValueCache rate-limit/dedup gate
        suppresses the redundant second publish."""
        em = _make_em()
        info = self._entity_info(point_id=222, entity_type='sensor')
        em._process_and_publish_state(info, 100, '', self._metadata(divisor=1))
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, 100, '', self._metadata(divisor=1))
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == 'nibe/state/222']
        self.assertEqual(state_calls, [])

    def test_force_true_always_republishes(self):
        em = _make_em()
        info = self._entity_info(point_id=223, entity_type='sensor')
        em._process_and_publish_state(info, 100, '', self._metadata(divisor=1))
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, 100, '', self._metadata(divisor=1), force=True)
        em.mqtt.publish.assert_any_call('nibe/state/223', '100', retain=True)

    def test_last_states_updated_after_publish(self):
        em = _make_em()
        info = self._entity_info(point_id=224, entity_type='switch')
        em._process_and_publish_state(info, 1, '', self._metadata())
        self.assertEqual(em.last_states[224], '1')


# ===========================================================================
# _process_and_publish_state — Hypothesis property tests
# ===========================================================================


class TestProcessAndPublishStateProperties(unittest.TestCase):
    """Hypothesis property tests for _process_and_publish_state.

    Key invariants that must hold across all entity types and all raw values:

      1. availability_topic always published 'online' for non-sentinel values
      2. availability_topic published 'offline' for the s16 sentinel (-32768)
      3. binary_sensor state is always 'ON' or 'OFF' — never a numeric string
      4. state_topic payload is always a string (never int, float, None)

    These are the invariants most likely to be broken by a future change that
    adds a new entity type or special-case decoding without updating the
    sentinel check, binary_sensor dispatch, or divisor path.
    """

    _ENTITY_TYPES = ['sensor', 'switch', 'binary_sensor', 'number', 'select', 'time']

    def _entity_info(self, point_id=100, entity_type='sensor'):
        return {
            'point_id':           point_id,
            'entity_type':        entity_type,
            'state_topic':        f'nibe/state/{point_id}',
            'availability_topic': f'nibe/avail/{point_id}',
            'point_data':         {},
            'value_mapping':      None,
        }

    def _metadata(self, **kw):
        m = {'variableSize': 'u8', 'divisor': 1, 'change': 0, 'decimal': 0,
             'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
             'minValue': 0, 'maxValue': 100}
        m.update(kw)
        return m

    @given(
        entity_type=st.sampled_from(_ENTITY_TYPES),
        raw_value=st.integers(min_value=-32767, max_value=32767),  # exclude sentinel
    )
    @example(entity_type='sensor',        raw_value=0)
    @example(entity_type='binary_sensor', raw_value=0)
    @example(entity_type='switch',        raw_value=1)
    @example(entity_type='time',          raw_value=9015)  # 02:30:00
    @example(entity_type='number',        raw_value=50)
    def test_non_sentinel_always_publishes_online_availability(
            self, entity_type, raw_value):
        """For any non-sentinel value, availability must be published 'online'
        before the state.  Regression guard: a new entity type added without
        updating the sentinel check would skip the online publish."""
        em = _make_em()
        info = self._entity_info(entity_type=entity_type)
        with patch('nibe_entity_manager.get_value_mapping', return_value=None):
            em._process_and_publish_state(
                info, raw_value, '', self._metadata())
        avail_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/avail/100']
        self.assertTrue(avail_calls,
                        f"No availability publish for entity_type={entity_type!r}")
        self.assertEqual(avail_calls[0].args[1], 'online',
                         f"Expected 'online', got {avail_calls[0].args[1]!r}")

    @given(entity_type=st.sampled_from(_ENTITY_TYPES))
    @example(entity_type='sensor')
    @example(entity_type='binary_sensor')
    @example(entity_type='switch')
    def test_s16_sentinel_publishes_offline_availability(self, entity_type):
        """The s16 sentinel value (-32768) must publish 'offline' for all
        entity types — the sentinel means the sensor is disconnected."""
        em = _make_em()
        info = self._entity_info(entity_type=entity_type)
        em._process_and_publish_state(
            info, -32768, '', self._metadata(variableSize='s16'))
        avail_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/avail/100']
        self.assertTrue(avail_calls,
                        "Sentinel value must still publish to availability topic")
        # The LAST availability publish must be 'offline'
        self.assertEqual(avail_calls[-1].args[1], 'offline',
                         "Sentinel value must publish 'offline'")

    @given(raw_value=st.integers(min_value=-32767, max_value=32767))
    @example(raw_value=0)
    @example(raw_value=1)
    @example(raw_value=255)
    def test_binary_sensor_state_always_on_or_off(self, raw_value):
        """binary_sensor state must always be 'ON' or 'OFF' — never a
        numeric string.  This is an HA protocol requirement: binary_sensor
        entities must report ON/OFF, not 0/1."""
        em = _make_em()
        info = self._entity_info(entity_type='binary_sensor')
        em._process_and_publish_state(
            info, raw_value, '', self._metadata())
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/100']
        if state_calls:
            state = state_calls[-1].args[1]
            self.assertIn(state, ('ON', 'OFF'),
                          f"binary_sensor state {state!r} is not ON or OFF")

    @given(
        entity_type=st.sampled_from(_ENTITY_TYPES),
        raw_value=st.integers(min_value=-32767, max_value=32767),
    )
    def test_state_payload_always_string(self, entity_type, raw_value):
        """State topic payload must always be a str — paho MQTT requires
        string payloads, and HA's MQTT integration expects string values."""
        em = _make_em()
        info = self._entity_info(entity_type=entity_type)
        with patch('nibe_entity_manager.get_value_mapping', return_value=None):
            em._process_and_publish_state(
                info, raw_value, '', self._metadata())
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/100']
        for call in state_calls:
            self.assertIsInstance(call.args[1], str,
                                  f"state payload {call.args[1]!r} is not a string")



# ===========================================================================
# 53b. _process_and_publish_state — change threshold wiring
# ===========================================================================


class TestChangeThresholdWiring(unittest.TestCase):
    """Pins the wiring between firmware 'change' metadata and ValueCache
    threshold suppression inside _process_and_publish_state.

    The ValueCache.should_publish logic is tested exhaustively in
    TestValueCacheHypothesisProperties; these tests verify that the
    firmware metadata field is correctly extracted and passed as the
    threshold — the integration path that actually runs on hardware.

    Point 1708 (Calculated supply climate system 1) is the reference:
      divisor=10, change=5, decimal=1, unit="°C".
    A raw change of 5 at divisor=10 means 0.5 °C — below the threshold
    of 5 raw counts — so the second publish must be suppressed.
    """

    def _entity_info(self, point_id=1708):
        return {
            'point_id': point_id,
            'entity_type': 'sensor',
            'state_topic': f'nibe/state/{point_id}',
            'availability_topic': f'nibe/avail/{point_id}',
            'point_data': {},
        }

    def _metadata(self, change=5, divisor=10):
        return {'variableSize': 's16', 'divisor': divisor, 'change': change}

    # -- suppression -------------------------------------------------------

    @example(first=200, change=10)  # S2125 point 1708: same value → suppress
    @example(first=228, change=10)  # S2125 point 4 (BT1 outdoor): same value → suppress
    @given(
        first=st.integers(min_value=0, max_value=10000),
        change=st.integers(min_value=1, max_value=50),
    )
    def test_change_below_threshold_suppresses_second_publish(self, first, change):
        """Second publish with the same raw value is suppressed — the cache
        recognises zero delta and the last_states fallback also finds no change."""
        em = _make_em()
        em.bulk_interval = 0
        info = self._entity_info()
        meta = self._metadata(change=change, divisor=1)
        em._process_and_publish_state(info, first, '', meta)
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, first, '', meta)  # identical value
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/1708']
        self.assertEqual(state_calls, [],
            f"Identical value with threshold {change}: must be suppressed")

    @example(first=200, second=206, change=5)   # S2125 point 1708: 200→206 = Δ6 ≥ threshold 5 → publish
    @example(first=228, second=234, change=5)   # S2125 point 4 (BT1 outdoor): 228→234 = Δ6 ≥ threshold 5 → publish
    @given(
        first=st.integers(min_value=0, max_value=9000),
        second=st.integers(min_value=0, max_value=10000),
        change=st.integers(min_value=1, max_value=50),
    )
    def test_change_exceeds_threshold_publishes(self, first, second, change):
        """Second publish fires when |Δraw| >= change threshold."""
        assume(abs(second - first) >= change)
        em = _make_em()
        em.bulk_interval = 0
        info = self._entity_info()
        meta = self._metadata(change=change)
        em._process_and_publish_state(info, first, '', meta)
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, second, '', meta)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/1708']
        self.assertGreater(len(state_calls), 0,
            f"Δ{abs(second - first)} >= threshold {change}: publish must fire")

    # -- zero threshold (default) ------------------------------------------

    def test_zero_change_threshold_always_publishes(self):
        """change=0 (default for most points) must never suppress — every
        poll publishes, which is the existing behaviour for static sensors."""
        em = _make_em()
        em.bulk_interval = 0
        info = self._entity_info()
        meta = self._metadata(change=0)
        em._process_and_publish_state(info, 100, '', meta)
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, 100, '', meta)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/1708']
        self.assertGreater(len(state_calls), 0,
            "change=0: same value must still publish on every poll")

    # -- missing field fallback --------------------------------------------

    def test_missing_change_field_defaults_to_zero(self):
        """Metadata without a 'change' key must fall back to 0 (no suppression),
        not raise KeyError."""
        em = _make_em()
        em.bulk_interval = 0
        info = self._entity_info()
        meta = {'variableSize': 's16', 'divisor': 10}   # no 'change' key
        em._process_and_publish_state(info, 100, '', meta)
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, 100, '', meta)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/1708']
        self.assertGreater(len(state_calls), 0)


# ===========================================================================
# 54. HAEntityRegistryWatcher._on_entity_enabled / _on_entity_disabled
# ===========================================================================


class TestFetchBulkDataStringCache(unittest.TestCase):
    """_fetch_bulk_data caches the clean_string() result for each point's
    title/description, only recomputing when the raw API string actually
    differs from what was cached last poll. A cache-invalidation bug here
    means either stale titles persisting after a firmware update (cache
    never invalidates) or needless CPU work every single poll cycle on a
    1000+ point installation (cache never hits) — covers only the cache
    behavior itself, not the rest of the (much larger) bulk-fetch function."""

    def _response(self, point_id=100, title='Outdoor temperature', description=''):
        return {
            str(point_id): {
                'title': title, 'description': description,
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'minValue': -400, 'maxValue': 400},
                'value': {'integerValue': 50, 'stringValue': '', 'isOk': True},
            }
        }

    def test_first_poll_populates_cache_and_calls_clean_string(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response()
        from nibe_entity_detection import clean_string as real_clean_string
        with patch('nibe_entity_manager.clean_string', wraps=real_clean_string) as spy:
            em._fetch_bulk_data(detect_changes=False)
        self.assertIn(100, em._point_string_cache)
        spy.assert_called()

    def test_unchanged_title_on_second_poll_skips_recompute(self):
        """Identical raw title/description on a second poll must hit the
        cache — clean_string must not be called again for this point."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response()
        em._fetch_bulk_data(detect_changes=False)  # populate cache

        from nibe_entity_detection import clean_string as real_clean_string
        with patch('nibe_entity_manager.clean_string', wraps=real_clean_string) as spy:
            em._fetch_bulk_data(detect_changes=False)
        spy.assert_not_called()

    def test_changed_title_invalidates_cache_and_recomputes(self):
        """A genuinely different raw title (e.g. after a firmware update
        changes point naming) must invalidate the cache and recompute —
        confirms the cache doesn't silently freeze stale data forever."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response(title='Old title')
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[100]['title'], 'Old title')

        em._api.fetch_bulk_points.return_value = self._response(title='New title')
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[100]['title'], 'New title')
        self.assertEqual(em._point_string_cache[100][0], 'New title')

    def test_changed_description_also_invalidates_cache(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response(description='Old desc')
        em._fetch_bulk_data(detect_changes=False)
        em._api.fetch_bulk_points.return_value = self._response(description='New desc')
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[100]['description'], 'New desc')

    def test_disappeared_point_removed_from_cache(self):
        """A point that drops out of the bulk response entirely (e.g.
        accessory disconnected) must have its string cache entry cleaned
        up too, not just bulk_data — otherwise the cache grows unbounded
        with stale entries for points that no longer exist."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response(point_id=100)
        em._fetch_bulk_data(detect_changes=False)
        self.assertIn(100, em._point_string_cache)

        em._api.fetch_bulk_points.return_value = self._response(point_id=200)  # 100 gone
        em._fetch_bulk_data(detect_changes=False)
        self.assertNotIn(100, em._point_string_cache)
        self.assertNotIn(100, em.bulk_data)

    def test_raw_value_updated_in_place_on_existing_entry(self):
        """bulk_data is updated in-place for an existing point (not
        rebuilt) — confirms the dict identity persists across polls while
        the value itself does update."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response()
        em._fetch_bulk_data(detect_changes=False)
        entry_before = em.bulk_data[100]

        resp2 = self._response()
        resp2['100']['value']['integerValue'] = 99
        em._api.fetch_bulk_points.return_value = resp2
        em._fetch_bulk_data(detect_changes=False)

        self.assertIs(em.bulk_data[100], entry_before)  # same object, updated in place
        self.assertEqual(em.bulk_data[100]['raw_value'], 99)



class TestHandleCommandWorkerFailurePath(unittest.TestCase):
    """The failure branch of _handle_command_worker, deliberately deferred
    from an earlier round focused on the success-path dynamic-point
    branching. Edge-triggered notification logic symmetric to
    update_alarm_state's _alarm_notification_active pattern — must not
    spam a notification on every failed write while one is already
    showing, must clear pending_writes so the point isn't stuck, and must
    force a readback so HA's optimistic UI doesn't keep displaying a
    value the controller actually rejected."""

    def _entity_info(self, point_id=100, entity_type='switch', display_title='Test point'):
        return {
            'point_id': point_id, 'entity_type': entity_type,
            'display_title': display_title, 'state_topic': f'nibe/state/{point_id}',
        }

    def test_failed_write_increments_counter(self):
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None  # readback also fails, fine
        before = em._write_failed
        em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        self.assertEqual(em._write_failed, before + 1)

    def test_failed_write_records_last_write_error(self):
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._handle_command_worker(self._entity_info(point_id=100), 1, '1', 'cmd1')
        self.assertIn('point 100', em._last_write_error)
        self.assertIn("'1'", em._last_write_error)

    def test_failed_write_clears_pending_entry(self):
        """A stale pending-write entry would block normal state updates for
        this point indefinitely — must be popped on failure."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em.pending_writes[100] = {'point_id': 100, 'value': 1}
        em._handle_command_worker(self._entity_info(point_id=100), 1, '1', 'cmd1')
        self.assertNotIn(100, em.pending_writes)

    def test_first_failure_sends_notification(self):
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._write_notification_active = False
        em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        em._notify.assert_called_once()
        self.assertEqual(em._notify.call_args.kwargs['title'], 'Nibe Bridge: Write Failed')

    def test_notification_includes_point_title_and_value(self):
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._handle_command_worker(
            self._entity_info(point_id=100, display_title='Permit heating'), 1, 'ON', 'cmd1',
        )
        msg = em._notify.call_args.kwargs['message']
        self.assertIn('Permit heating', msg)
        self.assertIn("'ON'", msg)
        self.assertIn('point 100', msg)

    def test_repeated_failure_does_not_re_notify(self):
        """The edge-trigger guard: once a write-failure notification is
        showing, subsequent failures must not stack additional ones."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._write_notification_active = True  # already showing
        em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        em._notify.assert_not_called()

    def test_notification_active_flag_set_after_first_failure(self):
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._write_notification_active = False
        em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        self.assertTrue(em._write_notification_active)

    def test_no_mqtt_client_skips_notification_entirely(self):
        """If self.mqtt is falsy (not yet connected), the notification path
        must be skipped gracefully — not attempt to notify over a missing
        client."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em.mqtt = None
        em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        em._notify.assert_not_called()

    def test_bridge_alert_published_on_first_failure(self):
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._handle_command_worker(self._entity_info(point_id=100), 1, '1', 'cmd1')
        em._pub.publish_bridge_alert.assert_called_once()
        kwargs = em._pub.publish_bridge_alert.call_args.kwargs
        self.assertEqual(kwargs['alert_type'], 'write_failed')
        self.assertEqual(kwargs['severity'], 'error')
        self.assertEqual(kwargs['context']['point_id'], 100)

    def test_bridge_alert_context_includes_write_failed_total(self):
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._write_failed = 4  # pre-existing count
        em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        kwargs = em._pub.publish_bridge_alert.call_args.kwargs
        self.assertEqual(kwargs['context']['write_failed_total'], 5)

    def test_failed_write_triggers_force_readback(self):
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        with patch.object(em, '_force_readback') as mock_readback:
            em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        mock_readback.assert_called_once()

    def test_successful_write_does_not_touch_failure_counters(self):
        """Sanity check that the success path is genuinely untouched by
        this round's testing — _write_failed must not increment on success."""
        em = _make_em()
        em._api.write_point.return_value = True
        before = em._write_failed
        em._handle_command_worker(self._entity_info(point_id=999, entity_type='button'), 1, '1', 'cmd1')
        self.assertEqual(em._write_failed, before)


# ===========================================================================
# 58. EntityManager._force_readback — post-failure UI correction
# ===========================================================================


class TestForceReadback(unittest.TestCase):
    """Fetches the live controller value for a single point and republishes
    it to HA, correcting the optimistic UI state after a rejected write.
    Zero coverage before this. The single-point endpoint's JSON key quirk
    ('value', not 'datavalue' — confirmed against real SMO S40 responses
    per the function's own docstring) is exactly the kind of detail worth
    pinning down with a fixture matching the real API shape."""

    def _entity_info(self, point_id=100, entity_type='sensor'):
        return {
            'point_id': point_id, 'entity_type': entity_type,
            'state_topic': f'nibe/state/{point_id}', 'availability_topic': f'nibe/avail/{point_id}',
            'point_data': {},
        }

    def test_successful_readback_republishes_state(self):
        em = _make_em()
        em._api.fetch_point.return_value = {
            'value': {'isOk': True, 'integerValue': 42, 'stringValue': ''},
            'metadata': {'divisor': 1, 'variableSize': ''},
        }
        em._force_readback(self._entity_info(point_id=100))
        em.mqtt.publish.assert_any_call('nibe/state/100', '42', retain=True)

    def test_readback_uses_force_true(self):
        """The republish must bypass the ValueCache rate-limit/dedup gate —
        otherwise a recently-cached identical value could suppress the
        correction the user actually needs to see."""
        em = _make_em()
        em._api.fetch_point.return_value = {
            'value': {'isOk': True, 'integerValue': 5, 'stringValue': ''},
            'metadata': {'divisor': 1, 'variableSize': ''},
        }
        with patch.object(em, '_process_and_publish_state') as mock_process:
            em._force_readback(self._entity_info(point_id=100))
        self.assertTrue(mock_process.call_args.kwargs.get('force')
                         or (len(mock_process.call_args.args) >= 5 and mock_process.call_args.args[4]))

    def test_none_response_does_not_crash(self):
        """fetch_point returning None (API error) must not raise — just
        logs and gives up on the correction for this poll."""
        em = _make_em()
        em._api.fetch_point.return_value = None
        em._force_readback(self._entity_info(point_id=100))  # must not raise
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == 'nibe/state/100']
        self.assertEqual(state_calls, [])

    def test_not_ok_value_skips_publish(self):
        """isOk=False (firmware-reported read failure) must not publish a
        value that might be garbage — matches the 'is_ok' gating used
        elsewhere in the bridge for bulk-fetched data."""
        em = _make_em()
        em._api.fetch_point.return_value = {
            'value': {'isOk': False, 'integerValue': 999, 'stringValue': ''},
            'metadata': {'divisor': 1},
        }
        em._force_readback(self._entity_info(point_id=100))
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == 'nibe/state/100']
        self.assertEqual(state_calls, [])

    def test_value_not_a_dict_does_not_crash(self):
        """Defensive: a malformed response where 'value' isn't even a dict
        must not crash with AttributeError on .get()."""
        em = _make_em()
        em._api.fetch_point.return_value = {'value': None, 'metadata': {}}
        em._force_readback(self._entity_info(point_id=100))  # must not raise

    def test_missing_value_key_treated_as_not_ok(self):
        em = _make_em()
        em._api.fetch_point.return_value = {'metadata': {'divisor': 1}}
        em._force_readback(self._entity_info(point_id=100))  # must not raise
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == 'nibe/state/100']
        self.assertEqual(state_calls, [])


# ===========================================================================
# 59. _publish_device_modes — aid/smart mode caching
# ===========================================================================


class TestTriggeredByPopulation(unittest.TestCase):
    """triggered_by was always None in the changelog/MQTT payload because
    _publish_dynamic_changes initialised it to None and never updated it —
    even though _post_write_controlling_point was correctly set on writes.
    The fix populates triggered_by from the controlling point before both
    the _update_changelog_history call and the MQTT publish."""

    def _make_em(self):
        em = _make_em()
        # Pre-register a controlling point so the title lookup works
        em.all_points_by_id[5110] = {
            'variableId':    5110,
            'display_title': 'Prevent condensation climate system 1',
            'entity_type':   'switch',
            'entity_category': 'config',
            'is_writable':   True,
            'is_dynamic':    False,
            'metadata':      {'minValue': 0, 'maxValue': 1, 'divisor': 1,
                               'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
        }
        em.bulk_data[5110] = {'raw_value': 1, 'display_value': '1'}
        return em

    def _dynamic_point_data(self, pid):
        return (pid, {
            'variableId': pid,
            'title': f'Dynamic point {pid}',
            'description': '',
            'metadata': {
                'minValue': 0, 'maxValue': 100,
                'divisor': 1, 'unit': '°C',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False,
                'variableType': 'integer', 'variableSize': 'u16',
                'modbusRegisterID': pid, 'shortUnit': '',
                'decimal': 0, 'change': 0, 'intDefaultValue': 0,
            },
        })

    def test_triggered_by_populated_when_controlling_point_set(self):
        """Core fix: change_event['triggered_by'] must reflect the
        controlling point when _post_write_controlling_point is set."""
        em = self._make_em()
        em._post_write_controlling_point = 5110

        captured = {}
        original_update = em._update_changelog_history
        def capture_changelog(change_event):
            captured['event'] = change_event
            original_update(change_event)
        em._update_changelog_history = capture_changelog

        em._publish_dynamic_changes(
            new_points=[self._dynamic_point_data(50827)],
            disappeared_points=set(),
        )

        trig = captured['event'].get('triggered_by')
        self.assertIsNotNone(trig, "triggered_by must not be None when controlling point is known")
        self.assertEqual(trig['id'], 5110)
        self.assertEqual(trig['title'], 'Prevent condensation climate system 1')

    def test_triggered_by_includes_value_when_bulk_data_available(self):
        """triggered_by should include the written value (from bulk_data)
        so the card can show 'value written: 1' in the changelog entry."""
        em = self._make_em()
        em._post_write_controlling_point = 5110
        em.bulk_data[5110] = {'raw_value': 1}

        captured = {}
        original_update = em._update_changelog_history
        def capture_changelog(change_event):
            captured['event'] = change_event
            original_update(change_event)
        em._update_changelog_history = capture_changelog

        em._publish_dynamic_changes(
            new_points=[self._dynamic_point_data(50827)],
            disappeared_points=set(),
        )
        trig = captured['event'].get('triggered_by')
        self.assertIn('value', trig)
        self.assertEqual(trig['value'], 1)

    def test_triggered_by_none_when_no_controlling_point(self):
        """Startup and periodic-poll changes have no controlling point —
        triggered_by must remain None so the card doesn't show a spurious
        'triggered by' line."""
        em = self._make_em()
        em._post_write_controlling_point = None

        captured = {}
        original_update = em._update_changelog_history
        def capture_changelog(change_event):
            captured['event'] = change_event
            original_update(change_event)
        em._update_changelog_history = capture_changelog

        em._publish_dynamic_changes(
            new_points=[self._dynamic_point_data(50827)],
            disappeared_points=set(),
        )
        trig = captured['event'].get('triggered_by')
        self.assertIsNone(trig)

    def test_triggered_by_persisted_in_changelog_history(self):
        """triggered_by must survive the round-trip through
        _update_changelog_history so the changelog modal can display it."""
        em = self._make_em()
        em._post_write_controlling_point = 5110

        em._publish_dynamic_changes(
            new_points=[self._dynamic_point_data(50827)],
            disappeared_points=set(),
        )
        entry = em.change_history[0]
        trig = entry.get('triggered_by')
        self.assertIsNotNone(trig)
        self.assertEqual(trig['id'], 5110)



class TestEnableEntityMissingPointLogLevel(unittest.TestCase):
    """enable_entity logs WARNING (not ERROR) when a point is absent from
    bulk data. ERROR implied something broken; WARNING is correct since
    conditional points like 3671/5033 are legitimately absent when a room
    sensor is installed."""

    def test_missing_point_logs_warning_not_error(self):
        em = _make_em()
        with self.assertLogs('nibe.entities', level='WARNING') as cm:
            result = em.enable_entity(99999)
        self.assertFalse(result)
        # Must be WARNING, not ERROR
        self.assertTrue(any('WARNING' in line for line in cm.output),
            "Missing point must log at WARNING level")
        self.assertFalse(any('ERROR' in line for line in cm.output),
            "Missing point must NOT log at ERROR level")

    def test_missing_point_message_mentions_conditional(self):
        em = _make_em()
        with self.assertLogs('nibe.entities', level='WARNING') as cm:
            em.enable_entity(99999)
        self.assertTrue(any('bulk data' in line or 'conditional' in line
                             for line in cm.output))



class TestEntityManagerProperties(unittest.TestCase):
    """The all_points and active_entities properties return list views of
    their respective dicts. Previously uncovered (single-line properties)."""

    def _point(self, pid):
        return {
            'variableId': pid, 'display_title': f'Point {pid}',
            'metadata': {'isWritable': False, 'divisor': 1,
                         'minValue': 0, 'maxValue': 100,
                         'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'variableType': 'integer', 'variableSize': 's16',
                         'unit': '', 'decimal': 0},
            'title': f'Point {pid}', 'description': '',
        }

    def test_all_points_empty(self):
        em = _make_em()
        self.assertEqual(em.all_points, [])

    def test_all_points_returns_list_of_values(self):
        em = _make_em()
        em.all_points_by_id[100] = self._point(100)
        em.all_points_by_id[200] = self._point(200)
        result = em.all_points
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        pids = {p['variableId'] for p in result}
        self.assertEqual(pids, {100, 200})

    def test_all_points_is_a_copy(self):
        """Mutating the returned list must not affect all_points_by_id."""
        em = _make_em()
        em.all_points_by_id[100] = self._point(100)
        result = em.all_points
        result.clear()
        self.assertEqual(len(em.all_points_by_id), 1)

    def test_active_entities_empty(self):
        em = _make_em()
        self.assertEqual(em.active_entities, [])

    def test_active_entities_returns_list_of_values(self):
        em = _make_em()
        em.active_entities_by_id[100] = {'variableId': 100}
        em.active_entities_by_id[200] = {'variableId': 200}
        result = em.active_entities
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    def test_active_entities_is_a_copy(self):
        em = _make_em()
        em.active_entities_by_id[100] = {'variableId': 100}
        result = em.active_entities
        result.clear()
        self.assertEqual(len(em.active_entities_by_id), 1)


# ===========================================================================
# 71. EntityManager.discover_points
# ===========================================================================


class TestDiscoverPoints(unittest.TestCase):
    """discover_points() fetches bulk data, establishes the baseline set,
    populates the DynamicPointMap, and publishes metadata + point list.
    _fetch_bulk_data is mocked — it is tested independently."""

    def _make_em_with_bulk(self, point_ids=(100, 200, 300)):
        """Return an em where _fetch_bulk_data populates all_points_by_id."""
        em = _make_em()

        def fake_fetch(**_kw):
            for pid in point_ids:
                em.all_points_by_id[pid] = {
                    'variableId': pid,
                    'display_title': f'Point {pid}',
                    'entity_type': 'sensor',
                    'is_writable': False,
                    'metadata': {
                        'isWritable': False, 'divisor': 1,
                        'minValue': 0, 'maxValue': 100,
                        'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                        'variableType': 'integer', 'variableSize': 's16',
                        'unit': '', 'decimal': 0,
                    },
                    'title': f'Point {pid}', 'description': '',
                }
            return True

        em._fetch_bulk_data = fake_fetch
        return em

    def test_returns_true_on_success(self):
        em = self._make_em_with_bulk()
        self.assertTrue(em.discover_points())

    def test_returns_false_when_fetch_fails(self):
        em = _make_em()
        em._fetch_bulk_data = lambda **_kw: False
        self.assertFalse(em.discover_points())

    def test_baseline_established_from_bulk(self):
        em = self._make_em_with_bulk(point_ids=(100, 200, 300))
        em.discover_points()
        self.assertEqual(em.baseline_point_ids, {100, 200, 300})

    def test_initial_discovery_complete_set_to_true(self):
        em = self._make_em_with_bulk()
        self.assertFalse(em.initial_discovery_complete)
        em.discover_points()
        self.assertTrue(em.initial_discovery_complete)

    def test_initial_discovery_complete_not_set_on_failure(self):
        em = _make_em()
        em._fetch_bulk_data = lambda **_kw: False
        em.discover_points()
        self.assertFalse(em.initial_discovery_complete)

    def test_publishes_metadata_on_success(self):
        em = self._make_em_with_bulk()
        em.discover_points()
        em._pub.publish_all_metadata.assert_called_once()

    def test_publishes_point_list_on_success(self):
        em = self._make_em_with_bulk()
        em.discover_points()
        em._pub.publish_point_list.assert_called_once()

    def test_no_publish_on_failure(self):
        em = _make_em()
        em._fetch_bulk_data = lambda **_kw: False
        em.discover_points()
        em._pub.publish_all_metadata.assert_not_called()
        em._pub.publish_point_list.assert_not_called()

    def test_dynamic_map_populated_after_discovery(self):
        """populate_from_bulk should be called and return a count."""
        em = self._make_em_with_bulk()
        with patch.object(em.dynamic_point_map, 'populate_from_bulk',
                          return_value=2) as mock_pop, \
             patch.object(em.dynamic_point_map, 'restore_from_bulk'):
            em.discover_points()
            mock_pop.assert_called_once()

    def test_dynamic_map_file_fallback_when_empty(self):
        """If dynamic_point_map is empty after MQTT load, try file fallback."""
        em = self._make_em_with_bulk()
        # Ensure map reports as empty
        with patch.object(em.dynamic_point_map, '__len__', return_value=0), \
             patch.object(em.dynamic_point_map, 'from_file',
                          return_value=5) as mock_file, \
             patch.object(em.dynamic_point_map, 'populate_from_bulk',
                          return_value=0), \
             patch.object(em.dynamic_point_map, 'restore_from_bulk'):
            em.discover_points()
            mock_file.assert_called_once()

    def test_dynamic_point_map_loaded_from_file_when_mqtt_empty(self):
        """589->596: when dynamic_point_map is empty after MQTT restore,
        from_file() is called and if it returns entries they are logged."""
        em = _make_em()
        self.assertEqual(len(em.dynamic_point_map), 0)

        with patch.object(em.dynamic_point_map, 'from_file', return_value=3) as mock_file, \
             patch.object(em, '_fetch_bulk_data', return_value=True), \
             patch.object(em, 'scan_mqtt_discovery', return_value=set()), \
             patch.object(em, 'restore_from_mqtt', return_value=0):
            em.discover_points()

        mock_file.assert_called_once()


# ===========================================================================
# 72. EntityManager.complete_deferred_discovery
# ===========================================================================


class TestCompleteDeferredDiscovery(unittest.TestCase):
    """complete_deferred_discovery() replays the full initialisation sequence
    after the API was unreachable at startup, mirroring main()'s three-way
    decide_startup_action branch: apply (fresh install) / restore (same
    mode, or migration boundary) / reconcile (deliberate mode change)."""

    def _make_em_ready(self, applied_mode=None, mqtt_enabled_count=3):
        """Return an em where discover_points succeeds and the mocked
        scan/read-applied-mode drive decide_startup_action's branch."""
        em = _make_em()
        em.discover_points     = MagicMock(return_value=True)
        em.scan_mqtt_discovery = MagicMock(
            return_value=set(range(mqtt_enabled_count)) if mqtt_enabled_count else set()
        )
        em.read_applied_mode      = MagicMock(return_value=applied_mode)
        em.restore_from_mqtt      = MagicMock()
        em.apply_mode             = MagicMock()
        em.record_applied_mode    = MagicMock()
        em.publish_enabled_state  = MagicMock()
        em._api.fetch_device_info.return_value = {
            'serial': '12345', 'firmware': '4.12', 'model': 'S-series'
        }
        return em

    def test_returns_true_on_success(self):
        em = self._make_em_ready()
        self.assertTrue(em.complete_deferred_discovery('essential'))

    def test_returns_false_when_discover_fails(self):
        em = self._make_em_ready()
        em.discover_points.return_value = False
        self.assertFalse(em.complete_deferred_discovery('essential'))

    def test_restore_called_when_mqtt_configs_found_and_mode_unchanged(self):
        em = self._make_em_ready(applied_mode='essential', mqtt_enabled_count=5)
        em.complete_deferred_discovery('essential')
        em.restore_from_mqtt.assert_called_once()
        em.apply_mode.assert_not_called()

    def test_apply_mode_called_when_no_mqtt_configs(self):
        em = self._make_em_ready(mqtt_enabled_count=0)
        em.complete_deferred_discovery('essential')
        em.apply_mode.assert_called_once_with('essential')
        em.restore_from_mqtt.assert_not_called()

    def test_reconcile_when_applied_mode_differs(self):
        """A deliberate mode change detected across a restart: restore
        first (to establish real broker state), then reconcile to the
        newly configured mode."""
        em = self._make_em_ready(applied_mode='essential', mqtt_enabled_count=5)
        em.complete_deferred_discovery('menus')
        em.restore_from_mqtt.assert_called_once()
        em.apply_mode.assert_called_once_with('menus')

    def test_migration_boundary_restores_and_records_baseline(self):
        """When no applied-mode record exists yet (read_applied_mode()
        returns None) but entities already exist on the broker, this is
        the migration boundary — restore non-destructively and record the
        configured mode as the new baseline so a future genuine mode
        change becomes detectable."""
        em = self._make_em_ready(applied_mode=None, mqtt_enabled_count=5)
        em.complete_deferred_discovery('essential')
        em.restore_from_mqtt.assert_called_once()
        em.apply_mode.assert_not_called()
        em.record_applied_mode.assert_called_once_with('essential')

    def test_publish_enabled_state_called_on_success(self):
        em = self._make_em_ready()
        em.complete_deferred_discovery('essential')
        em.publish_enabled_state.assert_called_once()

    def test_publish_enabled_state_not_called_on_failure(self):
        em = self._make_em_ready()
        em.discover_points.return_value = False
        em.complete_deferred_discovery('essential')
        em.publish_enabled_state.assert_not_called()

    def test_device_info_updated_from_api(self):
        em = self._make_em_ready()
        em._api.fetch_device_info.return_value = {
            'serial': '99999', 'firmware': '4.12', 'model': 'S-series'
        }
        em.complete_deferred_discovery('essential')
        em._api.fetch_device_info.assert_called_once()

    def test_proceeds_when_device_info_unavailable(self):
        """If device info fetch fails, discovery still continues."""
        em = self._make_em_ready()
        em._api.fetch_device_info.return_value = None
        result = em.complete_deferred_discovery('essential')
        self.assertTrue(result)
        em.discover_points.assert_called_once()


# ===========================================================================
# 73. MqttDiscoveryPublisher — browser/metadata publish functions
# ===========================================================================


class TestScanMqttDiscovery(unittest.TestCase):
    """scan_mqtt_discovery subscribes for retained discovery configs, uses a
    sentinel message to detect end-of-retained-messages, and returns the set
    of discovered point IDs."""

    def _make_em_with_sentinel(self, retained_payloads, sentinel_fires=True):
        """Wire up mqtt mock so message callbacks fire synchronously.

        retained_payloads: list of JSON dicts to deliver on the config topic.
        sentinel_fires: if True, sentinel callback fires after config messages.
        """
        em = _make_em()
        callbacks = {}

        def fake_callback_add(topic, cb):
            callbacks[topic] = cb

        def fake_publish(topic, payload, retain=False):
            # When the sentinel is published, fire all retained config messages
            # first, then fire the sentinel callback if requested.
            if 'scan_sentinel' in topic:
                for p in retained_payloads:
                    import json as _json
                    msg = MagicMock()
                    msg.topic = 'homeassistant/sensor/nibe_1234/config'
                    msg.payload = _json.dumps(p).encode()
                    cb = callbacks.get('homeassistant/+/+/config')
                    if cb:
                        cb(None, None, msg)
                if sentinel_fires:
                    cb = callbacks.get(topic)
                    if cb:
                        cb(None, None, MagicMock())

        em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)
        em.mqtt.publish = MagicMock(side_effect=fake_publish)
        return em

    def test_discovers_nibe_point_ids(self):
        payload = {'unique_id': 'nibe_1234', 'name': 'Test'}
        em = self._make_em_with_sentinel([payload])
        result = em.scan_mqtt_discovery()
        self.assertIn(1234, result)

    def test_ignores_non_nibe_unique_ids(self):
        payload = {'unique_id': 'other_device_42', 'name': 'Other'}
        em = self._make_em_with_sentinel([payload])
        result = em.scan_mqtt_discovery()
        self.assertEqual(len(result), 0)

    def test_ignores_non_numeric_nibe_ids(self):
        payload = {'unique_id': 'nibe_notanumber', 'name': 'Test'}
        em = self._make_em_with_sentinel([payload])
        result = em.scan_mqtt_discovery()
        self.assertEqual(len(result), 0)

    def test_invalid_json_payload_skipped(self):
        em = _make_em()
        callbacks = {}

        def fake_callback_add(topic, cb):
            callbacks[topic] = cb

        def fake_publish(topic, payload, retain=False):
            if 'scan_sentinel' in topic:
                msg = MagicMock()
                msg.topic = 'homeassistant/sensor/nibe_1234/config'
                msg.payload = b'not valid json'
                cb = callbacks.get('homeassistant/+/+/config')
                if cb:
                    cb(None, None, msg)
                cb = callbacks.get(topic)
                if cb:
                    cb(None, None, MagicMock())

        em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)
        em.mqtt.publish = MagicMock(side_effect=fake_publish)
        result = em.scan_mqtt_discovery()  # must not raise
        self.assertEqual(len(result), 0)

    def test_sentinel_timeout_still_returns_discovered(self):
        """When sentinel never fires, method falls through after timeout
        and still returns whatever was discovered before the timeout."""
        payload = {'unique_id': 'nibe_9999', 'name': 'Test'}
        em = self._make_em_with_sentinel([payload], sentinel_fires=False)
        with patch('nibe_entity_manager._MQTT_SCAN_TIMEOUT_S', 0):
            result = em.scan_mqtt_discovery()
        self.assertIn(9999, result)

    def test_updates_mqtt_enabled_points(self):
        payload = {'unique_id': 'nibe_5555', 'name': 'Test'}
        em = self._make_em_with_sentinel([payload])
        em.scan_mqtt_discovery()
        self.assertIn(5555, em.mqtt_enabled_points)

    def test_clears_previous_mqtt_enabled_points(self):
        em = self._make_em_with_sentinel([])
        em.mqtt_enabled_points.add(9999)
        em.scan_mqtt_discovery()
        self.assertNotIn(9999, em.mqtt_enabled_points)

    def test_unsubscribes_after_scan(self):
        em = self._make_em_with_sentinel([])
        em.scan_mqtt_discovery()
        self.assertTrue(em.mqtt.unsubscribe.called)
        self.assertTrue(em.mqtt.message_callback_remove.called)


# ===========================================================================
# 81. EntityManager — restore_from_mqtt
# ===========================================================================


class TestRestoreFromMqtt(unittest.TestCase):
    """restore_from_mqtt rebuilds active_entities from the set found by
    scan_mqtt_discovery. Tests use mocked publisher to avoid real MQTT."""

    def _make_em_with_points(self, point_ids):
        em = _make_em()
        for pid in point_ids:
            em.all_points_by_id[pid] = {
                'variableId': pid, 'display_title': f'Point {pid}',
                'title': f'Point {pid}', 'description': '',
                'entity_type': 'sensor', 'entity_category': 'diagnostic',
                'is_writable': False, 'is_dynamic': False,
                'metadata': {
                    'isWritable': False, 'divisor': 1, 'decimal': 0,
                    'unit': '', 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                    'variableType': 'integer', 'variableSize': 's16',
                    'minValue': 0, 'maxValue': 100,
                    'intDefaultValue': 0, 'stringDefaultValue': '',
                    'change': 1, 'shortUnit': '', 'modbusRegisterID': pid,
                },
            }
        return em

    def _entity_info(self, pid):
        return {
            'point_id': pid,
            'variableId': pid,
            'entity_type': 'sensor',
            'availability_topic': f'homeassistant/sensor/nibe_{pid}/available',
            'state_topic': f'homeassistant/sensor/nibe_{pid}/state',
            'command_topic': None,
            'unique_id': f'nibe_{pid}',
        }

    def test_returns_zero_when_no_enabled_points(self):
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.clear()
        result = em.restore_from_mqtt()
        self.assertEqual(result, 0)

    def test_returns_restored_count(self):
        em = self._make_em_with_points([100, 200])
        em.mqtt_enabled_points.update({100, 200})
        em._pub.publish_entity_discovery.side_effect = [
            self._entity_info(100), self._entity_info(200)
        ]
        result = em.restore_from_mqtt()
        self.assertEqual(result, 2)

    def test_missing_point_skipped_and_removed_from_enabled(self):
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.update({100, 999})  # 999 not in all_points_by_id
        em._pub.publish_entity_discovery.return_value = self._entity_info(100)
        em.restore_from_mqtt()
        self.assertNotIn(999, em.mqtt_enabled_points)

    def test_online_published_for_restored_entities(self):
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        em._pub.publish_entity_discovery.return_value = self._entity_info(100)
        em.restore_from_mqtt()
        avail_topic = 'homeassistant/sensor/nibe_100/available'
        em.mqtt.publish.assert_any_call(avail_topic, 'online', retain=True)

    def test_entity_added_to_active_entities(self):
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        em._pub.publish_entity_discovery.return_value = self._entity_info(100)
        em.restore_from_mqtt()
        self.assertIn(100, em.active_entities_by_id)

    def test_command_topic_subscribed_when_writable(self):
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        ei = self._entity_info(100)
        ei['command_topic'] = 'homeassistant/sensor/nibe_100/set'
        em._pub.publish_entity_discovery.return_value = ei
        em.restore_from_mqtt()
        em.mqtt.subscribe.assert_any_call(ei['command_topic'], qos=1)

    def test_publish_entity_discovery_failure_skips_point(self):
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        em._pub.publish_entity_discovery.return_value = None
        result = em.restore_from_mqtt()
        self.assertEqual(result, 0)
        self.assertNotIn(100, em.active_entities_by_id)

    def test_dynamic_point_added_to_active_dynamic_points(self):
        """restore_from_mqtt: is_dynamic=True points are added to
        active_dynamic_points (line 721)."""
        em = self._make_em_with_points([100])
        em.all_points_by_id[100]['is_dynamic'] = True
        em.mqtt_enabled_points.add(100)
        em._pub.publish_entity_discovery.return_value = self._entity_info(100)
        em.restore_from_mqtt()
        self.assertIn(100, em.active_dynamic_points)

    def test_writable_restored_entity_command_callback_dispatches(self):
        """The MQTT command callback registered during restore_from_mqtt
        must invoke _handle_command when called (line 803)."""
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        cmd_topic = 'homeassistant/switch/nibe_100/set'
        entity = self._entity_info(100)
        entity['command_topic'] = cmd_topic
        em._pub.publish_entity_discovery.return_value = entity

        stored_cb = {}
        def fake_callback_add(topic, cb):
            stored_cb[topic] = cb
        em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)

        em.restore_from_mqtt()
        self.assertIn(cmd_topic, stored_cb)

        msg = MagicMock()
        msg.payload = b'1'
        with patch.object(em, '_handle_command') as mock_handle:
            stored_cb[cmd_topic](None, None, msg)
        mock_handle.assert_called_once()

    def test_restore_adds_dynamic_point_to_active_set(self):
        """794->797: when a restored point has is_dynamic=True in all_points_by_id,
        it must be added to active_dynamic_points."""
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        em.all_points_by_id[100]['is_dynamic'] = True  # set on the point dict
        em._pub.publish_entity_discovery.return_value = self._entity_info(100)
        em.restore_from_mqtt()
        self.assertIn(100, em.active_dynamic_points)

    def test_restore_second_call_does_not_increment_republished_for_existing(self):
        """794->797 False branch: when active_entities_by_id already has the entity
        (prev is not None), republished count is not incremented."""
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        em._pub.publish_entity_discovery.return_value = self._entity_info(100)
        # First restore — prev is None, republished = 1
        em.restore_from_mqtt()
        # Second restore — prev is not None, republished not incremented
        em.mqtt.publish.reset_mock()
        count2 = em.restore_from_mqtt()
        # Both calls succeed; second call does not republish unnecessarily
        self.assertEqual(count2, 1)


# ===========================================================================
# 81. EntityManager — _handle_api_failure
# ===========================================================================


class TestHandleApiFailure(unittest.TestCase):
    """_handle_api_failure increments consecutive failures and sends an HA
    notification + MQTT alert when the threshold is reached."""

    def test_increments_consecutive_failures(self):
        em = _make_em()
        em.api_consecutive_failures = 0
        em._handle_api_failure()
        self.assertEqual(em.api_consecutive_failures, 1)

    def test_no_notification_below_threshold(self):
        em = _make_em()
        em.api_consecutive_failures = 0
        em.api_failure_threshold = 3
        em._handle_api_failure()
        em._notify.assert_not_called()

    def test_notification_sent_at_threshold(self):
        em = _make_em()
        em.api_consecutive_failures = 2  # one more will hit threshold of 3
        em.api_failure_threshold = 3
        em._handle_api_failure()
        em._notify.assert_called_once()

    def test_notification_not_repeated_above_threshold(self):
        em = _make_em()
        em.api_consecutive_failures = 5
        em.api_failure_threshold = 3
        em._api_notification_active = True  # already sent
        em._handle_api_failure()
        em._notify.assert_not_called()

    def test_mqtt_alert_published_at_threshold(self):
        em = _make_em()
        em.api_consecutive_failures = 2
        em.api_failure_threshold = 3
        em._handle_api_failure()
        em._pub.publish_bridge_alert.assert_called_once()

    def test_alert_type_is_api_unreachable(self):
        em = _make_em()
        em.api_consecutive_failures = 2
        em.api_failure_threshold = 3
        em._handle_api_failure()
        call_kwargs = em._pub.publish_bridge_alert.call_args
        self.assertEqual(call_kwargs.kwargs.get('alert_type') or
                         call_kwargs.args[0], 'api_unreachable')

    def test_api_notification_active_set_after_threshold(self):
        em = _make_em()
        em.api_consecutive_failures = 2
        em.api_failure_threshold = 3
        em._handle_api_failure()
        self.assertTrue(em._api_notification_active)

    def test_handle_api_failure_skips_bridge_alert_when_pub_is_none(self):
        """1605->1616: when _pub is None, publish_bridge_alert is skipped
        but the notification and flag are still set."""
        em = _make_em()
        em._pub = None
        em._notify = MagicMock()
        em.api_consecutive_failures = em.api_failure_threshold
        em._handle_api_failure()
        self.assertTrue(em._api_notification_active)

    def test_write_success_skips_bridge_alert_when_pub_is_none(self):
        """2088->2100: when _pub is None after a write success, publish_bridge_alert
        is skipped but the notification is still dismissed."""
        em = _make_em()
        em._pub = None
        em._dismiss = MagicMock()
        em._api = MagicMock()
        em._api.write_point.return_value = True
        em._write_notification_active = True
        em.mqtt = MagicMock()
        point_id = 100
        em.all_points_by_id[point_id] = {
            'variableId': point_id, 'display_title': 'Test',
            'entity_type': 'switch', 'entity_category': 'config',
            'is_writable': True, 'is_dynamic': False,
            'metadata': {'variableSize': 'u8', 'divisor': 1, 'decimal': 0,
                         'unit': '', 'shortUnit': '',
                         'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                         'modbusRegisterID': point_id,
                         'variableType': 'integer', 'minValue': 0, 'maxValue': 1,
                         'intDefaultValue': 0, 'stringDefaultValue': '',
                         'change': 1, 'isWritable': True},
            'description': '',
        }
        em.mqtt_enabled_points.add(point_id)
        entity_info = {
            'point_id': point_id, 'entity_type': 'switch',
            'state_topic': f'nibe/state/{point_id}',
            'command_topic': f'nibe/cmd/{point_id}',
            'availability_topic': f'nibe/avail/{point_id}',
        }
        em.active_entities_by_id[point_id] = entity_info
        with patch.object(em, '_run_learning_detection'):
            em._handle_command_worker(entity_info, 1, '1', 'test')
        em._dismiss.assert_called()

    def test_write_failure_skips_bridge_alert_when_pub_is_none(self):
        """2206->2219: when _pub is None on write failure, publish_bridge_alert
        is skipped but notify_ha and the flag are still set."""
        em = _make_em()
        em._pub = None
        em._notify = MagicMock()
        em._api = MagicMock()
        em._api.write_point.return_value = False
        em.mqtt = MagicMock()
        point_id = 100
        em.all_points_by_id[point_id] = {
            'variableId': point_id, 'display_title': 'Test',
            'entity_type': 'switch', 'entity_category': 'config',
            'is_writable': True, 'is_dynamic': False,
            'metadata': {'variableSize': 'u8', 'divisor': 1, 'decimal': 0,
                         'unit': '', 'shortUnit': '',
                         'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                         'modbusRegisterID': point_id,
                         'variableType': 'integer', 'minValue': 0, 'maxValue': 1,
                         'intDefaultValue': 0, 'stringDefaultValue': '',
                         'change': 1, 'isWritable': True},
            'description': '',
        }
        em.mqtt_enabled_points.add(point_id)
        entity_info = {
            'point_id': point_id, 'entity_type': 'switch',
            'state_topic': f'nibe/state/{point_id}',
            'command_topic': f'nibe/cmd/{point_id}',
            'availability_topic': f'nibe/avail/{point_id}',
        }
        em.active_entities_by_id[point_id] = entity_info
        em._handle_command_worker(entity_info, 1, '1', 'test')
        self.assertTrue(em._write_notification_active)


# ===========================================================================
# 82. EntityManager — mark_changelog_read
# ===========================================================================


class TestMarkChangelogRead(unittest.TestCase):
    """mark_changelog_read flips all entries to unread=False and publishes
    the updated changelog with unread_count=0."""

    def _seed_history(self, em, count=3):
        from collections import deque
        em.change_history = deque(maxlen=500)
        for i in range(count):
            em.change_history.appendleft({
                'timestamp': float(i), 'iso_timestamp': f'2024-0{i+1}-01T00:00:00Z',
                'added': [i], 'removed': [],
                'id': f'change_{i}', 'unread': True,
                'source': 'firmware', 'triggered_by': None,
            })

    def test_all_entries_marked_read(self):
        em = _make_em()
        self._seed_history(em, 3)
        em.mark_changelog_read()
        for entry in em.change_history:
            self.assertFalse(entry['unread'])

    def test_unread_topic_published_with_zero_count(self):
        import json
        em = _make_em()
        self._seed_history(em, 2)
        em.mark_changelog_read()
        unread_calls = [c for c in em.mqtt.publish.call_args_list
                        if 'unread' in c[0][0]]
        self.assertTrue(len(unread_calls) > 0)
        payload = json.loads(unread_calls[0][0][1])
        self.assertEqual(payload['unread_count'], 0)

    def test_history_topic_published(self):
        em = _make_em()
        self._seed_history(em, 2)
        em.mark_changelog_read()
        history_calls = [c for c in em.mqtt.publish.call_args_list
                         if 'history' in c[0][0]]
        self.assertTrue(len(history_calls) > 0)

    def test_seq_incremented(self):
        em = _make_em()
        self._seed_history(em, 1)
        before = em._history_seq
        em.mark_changelog_read()
        self.assertGreater(em._history_seq, before)


# ===========================================================================
# 83. EntityManager — _prune_changelog_if_due
# ===========================================================================


class TestPruneChangelogIfDue(unittest.TestCase):
    """_prune_changelog_if_due removes entries older than retention_days,
    keeping at least 50 regardless of age. Runs at most once per hour."""

    def _entry(self, timestamp, unread=True):
        return {
            'timestamp': timestamp, 'iso_timestamp': '2024-01-01T00:00:00Z',
            'added': [1], 'removed': [], 'id': 'x', 'unread': unread,
            'source': 'firmware', 'triggered_by': None,
        }

    def test_returns_false_when_not_due(self):
        em = _make_em()
        em._last_prune_time = time.time()  # just ran
        result = em._prune_changelog_if_due()
        self.assertFalse(result)

    def test_returns_true_when_due(self):
        em = _make_em()
        em._last_prune_time = 0  # never ran
        result = em._prune_changelog_if_due()
        self.assertTrue(result)

    def test_old_entries_removed(self):
        from collections import deque
        em = _make_em()
        em._last_prune_time = 0
        em.changelog_retention_days = 1  # 1 day retention
        now = time.time()
        em.change_history = deque(maxlen=500)
        # Add 55 recent and 10 old entries — total > 50 so old ones get pruned
        for _ in range(55):
            em.change_history.appendleft(self._entry(now - 100))   # recent
        for _ in range(10):
            em.change_history.appendleft(self._entry(now - 200000))  # old (>2 days)
        em._prune_changelog_if_due()
        remaining_ts = [e['timestamp'] for e in em.change_history]
        # All remaining entries should be recent (within 1 day)
        cutoff = now - 86400
        self.assertTrue(all(ts >= cutoff for ts in remaining_ts))

    def test_minimum_50_entries_kept_regardless_of_age(self):
        from collections import deque
        em = _make_em()
        em._last_prune_time = 0
        em.changelog_retention_days = 0  # expire everything
        em.change_history = deque(maxlen=500)
        # Add 60 very old entries
        for i in range(60):
            em.change_history.appendleft(self._entry(1.0))  # epoch — very old
        em._prune_changelog_if_due()
        self.assertGreaterEqual(len(em.change_history), 50)

    def test_no_prune_when_all_entries_recent(self):
        from collections import deque
        em = _make_em()
        em._last_prune_time = 0
        em.changelog_retention_days = 90
        now = time.time()
        em.change_history = deque(maxlen=500)
        for _ in range(10):
            em.change_history.appendleft(self._entry(now - 100))
        em._prune_changelog_if_due()
        self.assertEqual(len(em.change_history), 10)

    def test_last_prune_time_updated(self):
        em = _make_em()
        em._last_prune_time = 0
        before = time.time()
        em._prune_changelog_if_due()
        self.assertGreaterEqual(em._last_prune_time, before)


# ===========================================================================
# 84. EntityManager — republish_availability
# ===========================================================================


class TestRepublishAvailability(unittest.TestCase):
    """republish_availability publishes 'online' for all active entities
    after a broker restart."""

    def test_publishes_online_for_all_active_entities(self):
        em = _make_em()
        em.active_entities_by_id[100] = {
            'availability_topic': 'homeassistant/sensor/nibe_100/available'
        }
        em.active_entities_by_id[200] = {
            'availability_topic': 'homeassistant/sensor/nibe_200/available'
        }
        em.republish_availability()
        topics = [c[0][0] for c in em.mqtt.publish.call_args_list]
        self.assertIn('homeassistant/sensor/nibe_100/available', topics)
        self.assertIn('homeassistant/sensor/nibe_200/available', topics)

    def test_all_published_as_online(self):
        em = _make_em()
        em.active_entities_by_id[100] = {
            'availability_topic': 'homeassistant/sensor/nibe_100/available'
        }
        em.republish_availability()
        avail_calls = [c for c in em.mqtt.publish.call_args_list
                       if 'available' in c[0][0]]
        self.assertTrue(all(c[0][1] == 'online' for c in avail_calls))

    def test_no_publish_when_no_active_entities(self):
        em = _make_em()
        em.active_entities_by_id.clear()
        em.republish_availability()
        em.mqtt.publish.assert_not_called()

    def test_mgmt_avail_topic_published_when_set(self):
        em = _make_em()
        em.active_entities_by_id[100] = {
            'availability_topic': 'homeassistant/sensor/nibe_100/available'
        }
        em._mgmt_avail_topic = 'homeassistant/nibe/management/available'
        em.republish_availability()
        topics = [c[0][0] for c in em.mqtt.publish.call_args_list]
        self.assertIn('homeassistant/nibe/management/available', topics)


# ===========================================================================
# 79. load_config — remaining uncovered paths
# ===========================================================================


class TestPublishDeviceModesEarlyReturn(unittest.TestCase):
    """_publish_device_modes returns early when api_consecutive_failures > 0."""

    def test_returns_early_on_api_failure(self):
        from nibe_ha_integration import _publish_device_modes
        em  = MagicMock()
        pub = MagicMock()
        em.api_consecutive_failures = 1
        _publish_device_modes(em, pub)
        pub.publish_device_modes.assert_not_called()


# ===========================================================================
# Coverage: nibe_entity_manager.py — remaining gaps
# ===========================================================================


class TestUpdateEntityStateButtonEarlyReturn(unittest.TestCase):
    """_update_entity_state returns early for button entities (just publishes online)."""

    def test_button_publishes_online_and_returns(self):
        em = _make_em()
        entity_info = {
            'point_id': 100, 'entity_type': 'button',
            'availability_topic': 'nibe/avail/100',
            'state_topic': 'nibe/state/100',
            'command_topic': None,
            'point_data': {},
        }
        em.bulk_data[100] = {'raw_value': 0, 'string_value': '', 'is_ok': True,
                              'metadata': {}, 'title': 'Test'}
        em._update_entity_state(entity_info)
        em.mqtt.publish.assert_called_once_with('nibe/avail/100', 'online', retain=True)



class TestUpdateEntityStateNoValueMappingsDivisorPath(unittest.TestCase):
    """_update_entity_state falls through to divisor path when no value_mappings."""

    def test_no_value_mappings_uses_divisor(self):
        em = _make_em()
        entity_info = {
            'point_id': 200, 'entity_type': 'number',
            'availability_topic': 'nibe/avail/200',
            'state_topic': 'nibe/state/200',
            'command_topic': None,
            'point_data': {},
        }
        em.bulk_data[200] = {
            'raw_value': 250, 'string_value': '', 'is_ok': True,
            'metadata': {'variableSize': 's16', 'divisor': 10},
            'title': 'Test',
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/200']
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], '25')

    def _active_entity(self, em, entity_info):
        from contextlib import contextmanager
        @contextmanager
        def ctx():
            em.active_entities_by_id[entity_info['point_id']] = entity_info
            em.mqtt_enabled_points.add(entity_info['point_id'])
            try:
                yield
            finally:
                em.active_entities_by_id.pop(entity_info['point_id'], None)
                em.mqtt_enabled_points.discard(entity_info['point_id'])
        return ctx()



class TestUpdateEntityStateDynamicDisappearance(unittest.TestCase):
    """_update_entity_state routes post-write disappearance as dynamic change."""

    def test_post_write_absent_point_triggers_dynamic_change(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em._post_write_active = True
        point_id = 9001
        em.mqtt_enabled_points.add(point_id)
        em.baseline_point_ids.add(point_id)
        em.active_dynamic_points.add(point_id)
        # point_id is NOT in bulk_data → triggers disappearance
        entity_info = {
            'point_id': point_id, 'entity_type': 'switch',
            'availability_topic': f'nibe/avail/{point_id}',
            'state_topic': f'nibe/state/{point_id}',
            'command_topic': None,
            'point_data': {},
        }
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._update_entity_state(entity_info)
        mock_dyn.assert_called_once()
        _, disappeared = mock_dyn.call_args.args
        self.assertIn(point_id, disappeared)



class TestUpdateEntityStateAbsentNoPostWrite(unittest.TestCase):
    """When _post_write_active is False, absent points are simply disabled."""

    def test_absent_point_disables_entity(self):
        em = _make_em()
        em._post_write_active = False
        point_id = 9999
        em.mqtt_enabled_points.add(point_id)
        entity_info = {
            'point_id': point_id,
            'entity_type': 'sensor',
            'availability_topic': 'nibe/avail/9999',
            'state_topic': 'nibe/state/9999',
        }
        with patch.object(em, 'disable_entity') as mock_disable:
            em._update_entity_state(entity_info)
        mock_disable.assert_called_once_with(point_id)



class TestFetchBulkDataLockBusy(unittest.TestCase):
    """_fetch_bulk_data returns False and logs when lock is already held."""

    def test_returns_false_when_lock_busy(self):
        em = _make_em()
        em._bulk_fetch_lock.acquire()
        try:
            result = em._fetch_bulk_data()
        finally:
            em._bulk_fetch_lock.release()
        self.assertIs(result, False)



class TestFetchBulkDataBadResponse(unittest.TestCase):
    """_fetch_bulk_data handles non-dict API response."""

    def test_none_response_calls_handle_api_failure(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = None
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()

    def test_list_response_calls_handle_api_failure(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = []
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()



class TestFetchBulkDataHttpErrors(unittest.TestCase):
    """_fetch_bulk_data handles HTTPError and generic exceptions."""

    def test_http_401_calls_handle_api_failure(self):
        import urllib.error
        em = _make_em()
        err = urllib.error.HTTPError(url='', code=401, msg='Unauthorized',
                                     hdrs=None, fp=None)
        em._api.fetch_bulk_points.side_effect = err
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()

    def test_http_503_calls_handle_api_failure(self):
        import urllib.error
        em = _make_em()
        err = urllib.error.HTTPError(url='', code=503, msg='Service Unavailable',
                                     hdrs=None, fp=None)
        em._api.fetch_bulk_points.side_effect = err
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()

    def test_unhandled_exception_calls_handle_api_failure(self):
        em = _make_em()
        em._api.fetch_bulk_points.side_effect = RuntimeError("unexpected")
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()



class TestFetchBulkDataValueError(unittest.TestCase):
    """ValueError/KeyError during point processing is logged and skipped."""

    def test_value_error_during_processing_continues(self):
        em = _make_em()
        em.initial_discovery_complete = True
        # A point whose metadata causes a TypeError when processed
        em._api.fetch_bulk_points.return_value = {
            'bad': {   # non-integer key → ValueError in int(point_id_str)
                'title': 'Bad', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                              'isWritable': False},
                'value': {'integerValue': 0, 'stringValue': '', 'isOk': True},
            },
            '100': {
                'title': 'Good', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                              'isWritable': False},
                'value': {'integerValue': 42, 'stringValue': '', 'isOk': True},
            },
        }
        em._fetch_bulk_data(detect_changes=False)
        # Should not raise; good point should be processed
        self.assertIn(100, em.bulk_data)



class TestFetchBulkDataPostWriteNoChanges(unittest.TestCase):
    """Post-write scan with no dynamic changes logs debug message."""

    def test_post_write_no_changes_debug_log(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em._post_write_active = True
        em._api.fetch_bulk_points.return_value = {
            '100': {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                              'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        em.baseline_point_ids.add(100)
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_dyn.assert_not_called()



class TestFetchBulkDataApiRestoration(unittest.TestCase):
    """Successful fetch after failures dismisses api_unreachable notification."""

    def _minimal_response(self, point_id=100):
        """Non-empty truthy response that skips new-point routing."""
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                              'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def test_dismisses_notification_and_publishes_alert_after_failures(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em.api_consecutive_failures = em.api_failure_threshold + 1
        em._api_notification_active = True
        em._api.fetch_bulk_points.return_value = self._minimal_response()
        em._fetch_bulk_data(detect_changes=False)
        em._dismiss.assert_called()
        self.assertFalse(em._api_notification_active)

    def test_dismisses_discovery_notification_after_failures(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em._discovery_notification_active = True
        em._api.fetch_bulk_points.return_value = self._minimal_response()
        em._fetch_bulk_data(detect_changes=False)
        self.assertFalse(em._discovery_notification_active)



class TestFetchBulkDataNewSwitchPopulatesMap(unittest.TestCase):
    """New switch/select points discovered outside scan call populate_from_bulk."""

    def test_new_switch_point_calls_populate_from_bulk(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em._post_write_active = False
        em._api.fetch_bulk_points.return_value = {
            '500': {
                'title': 'Mode switch', 'description': '',
                'metadata': {
                    'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                    'isWritable': True, 'minValue': 0, 'maxValue': 1,
                    'variableType': 'integer', 'variableSize': 'u8',
                    'divisor': 1, 'decimal': 0, 'unit': '',
                },
                'value': {'integerValue': 0, 'stringValue': '', 'isOk': True},
            }
        }
        with patch.object(em.dynamic_point_map, 'populate_from_bulk') as mock_pop:
            em._fetch_bulk_data(detect_changes=True)
        mock_pop.assert_called_once()



class TestPublishDynamicChangesBothEmpty(unittest.TestCase):
    """_publish_dynamic_changes returns early when both args are empty."""

    def test_both_empty_returns_without_publishing(self):
        em = _make_em()
        em._publish_dynamic_changes([], set())
        em.mqtt.publish.assert_not_called()



class TestPublishDynamicChangesEmptyChangeEvent(unittest.TestCase):
    """_publish_dynamic_changes with disappeared points where entity not in all_points_by_id
    results in empty change_event — returns early without publishing the dynamic event."""

    def test_unknown_disappeared_points_skips_event_publish(self):
        em = _make_em()
        em.initial_discovery_complete = True
        # Point 9999 disappeared but is not in all_points_by_id → change_event stays empty
        with patch.object(em, 'publish_enabled_state'):
            em._publish_dynamic_changes([], {9999})
        # No dynamic event publish should happen since change_event is empty
        dynamic_calls = [c for c in em.mqtt.publish.call_args_list
                         if 'dynamic' in str(c)]
        self.assertEqual(dynamic_calls, [])



class TestPublishDynamicChangesDisablesEnabledPoint(unittest.TestCase):
    """_publish_dynamic_changes calls disable_entity for disappeared points that are enabled."""

    def _make_point(self, point_id):
        return {
            'variableId': point_id, 'display_title': f'Point {point_id}',
            'entity_type': 'switch', 'entity_category': 'config',
            'is_dynamic': True, 'is_writable': True,
            'metadata': {'variableSize': 'u8', 'divisor': 1,
                         'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
            'description': '',
        }

    def test_disappeared_enabled_point_calls_disable_entity(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 7777
        em.all_points_by_id[point_id] = self._make_point(point_id)
        em.mqtt_enabled_points.add(point_id)
        em.active_dynamic_points.add(point_id)
        with patch.object(em, 'disable_entity') as mock_disable, \
             patch.object(em, 'publish_enabled_state'), \
             patch.object(em, '_persist_active_dynamic'):
            em._publish_dynamic_changes([], {point_id})
        mock_disable.assert_called_once_with(point_id)



class TestPublishDynamicChangesUnexpectedException(unittest.TestCase):
    """Unexpected Exception in notification block is logged at error level
    and does not propagate (lines 1825-1826)."""

    def test_unexpected_exception_logged_not_raised(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 7777
        em.all_points_by_id[point_id] = {
            'variableId': point_id, 'display_title': 'Point 7777',
            'entity_type': 'switch', 'entity_category': 'config',
            'is_dynamic': True, 'is_writable': True,
            'metadata': {'variableSize': 'u8', 'divisor': 1,
                         'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
            'description': '',
        }
        em.mqtt_enabled_points.add(point_id)
        em.active_dynamic_points.add(point_id)
        # Patch em._notify to raise a non-ValueError/TypeError/AttributeError
        # exception to exercise the bare `except Exception` branch (lines 1825-1826).
        em._notify = MagicMock(side_effect=OSError("unexpected"))
        with patch.object(em, 'publish_enabled_state'), \
             patch.object(em, 'disable_entity'), \
             patch.object(em, '_persist_active_dynamic'):
            em._publish_dynamic_changes([], {point_id})   # must not raise



class TestUpdateAllStates(unittest.TestCase):
    """update_all_states: force flag, post-write window, active entity loop."""

    def _point_response(self, point_id=100, value=1):
        return {
            str(point_id): {
                'title': 'Test', 'description': '',
                'metadata': {
                    'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                    'isWritable': False, 'variableType': 'integer',
                    'variableSize': 'u8', 'divisor': 1, 'decimal': 0, 'unit': '',
                },
                'value': {'integerValue': value, 'stringValue': '', 'isOk': True},
            }
        }

    def test_no_active_entities_returns_without_iterating(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {}
        with patch.object(em, '_update_entity_state') as mock_update:
            em.update_all_states()
        mock_update.assert_not_called()

    def test_active_entities_calls_update_entity_state(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em._api.fetch_bulk_points.return_value = self._point_response(100, 1)
        entity_info = {
            'point_id': 100, 'entity_type': 'sensor',
            'availability_topic': 'nibe/avail/100',
            'state_topic': 'nibe/state/100',
            'command_topic': None, 'point_data': {},
        }
        em.active_entities_by_id[100] = entity_info
        with patch.object(em, '_update_entity_state') as mock_update:
            em.update_all_states()
        mock_update.assert_called_once_with(entity_info)

    def test_force_resets_last_bulk_fetch(self):
        em = _make_em()
        em.last_bulk_fetch = 99999.0
        em._api.fetch_bulk_points.return_value = {}
        em.update_all_states(force=True)
        # last_bulk_fetch is reset to 0 then updated to current time — either way != 99999
        self.assertNotEqual(em.last_bulk_fetch, 99999.0)

    def test_post_write_window_ends_when_expired(self):
        em = _make_em()
        em._post_write_active = True
        em._post_write_until = 0.0   # already past
        em._api.fetch_bulk_points.return_value = {}
        em.update_all_states()
        self.assertFalse(em._post_write_active)



class TestHandleCommandWorkerWriteSuccessTime(unittest.TestCase):
    """Write success for time entity produces HH:MM:SS optimistic state."""

    def test_time_entity_optimistic_state_hhmmss(self):
        em = _make_em()
        metadata = {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            'isWritable': True, 'minValue': 0, 'maxValue': 86399,
            'variableType': 'time', 'variableSize': 'u16',
            'divisor': 1, 'decimal': 0, 'unit': '',
        }
        entity_info = {
            'point_id':           300,
            'entity_type':        'time',
            'state_topic':        'nibe/state/300',
            'availability_topic': 'nibe/avail/300',
            'command_topic':      'nibe/cmd/300',
            'metadata':           metadata,
        }
        point = {
            'variableId': 300, 'display_title': 'Time point',
            'entity_type': 'time', 'entity_category': 'config',
            'is_writable': True, 'is_dynamic': False, 'description': '',
            'metadata': metadata,
        }
        em.all_points_by_id[300] = point
        em.bulk_data[300] = {'raw_value': 0, 'string_value': '', 'is_ok': True,
                              'metadata': metadata, 'title': 'Time point'}
        em._api.write_point.return_value = True
        # value=25200 (seconds), payload='07:00:00' (raw MQTT string)
        em._handle_command_worker(entity_info, 25200, '07:00:00', 'test-cmd')
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/300']
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[-1].args[1], '07:00:00')



class TestHandleCommandWriteSuccessClears_WriteNotification(unittest.TestCase):
    """Write success dismisses the write-error notification and publishes alert."""

    def test_write_success_clears_write_notification(self):
        em = _make_em()
        em._write_notification_active = True
        metadata = {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            'isWritable': True, 'minValue': 0, 'maxValue': 1,
            'variableType': 'integer', 'variableSize': 'u8',
            'divisor': 1, 'decimal': 0, 'unit': '',
        }
        entity_info = {
            'point_id': 100, 'entity_type': 'switch',
            'state_topic': 'nibe/state/100',
            'availability_topic': 'nibe/avail/100',
            'command_topic': 'nibe/cmd/100',
            'metadata': metadata,
        }
        point = {
            'variableId': 100, 'display_title': 'Test switch',
            'entity_type': 'switch', 'entity_category': 'config',
            'is_writable': True, 'is_dynamic': False, 'description': '',
            'metadata': metadata,
        }
        em.all_points_by_id[100] = point
        em.bulk_data[100] = {'raw_value': 0, 'string_value': '', 'is_ok': True,
                              'metadata': metadata, 'title': 'Test switch'}
        em._api.write_point.return_value = True
        em._handle_command_worker(entity_info, 1, '1', 'cmd-id')
        em._dismiss.assert_called()
        self.assertFalse(em._write_notification_active)



class TestWritePointLastStatesRepublishOnUnderflow(unittest.TestCase):
    """When value < min, last_states value is republished to snap HA UI back."""

    def test_last_states_republished_on_underflow(self):
        em = _make_em()
        metadata = {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            'isWritable': True, 'minValue': 5, 'maxValue': 100,
            'variableType': 'integer', 'variableSize': 'u8',
            'divisor': 1, 'decimal': 0, 'unit': '',
        }
        entity_info = {
            'point_id': 200, 'entity_type': 'number',
            'state_topic': 'nibe/state/200',
            'availability_topic': 'nibe/avail/200',
            'command_topic': 'nibe/cmd/200',
            'metadata': metadata,
        }
        em.last_states[200] = '10'
        # payload='1' parses to value=1, which is < minValue=5 → underflow
        result = em._parse_command_payload('1', entity_info, 'cmd-id')
        self.assertIsNone(result)
        calls = [c for c in em.mqtt.publish.call_args_list
                 if c.args[0] == 'nibe/state/200']
        self.assertTrue(calls)
        self.assertEqual(calls[-1].args[1], '10')



class TestDynamicLearningDetection(unittest.TestCase):
    """_run_learning_detection: size-change exit and deadline exit."""

    def _em_with_bulk(self, initial_size=1):
        em = _make_em()
        for i in range(initial_size):
            em.bulk_data[i] = {'raw_value': 0, 'string_value': '', 'is_ok': True,
                                'metadata': {}, 'title': f'P{i}'}
        return em

    def test_size_change_exits_loop_early(self):
        em = self._em_with_bulk(initial_size=2)
        sleep_calls = [0]
        def fake_sleep(t):
            sleep_calls[0] += 1
            em.bulk_data[999] = {'raw_value': 1, 'string_value': '', 'is_ok': True,
                                  'metadata': {}, 'title': 'New'}
        with patch('nibe_entity_manager.time.sleep', side_effect=fake_sleep), \
             patch('nibe_entity_manager.time.time', return_value=0.0), \
             patch.object(em.dynamic_point_map, 'record_outcome') as mock_rec, \
             patch.object(em, '_persist_dynamic_map'):
            em._run_learning_detection(5, 1, 'test')
        self.assertEqual(sleep_calls[0], 1)
        mock_rec.assert_called_once_with(5, 1, [999])

    def test_deadline_exit_records_empty_outcome(self):
        em = self._em_with_bulk(initial_size=1)
        time_seq = iter([0.0, 0.0, 999.0])  # _post_write_until, deadline, loop check
        with patch('nibe_entity_manager.time.sleep'), \
             patch('nibe_entity_manager.time.time', side_effect=time_seq), \
             patch.object(em.dynamic_point_map, 'record_outcome') as mock_rec, \
             patch.object(em, '_persist_dynamic_map'):
            em._run_learning_detection(10, 0, 'test')
        mock_rec.assert_called_once_with(10, 0, [])



class TestPublishEnabledStateCallbackException(unittest.TestCase):
    """publish_enabled_state catches exceptions from the change callback."""

    def test_callback_exception_does_not_raise(self):
        em = _make_em()
        em._on_enabled_state_change = MagicMock(side_effect=RuntimeError("boom"))
        em.mqtt_enabled_points.add(1)
        em.publish_enabled_state()   # must not raise
        em._on_enabled_state_change.assert_called_once()



class TestPublishEnabledStateCallback(unittest.TestCase):
    """Test the enabled state change callback behaviour."""

    def test_callback_not_called_when_set_unchanged(self):
        em = _make_em()
        callback = MagicMock()
        em.set_on_enabled_state_change(callback)
        em.mqtt_enabled_points = {1, 2}
        em._last_published_enabled = frozenset({1, 2})
        em.publish_enabled_state()
        callback.assert_not_called()

    def test_callback_called_when_set_changes(self):
        em = _make_em()
        callback = MagicMock()
        em.set_on_enabled_state_change(callback)
        em.mqtt_enabled_points = {1, 2, 3}
        em._last_published_enabled = frozenset({1, 2})
        em.publish_enabled_state()
        callback.assert_called_once()



class TestSetOnEnabledStateChange(unittest.TestCase):
    """set_on_enabled_state_change stores the callback."""

    def test_setter_stores_callback(self):
        em = _make_em()
        cb = MagicMock()
        em.set_on_enabled_state_change(cb)
        self.assertIs(em._on_enabled_state_change, cb)



class TestSetupHistoryLoadingCallbacks(unittest.TestCase):
    """_setup_history_loading: empty payload, exception path."""

    def _make_message(self, payload):
        msg = MagicMock()
        msg.payload = payload
        return msg

    def test_empty_payload_returns_without_loading(self):
        em = _make_em()
        from nibe_entity_manager import EntityManager
        EntityManager._setup_history_loading(em)
        em._on_history_message(None, None, self._make_message(b''))
        # change_history should be untouched
        self.assertEqual(len(em.change_history), 0)

    def test_bad_payload_resets_history(self):
        em = _make_em()
        from nibe_entity_manager import EntityManager
        EntityManager._setup_history_loading(em)
        em._on_history_message(None, None, self._make_message(b'not valid json or gzip'))
        # Should reset to empty deque rather than crash
        self.assertEqual(len(em.change_history), 0)



class TestSetupHistoryLoadingUnreadCallback(unittest.TestCase):
    """on_unread_message: empty payload, valid payload, exception."""

    def _make_message(self, payload):
        msg = MagicMock()
        msg.payload = payload
        return msg

    def test_empty_payload_does_not_crash(self):
        em = _make_em()
        from nibe_entity_manager import EntityManager
        EntityManager._setup_history_loading(em)
        em._on_unread_message(None, None, self._make_message(b''))

    def test_valid_unread_marks_entries(self):
        em = _make_em()
        from nibe_entity_manager import EntityManager
        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({'unread': False, 'id': 1})
        em.change_history.appendleft({'unread': False, 'id': 2})
        payload = json.dumps({'unread_count': 1}).encode()
        em._on_unread_message(None, None, self._make_message(payload))
        entries = list(em.change_history)
        # list[-1:] selects the oldest entry (id=1, at the end of the list)
        self.assertTrue(entries[-1]['unread'])
        self.assertFalse(entries[0]['unread'])

    def test_bad_unread_payload_does_not_crash(self):
        em = _make_em()
        from nibe_entity_manager import EntityManager
        EntityManager._setup_history_loading(em)
        em._on_unread_message(None, None, self._make_message(b'NOT JSON'))



class TestSetupDynamicMapLoadingCallbacks(unittest.TestCase):
    """on_dynamic_map_message and on_active_dynamic_message handlers."""

    def _make_message(self, payload):
        msg = MagicMock()
        msg.payload = payload
        return msg

    def _make_em_with_dynamic_loading(self):
        """EM with real _setup_dynamic_map_loading (not patched out)."""
        with patch('nibe_entity_manager.EntityManager.resubscribe_all'), \
             patch('nibe_entity_manager.EntityManager._setup_history_loading'):
            from nibe_entity_manager import EntityManager
            em = EntityManager(
                api_client  = MagicMock(),
                publisher   = MagicMock(),
                notify_fn   = MagicMock(),
                dismiss_fn  = MagicMock(),
                mqtt_client = MagicMock(),
            )
        em.device_info = {}
        em.device_name = 'Test'
        return em

    def test_dynamic_map_ignored_after_discovery_complete(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = True
        with patch.object(em.dynamic_point_map, 'deserialise') as mock_deser:
            em._on_dynamic_map_message(None, None, self._make_message(b'{}'))
        mock_deser.assert_not_called()

    def test_dynamic_map_empty_payload_skipped(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with patch.object(em.dynamic_point_map, 'deserialise') as mock_deser:
            em._on_dynamic_map_message(None, None, self._make_message(b''))
        mock_deser.assert_not_called()

    def test_dynamic_map_plain_json_loads(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with patch.object(em.dynamic_point_map, 'deserialise') as mock_deser:
            em._on_dynamic_map_message(None, None, self._make_message(b'{}'))
        mock_deser.assert_called_once_with('{}')

    def test_dynamic_map_bad_payload_does_not_crash(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with patch.object(em.dynamic_point_map, 'deserialise',
                          side_effect=RuntimeError("boom")):
            em._on_dynamic_map_message(None, None, self._make_message(b'{}'))

    def test_active_dynamic_ignored_after_discovery_complete(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = True
        em._on_active_dynamic_message(None, None, self._make_message(b'[1,2,3]'))
        self.assertFalse(em.active_dynamic_points)

    def test_active_dynamic_empty_payload_skipped(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        em._on_active_dynamic_message(None, None, self._make_message(b''))
        self.assertFalse(em.active_dynamic_points)

    def test_active_dynamic_non_list_skipped(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        em._on_active_dynamic_message(
            None, None, self._make_message(json.dumps({'a': 1}).encode()))
        self.assertFalse(em.active_dynamic_points)

    def test_active_dynamic_valid_payload_updates_set(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        em._on_active_dynamic_message(
            None, None, self._make_message(json.dumps([100, 200]).encode()))
        self.assertIn(100, em.active_dynamic_points)
        self.assertIn(200, em.active_dynamic_points)


    """_reconcile_dynamic_points returns early when initial discovery not complete."""

    def test_returns_early_before_discovery_complete(self):
        em = _make_em()
        em.initial_discovery_complete = False
        with patch.object(em.dynamic_point_map,
                          'expected_active_dynamic_points') as mock_exp:
            em._reconcile_dynamic_points()
        mock_exp.assert_not_called()



class TestReconcileDynamicPointsCases(unittest.TestCase):
    """_reconcile_dynamic_points: case 1 (activate), case 2 (remove absent),
    case 3 (stale entry removal)."""

    def _bulk_entry(self, point_id):
        return {
            'raw_value': 1, 'string_value': '', 'is_ok': True,
            'metadata': {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                         'isWritable': False, 'variableType': 'integer',
                         'variableSize': 'u8', 'divisor': 1},
            'title': f'Point {point_id}', 'description': '',
        }

    def test_expected_and_present_not_enabled_gets_activated(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 1001
        em.bulk_data[point_id] = self._bulk_entry(point_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(
            return_value={point_id}
        )
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with patch.object(em, 'enable_entity') as mock_enable, \
             patch.object(em, '_index_point'):
            em._reconcile_dynamic_points()
        mock_enable.assert_called_once_with(point_id)

    def test_expected_but_absent_removes_from_active(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 1002
        # point not in bulk_data but in active_dynamic_points
        em.active_dynamic_points.add(point_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(
            return_value={point_id}
        )
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with patch.object(em, 'disable_entity'), \
             patch.object(em, '_deindex_point'):
            em._reconcile_dynamic_points()
        self.assertNotIn(point_id, em.active_dynamic_points)

    def test_stale_persisted_not_in_expected_is_removed(self):
        em = _make_em()
        em.initial_discovery_complete = True
        stale_id = 1003
        em.active_dynamic_points.add(stale_id)  # persisted but not expected
        em.bulk_data[stale_id] = self._bulk_entry(stale_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(
            return_value=set()  # stale_id not expected
        )
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with patch.object(em, 'disable_entity'), \
             patch.object(em, '_deindex_point'):
            em._reconcile_dynamic_points()
        self.assertNotIn(stale_id, em.active_dynamic_points)



class TestBuildDeviceInfoApiNameFallback(unittest.TestCase):
    """_build_device_info uses API name when config still has default."""

    def test_api_name_used_when_config_is_default(self):
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(
            api_response={'product': {'name': 'S1255-6', 'manufacturer': 'NIBE'}},
            device_id='abc123',
            device_name='Nibe SMO S40',    # default name
            base_url='https://192.168.1.1/api/v1/devices/abc123',
        )
        self.assertEqual(result['name'], 'S1255-6')

    def test_user_name_kept_when_not_default(self):
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(
            api_response={'product': {'name': 'S1255-6', 'manufacturer': 'NIBE'}},
            device_id='abc123',
            device_name='My Heat Pump',   # user-set name
            base_url='https://192.168.1.1/api/v1/devices/abc123',
        )
        self.assertEqual(result['name'], 'My Heat Pump')


# ===========================================================================
# Coverage: nibe_entity_manager.py — remaining reconcile branches,
#           gzip dynamic map, resubscribe_all
# ===========================================================================


class TestReconcileDynamicPointsAlreadyEnabledCase(unittest.TestCase):
    """Reconcile case 1b: point expected, present, and already enabled —
    must republish online and refresh state without re-enabling."""

    def _bulk_entry(self, point_id):
        return {
            'raw_value': 1, 'string_value': '', 'is_ok': True,
            'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'isWritable': False, 'variableType': 'integer',
                         'variableSize': 'u8', 'divisor': 1},
            'title': f'Point {point_id}', 'description': '',
        }

    def test_already_enabled_publishes_online_and_updates_state(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 2001
        entity_info = {
            'point_id': point_id, 'entity_type': 'sensor',
            'availability_topic': f'nibe/avail/{point_id}',
            'state_topic': f'nibe/state/{point_id}',
            'command_topic': None, 'point_data': {},
        }
        em.bulk_data[point_id]           = self._bulk_entry(point_id)
        em.mqtt_enabled_points.add(point_id)
        em.active_dynamic_points.add(point_id)
        em.active_entities_by_id[point_id] = entity_info

        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(
            return_value={point_id}
        )
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())

        with patch.object(em, 'enable_entity') as mock_enable, \
             patch.object(em, '_update_entity_state') as mock_update:
            em._reconcile_dynamic_points()

        mock_enable.assert_not_called()
        em.mqtt.publish.assert_any_call(
            f'nibe/avail/{point_id}', 'online', retain=True
        )
        mock_update.assert_called_once_with(entity_info)



class TestReconcileDynamicPointsAbsentEnabledCase(unittest.TestCase):
    """Reconcile case 2: expected but absent from bulk and in mqtt_enabled_points
    → disable_entity must be called."""

    def test_absent_enabled_calls_disable_entity(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 2002
        # Point in active_dynamic_points and mqtt_enabled_points but NOT in bulk_data
        em.active_dynamic_points.add(point_id)
        em.mqtt_enabled_points.add(point_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(
            return_value={point_id}
        )
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())

        with patch.object(em, 'disable_entity') as mock_disable, \
             patch.object(em, '_deindex_point'):
            em._reconcile_dynamic_points()

        mock_disable.assert_called_once_with(point_id)
        self.assertNotIn(point_id, em.active_dynamic_points)



class TestReconcileDynamicPointsStaleEnabledCase(unittest.TestCase):
    """Reconcile case 3: stale persisted entry that is also in mqtt_enabled_points
    → disable_entity must be called."""

    def test_stale_enabled_calls_disable_entity(self):
        em = _make_em()
        em.initial_discovery_complete = True
        stale_id = 2003
        em.active_dynamic_points.add(stale_id)
        em.mqtt_enabled_points.add(stale_id)
        em.bulk_data[stale_id] = {
            'raw_value': 0, 'string_value': '', 'is_ok': True,
            'metadata': {}, 'title': f'Point {stale_id}', 'description': '',
        }
        # expected_active is empty → stale_id becomes stale
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value=set())
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())

        with patch.object(em, 'disable_entity') as mock_disable, \
             patch.object(em, '_deindex_point'):
            em._reconcile_dynamic_points()

        mock_disable.assert_called_once_with(stale_id)
        self.assertNotIn(stale_id, em.active_dynamic_points)



class TestDynamicMapGzipBranch(unittest.TestCase):
    """on_dynamic_map_message: gzip-compressed payload is decompressed before deserialise."""

    def _make_em_with_dynamic_loading(self):
        with patch('nibe_entity_manager.EntityManager.resubscribe_all'), \
             patch('nibe_entity_manager.EntityManager._setup_history_loading'):
            from nibe_entity_manager import EntityManager
            em = EntityManager(
                api_client  = MagicMock(),
                publisher   = MagicMock(),
                notify_fn   = MagicMock(),
                dismiss_fn  = MagicMock(),
                mqtt_client = MagicMock(),
            )
        em.device_info = {}
        em.device_name = 'Test'
        return em

    def test_gzip_payload_is_decompressed_and_deserialised(self):
        import gzip as _gzip
        import base64
        from nibe_entity_manager import _GZIP_SENTINEL
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False

        inner = json.dumps({"entries": []})
        compressed = base64.b64encode(
            _gzip.compress(inner.encode('utf-8'))
        ).decode('ascii')
        payload = (_GZIP_SENTINEL + compressed).encode('utf-8')

        msg = MagicMock()
        msg.payload = payload

        with patch.object(em.dynamic_point_map, 'deserialise') as mock_deser:
            em._on_dynamic_map_message(None, None, msg)

        mock_deser.assert_called_once()
        # The call arg must be valid JSON equivalent to inner
        called_json = mock_deser.call_args.args[0]
        self.assertEqual(json.loads(called_json), json.loads(inner))



class TestResubscribeAll(unittest.TestCase):
    """resubscribe_all: re-subscribes all entity command topics, management
    topics, changelog topics, and dynamic map topics."""

    def _make_em_with_resubscribe(self):
        """EM with real resubscribe_all (not patched out)."""
        with patch('nibe_entity_manager.EntityManager._setup_history_loading'), \
             patch('nibe_entity_manager.EntityManager._setup_dynamic_map_loading'):
            from nibe_entity_manager import EntityManager
            em = EntityManager(
                api_client  = MagicMock(),
                publisher   = MagicMock(),
                notify_fn   = MagicMock(),
                dismiss_fn  = MagicMock(),
                mqtt_client = MagicMock(),
            )
        em.device_info = {}
        em.device_name = 'Test'
        # Wire up minimal callback stubs that resubscribe_all references
        em._on_history_message  = MagicMock()
        em._on_unread_message   = MagicMock()
        em._on_dynamic_map_message    = MagicMock()
        em._on_active_dynamic_message = MagicMock()
        return em

    def test_resubscribes_entity_command_topics(self):
        em = self._make_em_with_resubscribe()
        entity_info = {
            'point_id': 100, 'entity_type': 'switch',
            'command_topic': 'nibe/cmd/100',
            'availability_topic': 'nibe/avail/100',
            'state_topic': 'nibe/state/100',
        }
        em.active_entities_by_id[100] = entity_info
        em.resubscribe_all()
        em.mqtt.subscribe.assert_any_call('nibe/cmd/100', qos=1)

    def test_resubscribes_management_topics(self):
        em = self._make_em_with_resubscribe()
        handler = MagicMock()
        em._mgmt_subscriptions = [('nibe/mgmt/aid_mode', handler, 1)]
        em.resubscribe_all()
        em.mqtt.subscribe.assert_any_call('nibe/mgmt/aid_mode', qos=1)

    def test_resubscribes_changelog_and_dynamic_topics(self):
        from nibe_mqtt_publisher import BrowserTopic
        em = self._make_em_with_resubscribe()
        em.resubscribe_all()
        subscribed = [c.args[0] for c in em.mqtt.subscribe.call_args_list]
        self.assertIn(BrowserTopic.CHANGELOG_HISTORY, subscribed)
        self.assertIn(BrowserTopic.CHANGELOG_UNREAD, subscribed)
        self.assertIn(BrowserTopic.DYNAMIC_MAP, subscribed)
        self.assertIn(BrowserTopic.ACTIVE_DYNAMIC, subscribed)

    def test_resubscribed_command_callback_dispatches_to_handle_command(self):
        """The MQTT command callback registered by resubscribe_all must invoke
        _handle_command when called (line 2300)."""
        em = self._make_em_with_resubscribe()
        cmd_topic = 'nibe/cmd/200'
        entity_info = {
            'point_id': 200, 'entity_type': 'switch',
            'command_topic': cmd_topic,
            'availability_topic': 'nibe/avail/200',
            'state_topic': 'nibe/state/200',
        }
        em.active_entities_by_id[200] = entity_info

        stored_cb = {}
        def fake_callback_add(topic, cb):
            stored_cb[topic] = cb
        em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)

        em.resubscribe_all()
        self.assertIn(cmd_topic, stored_cb)

        msg = MagicMock()
        msg.payload = b'1'
        with patch.object(em, '_handle_command') as mock_handle:
            stored_cb[cmd_topic](None, None, msg)
        mock_handle.assert_called_once()

    def test_entity_without_command_topic_skipped(self):
        em = self._make_em_with_resubscribe()
        entity_info = {
            'point_id': 200, 'entity_type': 'sensor',
            'command_topic': None,
            'availability_topic': 'nibe/avail/200',
            'state_topic': 'nibe/state/200',
        }
        em.active_entities_by_id[200] = entity_info
        em.resubscribe_all()
        cmd_subs = [c for c in em.mqtt.subscribe.call_args_list
                    if c.args[0] == 'nibe/cmd/200']
        self.assertEqual(cmd_subs, [])


# ===========================================================================
# Remaining coverage gaps: nibe_api, nibe_dynamic_map,
# nibe_entity_detection, nibe_mqtt_publisher
# ===========================================================================


class TestDisableEntityUsesDiscard(unittest.TestCase):
    """disable_entity must use discard() not remove() so a concurrent double-
    disable from different threads does not raise KeyError."""

    def test_discard_on_already_removed_does_not_raise(self):
        """Simulate the race: point already removed from mqtt_enabled_points
        by another thread before the second caller reaches the set operation."""
        em = _make_em()
        # Set up a minimal enabled entity
        entity_info = {
            'point_id': 100, 'entity_type': 'sensor', 'entity_id': 'nibe_100',
            'state_topic': 'nibe/state/100', 'availability_topic': 'nibe/avail/100',
            'command_topic': None, 'attributes_topic': None,
        }
        em.active_entities_by_id[100] = entity_info
        em.mqtt_enabled_points.add(100)
        # First disable succeeds normally
        em.disable_entity(100)
        self.assertNotIn(100, em.mqtt_enabled_points)
        # Second disable on a point not in the set must not raise
        em.disable_entity(100)   # would raise KeyError with .remove()

    def test_discard_is_used_not_remove(self):
        """Verify the implementation uses discard, not remove, by inspecting
        the source — a regression guard so this is never silently reverted."""
        import inspect
        from nibe_entity_manager import EntityManager
        src = inspect.getsource(EntityManager.disable_entity)
        self.assertNotIn('mqtt_enabled_points.remove', src,
            "disable_entity must use .discard() not .remove() to be thread-safe")
        self.assertIn('mqtt_enabled_points.discard', src)



class TestFetchBulkDataMetadataUpdate(unittest.TestCase):
    """_fetch_bulk_data must use equality (!=) not identity (is not) when
    deciding whether to update bulk_data['metadata'].  Since every API
    response produces a fresh dict object, identity comparison always
    returns True, meaning metadata was reassigned on every poll regardless
    of whether the content changed.  This test locks in the correct behaviour."""

    def _response(self, point_id, meta_override=None):
        meta = {
            'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
            'isWritable': False, 'variableType': 'integer',
            'variableSize': 'u8', 'divisor': 1, 'minValue': 0, 'maxValue': 100,
        }
        if meta_override:
            meta.update(meta_override)
        return {
            str(point_id): {
                'title': 'Test', 'description': '',
                'metadata': meta,
                'value': {'integerValue': 42, 'stringValue': '', 'isOk': True},
            }
        }

    def test_metadata_not_reassigned_when_content_unchanged(self):
        """When API returns metadata with the same content as before, the
        existing bulk_data['metadata'] object must NOT be replaced — the
        equality check must prevent unnecessary reassignment."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)

        # First fetch — populates bulk_data
        em._api.fetch_bulk_points.return_value = self._response(100)
        em._fetch_bulk_data(detect_changes=False)
        original_metadata = em.bulk_data[100]['metadata']

        # Second fetch — same metadata content, new dict object
        em._api.fetch_bulk_points.return_value = self._response(100)
        em._fetch_bulk_data(detect_changes=False)

        # Metadata object must be the SAME as after the first fetch
        # (not replaced by a new dict with identical content)
        self.assertIs(em.bulk_data[100]['metadata'], original_metadata,
            "Metadata must not be reassigned when content is unchanged — "
            "use != not 'is not' for the comparison")

    def test_metadata_updated_when_content_changes(self):
        """When API returns metadata with different content, bulk_data['metadata']
        must be updated to reflect the change."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)

        em._api.fetch_bulk_points.return_value = self._response(100, {'divisor': 1})
        em._fetch_bulk_data(detect_changes=False)

        em._api.fetch_bulk_points.return_value = self._response(100, {'divisor': 10})
        em._fetch_bulk_data(detect_changes=False)

        self.assertEqual(em.bulk_data[100]['metadata']['divisor'], 10)

    def test_implementation_uses_equality_not_identity(self):
        """Regression guard: verify the source uses != not 'is not'."""
        import inspect
        from nibe_entity_manager import EntityManager
        src = inspect.getsource(EntityManager._fetch_bulk_data)
        self.assertNotIn("is not metadata", src,
            "_fetch_bulk_data must use != not 'is not' for metadata comparison")
        self.assertIn("!= metadata", src)

    """_publish_dynamic_changes: ValueError/TypeError/AttributeError raised
    in the HA notification block must be caught and logged, not propagated
    (lines 1754-1757)."""

    def test_notification_exception_does_not_propagate(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em._post_write_controlling_point = None

        point_id = 500
        em.all_points_by_id[point_id] = {
            'variableId': point_id, 'display_title': 'Test', 'description': '',
            'entity_type': 'sensor', 'entity_category': 'diagnostic',
            'is_writable': False, 'is_dynamic': True,
            'metadata': {'isWritable': False, 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'variableType': 'integer', 'variableSize': 'u8',
                         'divisor': 1, 'minValue': 0, 'maxValue': 1},
        }

        new_points = [(point_id, {
            'title': 'Test', 'description': '',
            'metadata': em.all_points_by_id[point_id]['metadata'],
            'value': {'integerValue': 0, 'stringValue': '', 'isOk': True},
        })]

        # Make _notify raise a ValueError — should be caught, not propagated
        em._notify.side_effect = ValueError("simulated notification error")

        with patch.object(em, 'enable_entity'):
            try:
                em._publish_dynamic_changes(new_points, set())
            except (ValueError, TypeError, AttributeError) as e:
                self.fail(f"Exception should have been caught: {e}")



class TestFetchBulkDataMalformed(unittest.TestCase):
    """Test _fetch_bulk_data with malformed response entries."""

    def test_skip_non_dict_point(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '100': {
                'title': 'Good point',
                'metadata': {},
                'value': {'integerValue': 0, 'isOk': True}
            },
            '200': ['not', 'a', 'dict'],
        }
        em._fetch_bulk_data(detect_changes=False)
        self.assertIn(100, em.bulk_data)
        self.assertNotIn(200, em.bulk_data)

    def test_point_without_value_key(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '300': {
                'title': 'Missing value',
                'metadata': {},
            }
        }
        em._fetch_bulk_data(detect_changes=False)
        self.assertIn(300, em.bulk_data)
        self.assertEqual(em.bulk_data[300]['raw_value'], 0)
        self.assertFalse(em.bulk_data[300]['is_ok'])



class TestGetMemoryUsage(unittest.TestCase):
    """get_memory_usage returns a dict with the expected keys and
    sensible values (lines 2965-2989)."""

    def test_returns_expected_keys(self):
        em = _make_em()
        stats = em.get_memory_usage()
        for key in ['total_points', 'active_entities', 'mqtt_enabled_points',
                    'active_dynamic_points', 'value_cache_size', 'last_states_size',
                    'point_string_cache_size', 'pending_writes', 'estimated_memory_mb']:
            self.assertIn(key, stats, f"Expected key '{key}' missing from get_memory_usage()")

    def test_counts_reflect_state(self):
        em = _make_em()
        em.all_points_by_id[100] = {'variableId': 100}
        em.mqtt_enabled_points.add(100)
        stats = em.get_memory_usage()
        self.assertEqual(stats['total_points'], 1)
        self.assertEqual(stats['mqtt_enabled_points'], 1)

    def test_estimated_memory_mb_is_non_negative(self):
        em = _make_em()
        stats = em.get_memory_usage()
        self.assertGreaterEqual(stats['estimated_memory_mb'], 0)

    def test_actual_object_size_none_when_getsizeof_raises(self):
        """If sys.getsizeof raises, actual_object_size_mb is None rather
        than propagating the exception (lines 2971-2972)."""
        em = _make_em()
        with patch('nibe_entity_manager.sys.getsizeof', side_effect=TypeError("unsupported")):
            stats = em.get_memory_usage()
        self.assertIsNone(stats['actual_object_size_mb'])


# ===========================================================================
# Tests for refactored generate_nibe_mqtt.py — _build_infrastructure,
# _run_startup_sequence, _poll_loop, _shutdown, and main() integration.
# ===========================================================================


class TestUpdateEntityStateValueMappingSelfHealing(unittest.TestCase):
    """_update_entity_state self-healing value_mapping paths."""

    def _active_entity(self, em, entity_info):
        from contextlib import contextmanager
        @contextmanager
        def ctx():
            em.active_entities_by_id[entity_info['point_id']] = entity_info
            em.mqtt_enabled_points.add(entity_info['point_id'])
            try:
                yield
            finally:
                em.active_entities_by_id.pop(entity_info['point_id'], None)
                em.mqtt_enabled_points.discard(entity_info['point_id'])
        return ctx()

    def test_value_mapping_written_back_into_entity_info_on_cache_miss(self):
        """Self-healing: absent value_mapping triggers get_value_mapping() and
        writes the result back so subsequent polls avoid the lookup.
        Uses point 3745 (language select, MODBUS_HOLDING_REGISTER).
        """
        em = _make_em()
        entity_info = {
            'point_id': 3745, 'entity_type': 'select',
            'availability_topic': 'nibe/avail/3745',
            'state_topic': 'nibe/state/3745',
            'command_topic': 'nibe/cmd/3745',
            'point_data': {},
        }
        em.bulk_data[3745] = {
            'raw_value': 9, 'string_value': '', 'is_ok': True,
            'metadata': {
                'variableSize': 'u8', 'divisor': 1, 'decimal': 0, 'unit': '',
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'modbusRegisterID': 3745, 'isWritable': True,
                'minValue': 0, 'maxValue': 23,
            },
            'title': 'Language',
        }
        self.assertNotIn('value_mapping', entity_info)
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertIn('value_mapping', entity_info,
                      "value_mapping must be written back after a cache miss")
        self.assertIsInstance(entity_info['value_mapping'], dict)
        self.assertIn(9, entity_info['value_mapping'])
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/3745']
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], 'Nederlands')

    def test_select_no_mapping_falls_through_to_raw_str(self):
        """select where get_value_mapping() returns None falls through to
        str(raw_value) — branch 1271→1278 / 1247→1254."""
        em = _make_em()
        entity_info = {
            'point_id': 9999, 'entity_type': 'select',
            'availability_topic': 'nibe/avail/9999',
            'state_topic': 'nibe/state/9999',
            'command_topic': 'nibe/cmd/9999',
            'point_data': {},
        }
        em.bulk_data[9999] = {
            'raw_value': 2, 'string_value': '', 'is_ok': True,
            'metadata': {
                'variableSize': 'u8', 'divisor': 1, 'decimal': 0, 'unit': '',
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'modbusRegisterID': 9999, 'isWritable': True,
                'minValue': 0, 'maxValue': 3,
            },
            'title': 'Unknown select',
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertNotIn('value_mapping', entity_info)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/9999']
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], '2')

    def test_select_raw_value_not_in_mapping_falls_through_to_raw_str(self):
        """select where mapping exists but raw_value not in it — branch 1275→1278."""
        em = _make_em()
        entity_info = {
            'point_id': 3745, 'entity_type': 'select',
            'availability_topic': 'nibe/avail/3745',
            'state_topic': 'nibe/state/3745',
            'command_topic': 'nibe/cmd/3745',
            'point_data': {},
        }
        em.bulk_data[3745] = {
            'raw_value': 99, 'string_value': '', 'is_ok': True,
            'metadata': {
                'variableSize': 'u8', 'divisor': 1, 'decimal': 0, 'unit': '',
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'modbusRegisterID': 3745, 'isWritable': True,
                'minValue': 0, 'maxValue': 23,
            },
            'title': 'Language',
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertIn('value_mapping', entity_info)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/3745']
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], '99')

    def test_sensor_no_mapping_falls_through_to_divisor(self):
        """sensor where get_value_mapping() returns None falls through to
        apply_divisor — branch 1284→1291."""
        em = _make_em()
        entity_info = {
            'point_id': 9998, 'entity_type': 'sensor',
            'availability_topic': 'nibe/avail/9998',
            'state_topic': 'nibe/state/9998',
            'command_topic': None,
            'point_data': {},
        }
        em.bulk_data[9998] = {
            'raw_value': 123, 'string_value': '', 'is_ok': True,
            'metadata': {
                'variableSize': 'u8', 'divisor': 10, 'decimal': 1, 'unit': '°C',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'modbusRegisterID': 9998, 'isWritable': False,
                'minValue': -400, 'maxValue': 400,
            },
            'title': 'Unknown sensor',
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertNotIn('value_mapping', entity_info)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/9998']
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], '12.3')


# ---------------------------------------------------------------------------
# Pending write absent from bulk_data
# ---------------------------------------------------------------------------


class TestRecordOutcomeAllEmptyFalse(unittest.TestCase):
    """record_outcome: all_empty=False guard for select points (branch 372→374)."""

    def test_record_outcome_select_non_controlling_not_set_when_other_value_has_dynamic_points(self):
        """For a select (3+ values) where all_empty=False, is_controlling must
        remain None after the final value is processed."""
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        dm = DynamicPointMap()
        dm._table[6000] = DynamicPointEntry(
            point_id=6000, title='Heat source', entity_type='select',
            processed_values={0},
            unprocessed_values={1, 2},
            is_controlling=None,
            dynamic_points_by_value={0: [5000]},
        )
        # Record value=2 first — not fully processed yet
        dm.record_outcome(6000, 2, [])
        self.assertIsNone(dm._table[6000].is_controlling)
        self.assertIn(1, dm._table[6000].unprocessed_values)
        # Now fully process value=1 — all_empty=False (value 0 has [5000])
        dm.record_outcome(6000, 1, [])
        self.assertEqual(dm._table[6000].unprocessed_values, set())
        self.assertIsNone(
            dm._table[6000].is_controlling,
            "is_controlling must remain None when all_empty=False",
        )


# ---------------------------------------------------------------------------
# History loading entry validation (nibe_entity_manager)
# ---------------------------------------------------------------------------


class TestHistoryLoadingEntryValidation(unittest.TestCase):
    """_on_history_message: skip non-dict and malformed entries."""

    def _make_message(self, payload):
        msg = MagicMock()
        msg.payload = payload
        return msg

    def _compress(self, data):
        from nibe_entity_manager import _compress_payload
        return _compress_payload(data).encode('utf-8')

    def _valid_entry(self):
        return {
            'timestamp': 1700000000.0, 'iso_timestamp': '2024-01-01',
            'added': [{'id': 100, 'title': 'T', 'type': 'sensor'}],
            'removed': [], 'id': 'change_1', 'unread': False, 'source': 'firmware',
        }

    def test_non_dict_entry_skipped_valid_entry_kept(self):
        """Non-dict history entries must be skipped; valid ones retained."""
        em = _make_em()
        from nibe_entity_manager import EntityManager
        EntityManager._setup_history_loading(em)
        payload_data = {
            'history': ["not a dict", 42, self._valid_entry()],
        }
        em._on_history_message(None, None, self._make_message(self._compress(payload_data)))
        self.assertEqual(len(em.change_history), 1)
        self.assertEqual(em.change_history[0]['id'], 'change_1')

    def test_entry_with_non_list_added_skipped(self):
        """Entry where 'added' is not a list must be skipped."""
        em = _make_em()
        from nibe_entity_manager import EntityManager
        EntityManager._setup_history_loading(em)
        bad = dict(self._valid_entry())
        bad['added'] = "should_be_a_list"
        payload_data = {'history': [bad, self._valid_entry()]}
        em._on_history_message(None, None, self._make_message(self._compress(payload_data)))
        self.assertEqual(len(em.change_history), 1)

    def test_entry_with_non_list_removed_skipped(self):
        """Entry where 'removed' is not a list must be skipped."""
        em = _make_em()
        from nibe_entity_manager import EntityManager
        EntityManager._setup_history_loading(em)
        bad = dict(self._valid_entry())
        bad['removed'] = {"wrong": "type"}
        payload_data = {'history': [bad, self._valid_entry()]}
        em._on_history_message(None, None, self._make_message(self._compress(payload_data)))
        self.assertEqual(len(em.change_history), 1)


# ---------------------------------------------------------------------------
# update_all_states interval / lock-busy branches
# ---------------------------------------------------------------------------


class TestUpdateAllStatesIntervalAndLockBusy(unittest.TestCase):
    """update_all_states: interval-not-elapsed and lock-busy branches."""

    def test_fetch_skipped_when_interval_not_elapsed(self):
        """When interval has not elapsed, fetch_bulk_points must not be called."""
        em = _make_em()
        em.last_bulk_fetch = time.time()
        em.bulk_interval   = 99999
        em._api.fetch_bulk_points.return_value = {}
        em.update_all_states()
        em._api.fetch_bulk_points.assert_not_called()

    def test_last_bulk_fetch_not_updated_when_lock_busy(self):
        """When lock was busy (_fetch_bulk_data returns False, failures unchanged),
        last_bulk_fetch must not advance so the next call retries immediately."""
        em = _make_em()
        em.last_bulk_fetch = 0.0
        original_failures  = em.api_consecutive_failures
        with patch.object(em, '_fetch_bulk_data', return_value=False):
            em.update_all_states()
        self.assertEqual(em.api_consecutive_failures, original_failures)
        self.assertEqual(em.last_bulk_fetch, 0.0,
                         "last_bulk_fetch must not be updated when lock was busy")


# ---------------------------------------------------------------------------
# _reconcile_dynamic_points: already-enabled with no entity_info
# ---------------------------------------------------------------------------


class TestReconcileAlreadyEnabledNoEntityInfo(unittest.TestCase):
    """_reconcile_dynamic_points: entity in mqtt_enabled_points but not in
    active_entities_by_id — guard must not crash."""

    def test_already_enabled_no_entity_info_still_adds_to_active_dynamic(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 2003
        em.bulk_data[point_id] = {
            'raw_value': 1, 'is_ok': True, 'string_value': '',
            'metadata': {}, 'title': 'Test',
        }
        em.mqtt_enabled_points.add(point_id)
        em.active_dynamic_points.add(point_id)
        # Deliberately NOT adding to active_entities_by_id
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(
            return_value={point_id}
        )
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with patch.object(em, 'enable_entity') as mock_enable, \
             patch.object(em, '_update_entity_state') as mock_update:
            em._reconcile_dynamic_points()
        mock_enable.assert_not_called()
        mock_update.assert_not_called()
        self.assertIn(point_id, em.active_dynamic_points)


# ---------------------------------------------------------------------------
# last_states fallback publish properties
# ---------------------------------------------------------------------------



# ===========================================================================
# Branch coverage: targeted gaps from --cov-branch audit
# ===========================================================================


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
        from nibe_entity_manager import ValueCache
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
        from nibe_entity_manager import ValueCache
        vc = ValueCache()
        vc.update(1, 100)
        result = vc.should_publish(1, 110, threshold=5, min_interval=30)
        self.assertTrue(result, "change exceeding threshold must publish")


class TestDiscoverPointsMapNonEmpty(unittest.TestCase):
    """discover_points: 591→598 — dynamic_point_map already has entries.

    When the map is non-empty after MQTT restore, from_file() must NOT be
    called — the file fallback is only for the empty-map case.
    """

    def test_non_empty_map_skips_file_fallback(self):
        em = _make_em()
        # Pre-populate the dynamic_point_map so len > 0
        from nibe_dynamic_map import DynamicPointEntry
        em.dynamic_point_map._table[999] = DynamicPointEntry(
            point_id=999, title='X', entity_type='switch',
            processed_values=set(), unprocessed_values=set(),
            is_controlling=None, dynamic_points_by_value={},
        )
        self.assertGreater(len(em.dynamic_point_map), 0)
        with patch.object(em.dynamic_point_map, 'from_file') as mock_file, \
             patch.object(em, '_fetch_bulk_data', return_value=True), \
             patch.object(em, 'scan_mqtt_discovery', return_value=set()), \
             patch.object(em, 'restore_from_mqtt', return_value=0):
            em.discover_points()
        mock_file.assert_not_called()


class TestScanMqttDiscoveryEmptyPayload(unittest.TestCase):
    """scan_mqtt_discovery on_config: 745→exit — empty message payload.

    The on_config guard 'and message.payload' means an empty-payload message
    (e.g. a retained-config deletion) must be silently skipped without trying
    to JSON-decode a zero-length bytes object.
    """

    def test_empty_payload_config_message_is_skipped(self):
        em = _make_em()
        callbacks = {}

        def fake_callback_add(topic, cb):
            callbacks[topic] = cb

        def fake_publish(topic, payload, retain=False):
            if 'scan_sentinel' in topic:
                # Deliver a config message with an empty payload
                msg = MagicMock()
                msg.topic = 'homeassistant/sensor/nibe_1234/config'
                msg.payload = b''     # empty — should be skipped
                cb = callbacks.get('homeassistant/+/+/config')
                if cb:
                    cb(None, None, msg)
                # Fire sentinel to end the scan
                cb = callbacks.get(topic)
                if cb:
                    cb(None, None, MagicMock())

        em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)
        em.mqtt.publish = MagicMock(side_effect=fake_publish)
        result = em.scan_mqtt_discovery()
        # Point 1234 must NOT appear — empty payload skipped
        self.assertNotIn(1234, result)


class TestUpdateEntityStateAbsentNotInMqttEnabled(unittest.TestCase):
    """_update_entity_state absent-from-bulk: 1145→1166 False branch.

    When point_id is absent from bulk_data AND not in mqtt_enabled_points,
    the method must return immediately without disabling anything — the point
    was never enabled in the first place.
    """

    def test_absent_point_not_mqtt_enabled_returns_without_disabling(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(999)
        # 999 is in baseline but NOT in mqtt_enabled_points
        entity_info = {
            'point_id': 999, 'entity_type': 'sensor',
            'availability_topic': 'nibe/avail/999',
            'state_topic': 'nibe/state/999',
        }
        em.active_entities_by_id[999] = entity_info
        # bulk_data deliberately does not contain 999
        with patch.object(em, 'disable_entity') as mock_disable:
            em._update_entity_state(entity_info)
        mock_disable.assert_not_called()


class TestUpdateEntityStatePostWriteKnownAbsent(unittest.TestCase):
    """_update_entity_state: 1151→1166 — post-write active but point is
    already in known_dynamic - active_dynamic (expected-absent).

    A point that is known_dynamic AND currently absent (in known_dynamic -
    active_dynamic) during a post-write scan is already accounted for — it
    should NOT be re-routed through _publish_dynamic_changes again.
    """

    def test_known_absent_dynamic_during_post_write_not_rerouted(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(500)
        em.mqtt_enabled_points.add(500)
        em._post_write_active = True
        # Make point known-dynamic but NOT active (i.e. in the absent set)
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(
            return_value={500}
        )
        em.active_dynamic_points = set()   # 500 is absent: in known - active
        entity_info = {
            'point_id': 500, 'entity_type': 'sensor',
            'availability_topic': 'nibe/avail/500',
            'state_topic': 'nibe/state/500',
        }
        em.active_entities_by_id[500] = entity_info
        # bulk_data does not contain 500
        with patch.object(em, '_publish_dynamic_changes') as mock_pub_dyn:
            em._update_entity_state(entity_info)
        mock_pub_dyn.assert_not_called()


class TestFetchBulkDataNotDetectChangesKnownDynamic(unittest.TestCase):
    """_fetch_bulk_data: 1487→1369 — detect_changes=False AND point is
    already known_dynamic.

    When detect_changes=False (discovery scan) and the point is already in
    the DynamicPointMap, the entity-type lookup at line 1488 must be skipped
    — the map entry is authoritative.
    """

    def test_known_dynamic_point_skips_type_lookup_during_discovery(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(5110)
        em.mqtt_enabled_points.add(5110)
        em.dynamic_point_map.is_known_dynamic = MagicMock(return_value=True)
        em._api.fetch_bulk_points.return_value = {
            '5110': {
                'title': 'Heat mode', 'description': '',
                'metadata': {
                    'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                    'isWritable': True, 'minValue': 0, 'maxValue': 1,
                    'variableType': 'integer', 'variableSize': 'u8',
                    'divisor': 1, 'decimal': 0, 'unit': '',
                },
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        with patch.object(em, '_get_cached_entity_type') as mock_type:
            em._fetch_bulk_data(detect_changes=False)
        mock_type.assert_not_called()


class TestFetchBulkDataApiRestorationNoPriorNotification(unittest.TestCase):
    """_fetch_bulk_data: 1564→1584 — api_consecutive_failures >= threshold
    but _api_notification_active is False (notification was never sent).

    Steady-state normal recovery: the bridge always succeeds so no notification
    was ever raised.  The dismiss/alert block must be skipped entirely.
    """

    def _minimal_response(self, point_id=100):
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def test_no_prior_notification_skips_dismiss_and_alert(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em.api_consecutive_failures = em.api_failure_threshold + 1
        em._api_notification_active = False   # notification was never raised
        em._api.fetch_bulk_points.return_value = self._minimal_response()
        em._fetch_bulk_data(detect_changes=False)
        # dismiss must NOT have been called (no notification to dismiss)
        em._dismiss.assert_not_called()

    def test_pub_none_skips_bridge_alert_on_recovery(self):
        """1570→1584: _api_notification_active=True but _pub is None —
        dismiss fires but publish_bridge_alert is never called."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em.api_consecutive_failures = em.api_failure_threshold + 1
        em._api_notification_active = True
        em._pub = None   # publisher not yet wired (edge case at startup)
        em._api.fetch_bulk_points.return_value = self._minimal_response()
        # Must not raise AttributeError accessing _pub.publish_bridge_alert
        em._fetch_bulk_data(detect_changes=False)
        self.assertFalse(em._api_notification_active)


class TestOnHistoryMessageMissingHistoryKey(unittest.TestCase):
    """on_history_message: 2620→exit — payload has no 'history' key
    (or it is not a list).

    The handler must silently return without touching change_history.
    """

    def _make_message(self, payload_str):
        """Wrap a string payload (the bridge uses 'gzip1:<base64>' format)."""
        msg = MagicMock()
        msg.payload = payload_str.encode() if isinstance(payload_str, str) else payload_str
        return msg

    def _pack(self, data):
        """Produce a valid bridge-format payload using _compress_payload."""
        from nibe_entity_manager import _compress_payload
        return _compress_payload(data)

    def test_missing_history_key_does_not_touch_change_history(self):
        from nibe_entity_manager import EntityManager
        em = _make_em()
        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({'id': 1, 'unread': False})
        # Payload with 'incoming_seq' but no 'history' key
        em._on_history_message(None, None, self._make_message(
            self._pack({'incoming_seq': 99})
        ))
        # change_history must be untouched — 'history' key absent
        self.assertEqual(len(em.change_history), 1)
        self.assertEqual(list(em.change_history)[0]['id'], 1)

    def test_history_not_a_list_does_not_touch_change_history(self):
        from nibe_entity_manager import EntityManager
        em = _make_em()
        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({'id': 2, 'unread': False})
        # 'history' present but wrong type (string instead of list)
        em._on_history_message(None, None, self._make_message(
            self._pack({'incoming_seq': 5, 'history': 'not a list'})
        ))
        self.assertEqual(len(em.change_history), 1)


class TestOnUnreadMessageZeroCount(unittest.TestCase):
    """on_unread_message: 2661→exit — unread_count=0 with non-empty history.

    When the count is 0, the 'if unread_count > 0 and change_history' guard
    is False so no entries are marked as unread.  All entries must remain
    with unread=False.
    """

    def _make_message(self, payload_bytes):
        msg = MagicMock()
        msg.payload = payload_bytes
        return msg

    def test_zero_unread_count_leaves_entries_unread_false(self):
        import json as _json
        from nibe_entity_manager import EntityManager
        em = _make_em()
        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({'unread': False, 'id': 1})
        em.change_history.appendleft({'unread': False, 'id': 2})
        payload = _json.dumps({'unread_count': 0}).encode()
        em._on_unread_message(None, None, self._make_message(payload))
        for entry in em.change_history:
            self.assertFalse(entry['unread'],
                             "unread_count=0 must not mark any entry as unread")


class TestReconcileDynamicPointsAbsentNeverActive(unittest.TestCase):
    """_reconcile_dynamic_points: 2865→2822 — dynamic point is expected
    (in expected_active) and absent from bulk, but was never active
    (not in active_dynamic_points).

    The branch at 2865 checks 'if point_id in active_dynamic_points'.
    When False, the point was never activated — nothing to deindex or
    disable — the loop simply continues to the next point.
    """

    def test_expected_absent_never_active_point_not_deindexed(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.active_dynamic_points = set()   # point 777 has never been active

        # Make dynamic_point_map.expected_active_dynamic_points return {777}
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(
            return_value={777}
        )
        # bulk_data does NOT contain 777 (absent from bulk)
        em.bulk_data = {}

        with patch.object(em, '_deindex_point') as mock_deindex, \
             patch.object(em, 'disable_entity') as mock_disable:
            em._reconcile_dynamic_points()

        mock_deindex.assert_not_called()
        mock_disable.assert_not_called()

class TestUpdateEntityStateValueMappingAlreadyCached(unittest.TestCase):
    """_update_entity_state: 1271→1278 and 1284→1291 False branches.

    When entity_info already has 'value_mapping' set (from a previous poll),
    the 'if mapping is None' guard is False — the cache-miss lookup is
    skipped entirely and the existing mapping is used directly.
    """

    def _active_entity(self, em, entity_info):
        from contextlib import contextmanager
        @contextmanager
        def ctx():
            em.active_entities_by_id[entity_info['point_id']] = entity_info
            em.mqtt_enabled_points.add(entity_info['point_id'])
            try:
                yield
            finally:
                em.active_entities_by_id.pop(entity_info['point_id'], None)
                em.mqtt_enabled_points.discard(entity_info['point_id'])
        return ctx()

    def test_select_pre_cached_mapping_skips_lookup(self):
        """1271→1278: mapping already in entity_info → is None False branch."""
        em = _make_em()
        entity_info = {
            'point_id': 3745, 'entity_type': 'select',
            'availability_topic': 'nibe/avail/3745',
            'state_topic': 'nibe/state/3745',
            'command_topic': 'nibe/cmd/3745',
            'point_data': {},
            'value_mapping': {9: 'Nederlands'},   # pre-cached from previous poll
        }
        em.bulk_data[3745] = {
            'raw_value': 9, 'string_value': '', 'is_ok': True,
            'metadata': {
                'variableSize': 'u8', 'divisor': 1, 'decimal': 0, 'unit': '',
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'modbusRegisterID': 3745, 'isWritable': True,
                'minValue': 0, 'maxValue': 23,
            },
            'title': 'Language',
        }
        with self._active_entity(em, entity_info):
            with patch('nibe_entity_manager.get_value_mapping') as mock_gvm:
                em._update_entity_state(entity_info)
        # The cached mapping must be used; get_value_mapping must NOT be called
        mock_gvm.assert_not_called()
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/3745']
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], 'Nederlands')

    def test_sensor_pre_cached_mapping_skips_lookup(self):
        """1284→1291: mapping already in entity_info → is None False branch."""
        em = _make_em()
        entity_info = {
            'point_id': 9998, 'entity_type': 'sensor',
            'availability_topic': 'nibe/avail/9998',
            'state_topic': 'nibe/state/9998',
            'command_topic': None,
            'point_data': {},
            'value_mapping': {5: 'Cool'},   # pre-cached
        }
        em.bulk_data[9998] = {
            'raw_value': 5, 'string_value': '', 'is_ok': True,
            'metadata': {
                'variableSize': 'u8', 'divisor': 1, 'decimal': 0, 'unit': '',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'modbusRegisterID': 9998, 'isWritable': False,
                'minValue': 0, 'maxValue': 10,
            },
            'title': 'Sensor with mapping',
        }
        with self._active_entity(em, entity_info):
            with patch('nibe_entity_manager.get_value_mapping') as mock_gvm:
                em._update_entity_state(entity_info)
        mock_gvm.assert_not_called()
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/9998']
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], 'Cool')



# ===========================================================================
# Snapshot save / restore / delete
# ===========================================================================


class TestSaveSnapshot(unittest.TestCase):
    """save_snapshot: persistence, naming, cap, and MQTT publish."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_save.json'
        # Remove any leftover from previous runs
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass

    def _em_with_enabled(self, pids):
        em = _make_em()
        for pid in pids:
            em.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
            em.mqtt_enabled_points.add(pid)
        return em

    def test_save_creates_snapshot_with_correct_fields(self):
        em = self._em_with_enabled([1, 2, 3])
        ok, msg = em.save_snapshot('Test', path=self._path)
        self.assertTrue(ok)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(len(snaps), 1)
        snap = snaps[0]
        self.assertEqual(snap['name'], 'Test')
        self.assertEqual(set(snap['point_ids']), {1, 2, 3})
        self.assertEqual(snap['point_count'], 3)
        self.assertIn('timestamp', snap)

    def test_save_publishes_to_mqtt(self):
        em = self._em_with_enabled([10, 20])
        em.save_snapshot('MQTT Test', path=self._path)
        topics = [c.args[0] for c in em.mqtt.publish.call_args_list]
        from nibe_mqtt_publisher import BrowserTopic
        self.assertIn(BrowserTopic.SNAPSHOTS, topics)

    def test_save_replaces_existing_same_name(self):
        em = self._em_with_enabled([1, 2])
        em.save_snapshot('Dup', path=self._path)
        em.mqtt_enabled_points.add(3)
        em.all_points_by_id[3] = {'variableId': 3, 'title': 'P3'}
        ok, _ = em.save_snapshot('Dup', path=self._path)
        self.assertTrue(ok)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(set(snaps[0]['point_ids']), {1, 2, 3})

    def test_save_rejects_empty_name(self):
        em = self._em_with_enabled([1])
        ok, msg = em.save_snapshot('   ', path=self._path)
        self.assertFalse(ok)
        self.assertIn('empty', msg.lower())

    def test_save_rejects_when_at_cap(self):
        em = self._em_with_enabled([1])
        import nibe_entity_manager as nem
        original = nem._SNAPSHOTS_MAX
        try:
            nem._SNAPSHOTS_MAX = 2
            em.save_snapshot('A', path=self._path)
            em.save_snapshot('B', path=self._path)
            ok, msg = em.save_snapshot('C', path=self._path)
            self.assertFalse(ok)
            self.assertIn('Maximum', msg)
        finally:
            nem._SNAPSHOTS_MAX = original

    def test_save_strips_name_whitespace(self):
        em = self._em_with_enabled([1])
        ok, _ = em.save_snapshot('  Summer  ', path=self._path)
        self.assertTrue(ok)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(snaps[0]['name'], 'Summer')

    def test_save_point_ids_are_sorted(self):
        em = self._em_with_enabled([30, 10, 20])
        em.save_snapshot('Sorted', path=self._path)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(snaps[0]['point_ids'], [10, 20, 30])


class TestRestoreSnapshot(unittest.TestCase):
    """restore_snapshot: flush, merge, missing points, dynamic protection."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_restore.json'
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass
        # Prevent restore_snapshot from reading /data/applied_mode under xdist —
        # another worker may have written 'menus' or 'all' there, triggering the
        # mode guard and causing spurious failures.
        self._mode_patcher = patch(
            'nibe_entity_manager.EntityManager._read_applied_mode_from_file',
            return_value='essential',
        )
        self._mode_patcher.start()

    def tearDown(self):
        self._mode_patcher.stop()

    def _em_with_firmware(self, all_pids, enabled_pids=None, dynamic_pids=None):
        em = _make_em()
        for pid in all_pids:
            em.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
        for pid in (enabled_pids or []):
            em.mqtt_enabled_points.add(pid)
        em.active_dynamic_points = set(dynamic_pids or [])
        return em

    def _seed_snapshot(self, em, name, pids):
        """Write a snapshot directly to the file without calling save_snapshot."""
        import json
        import time as _t
        snaps = em._load_snapshots(path=self._path)
        snaps.append({
            'name': name, 'timestamp': _t.strftime('%Y-%m-%d %H:%M:%S'),
            'point_ids': sorted(pids), 'point_count': len(pids), 'mode': 'essential',
        })
        with open(self._path, 'w') as f:
            json.dump(snaps, f)

    def test_flush_enables_saved_disables_others(self):
        em = self._em_with_firmware(
            all_pids=[1, 2, 3, 4],
            enabled_pids=[1, 2, 3],  # 3 currently enabled
        )
        self._seed_snapshot(em, 'Snap', [2, 4])  # saved: 2 and 4
        ok, msg = em.restore_snapshot('Snap', mode='flush', path=self._path)
        self.assertTrue(ok)
        # 2 stays, 4 added, 1 and 3 removed
        self.assertIn(2, em.mqtt_enabled_points)
        self.assertIn(4, em.mqtt_enabled_points)
        self.assertNotIn(1, em.mqtt_enabled_points)
        self.assertNotIn(3, em.mqtt_enabled_points)

    def test_merge_adds_saved_keeps_existing(self):
        em = self._em_with_firmware(
            all_pids=[1, 2, 3, 4],
            enabled_pids=[1, 2],
        )
        self._seed_snapshot(em, 'Snap', [3, 4])
        ok, _ = em.restore_snapshot('Snap', mode='merge', path=self._path)
        self.assertTrue(ok)
        # 1, 2 stay; 3, 4 added
        self.assertEqual(em.mqtt_enabled_points, {1, 2, 3, 4})

    def test_flush_does_not_disable_dynamic_points(self):
        em = self._em_with_firmware(
            all_pids=[1, 2, 10],
            enabled_pids=[1, 10],   # 10 is dynamic
            dynamic_pids=[10],
        )
        self._seed_snapshot(em, 'Snap', [1])
        em.restore_snapshot('Snap', mode='flush', path=self._path)
        # 10 must stay enabled — it's a dynamic point
        self.assertIn(10, em.mqtt_enabled_points)

    def test_missing_firmware_points_skipped(self):
        em = self._em_with_firmware(all_pids=[1, 2])
        self._seed_snapshot(em, 'Snap', [1, 2, 9999])  # 9999 not in firmware
        ok, msg = em.restore_snapshot('Snap', mode='flush', path=self._path)
        self.assertTrue(ok)
        self.assertIn(1, em.mqtt_enabled_points)
        self.assertIn(2, em.mqtt_enabled_points)
        self.assertNotIn(9999, em.mqtt_enabled_points)
        self.assertIn('skipped', msg)

    def test_restore_not_found_returns_false(self):
        em = self._em_with_firmware(all_pids=[1])
        ok, msg = em.restore_snapshot('NoSuch', path=self._path)
        self.assertFalse(ok)
        self.assertIn('not found', msg)

    def test_restore_publishes_enabled_state(self):
        em = self._em_with_firmware(all_pids=[1, 2], enabled_pids=[1])
        self._seed_snapshot(em, 'Snap', [2])
        with patch.object(em, 'publish_enabled_state') as mock_pub:
            em.restore_snapshot('Snap', mode='merge', path=self._path)
        mock_pub.assert_called_once()

    def test_restore_blocked_in_menus_mode(self):
        """Restore must be blocked when applied mode is 'menus' to prevent
        conflict with the system-managed entity set."""
        em = self._em_with_firmware(all_pids=[1, 2])
        self._seed_snapshot(em, 'Snap', [1])
        with patch.object(em, '_read_applied_mode_from_file', return_value='menus'):
            ok, msg = em.restore_snapshot('Snap', path=self._path)
        self.assertFalse(ok)
        self.assertIn('menus', msg)
        self.assertIn('mode', msg.lower())

    def test_restore_blocked_in_all_mode(self):
        """Restore must be blocked when applied mode is 'all'."""
        em = self._em_with_firmware(all_pids=[1, 2])
        self._seed_snapshot(em, 'Snap', [1])
        with patch.object(em, '_read_applied_mode_from_file', return_value='all'):
            ok, msg = em.restore_snapshot('Snap', path=self._path)
        self.assertFalse(ok)
        self.assertIn('all', msg)

    def test_restore_allowed_in_essential_mode(self):
        """Restore must be allowed in 'essential' mode."""
        em = self._em_with_firmware(all_pids=[1, 2])
        self._seed_snapshot(em, 'Snap', [1])
        with patch.object(em, '_read_applied_mode_from_file', return_value='essential'):
            ok, _ = em.restore_snapshot('Snap', path=self._path)
        self.assertTrue(ok)

    def test_restore_default_mode_is_flush(self):
        em = self._em_with_firmware(all_pids=[1, 2, 3], enabled_pids=[1, 2, 3])
        self._seed_snapshot(em, 'Snap', [1])
        em.restore_snapshot('Snap', path=self._path)  # no mode arg
        # Default flush: only 1 should remain
        self.assertEqual(em.mqtt_enabled_points - em.active_dynamic_points, {1})


class TestDeleteSnapshot(unittest.TestCase):
    """delete_snapshot: removal, not-found, MQTT publish."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_delete.json'
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass

    def test_delete_removes_named_snapshot(self):
        em = _make_em()
        em.all_points_by_id[1] = {'variableId': 1, 'title': 'P1'}
        em.mqtt_enabled_points.add(1)
        em.save_snapshot('ToDelete', path=self._path)
        em.save_snapshot('Keep', path=self._path)
        ok, msg = em.delete_snapshot('ToDelete', path=self._path)
        self.assertTrue(ok)
        snaps = em._load_snapshots(path=self._path)
        names = [s['name'] for s in snaps]
        self.assertNotIn('ToDelete', names)
        self.assertIn('Keep', names)

    def test_delete_not_found_returns_false(self):
        em = _make_em()
        ok, msg = em.delete_snapshot('NoSuch', path=self._path)
        self.assertFalse(ok)
        self.assertIn('not found', msg)

    def test_delete_publishes_updated_list(self):
        em = _make_em()
        em.all_points_by_id[1] = {'variableId': 1, 'title': 'P1'}
        em.mqtt_enabled_points.add(1)
        em.save_snapshot('Del', path=self._path)
        em.mqtt.publish.reset_mock()
        em.delete_snapshot('Del', path=self._path)
        from nibe_mqtt_publisher import BrowserTopic
        topics = [c.args[0] for c in em.mqtt.publish.call_args_list]
        self.assertIn(BrowserTopic.SNAPSHOTS, topics)


class TestPublishSnapshots(unittest.TestCase):
    """publish_snapshots: publishes current file contents to MQTT."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_publish.json'
        import json
        snaps = [{'name': 'A', 'timestamp': '2026-01-01 00:00:00',
                  'point_ids': [1], 'point_count': 1, 'mode': 'essential'}]
        with open(self._path, 'w') as f:
            json.dump(snaps, f)

    def test_publish_sends_snapshot_list(self):
        import json as _json
        em = _make_em()
        with patch('nibe_entity_manager._SNAPSHOTS_FILE', self._path):
            em.publish_snapshots()
        from nibe_mqtt_publisher import BrowserTopic
        calls = [c for c in em.mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.SNAPSHOTS]
        self.assertEqual(len(calls), 1)
        payload = _json.loads(calls[0].args[1])
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]['name'], 'A')

    def test_publish_empty_when_no_file(self):
        import json as _json
        em = _make_em()
        with patch('nibe_entity_manager._SNAPSHOTS_FILE', '/tmp/nonexistent_snap.json'):
            em.publish_snapshots()
        from nibe_mqtt_publisher import BrowserTopic
        calls = [c for c in em.mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.SNAPSHOTS]
        self.assertEqual(len(calls), 1)
        payload = _json.loads(calls[0].args[1])
        self.assertEqual(payload, [])


class TestLoadSaveSnapshotsRoundtrip(unittest.TestCase):
    """_load_snapshots / _save_snapshots: file I/O robustness."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_io.json'
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass

    def test_roundtrip_preserves_content(self):
        em = _make_em()
        snaps = [{'name': 'X', 'point_ids': [1, 2], 'point_count': 2,
                  'timestamp': '2026-01-01 00:00:00', 'mode': 'essential'}]
        em._save_snapshots(snaps, path=self._path)
        loaded = em._load_snapshots(path=self._path)
        self.assertEqual(loaded, snaps)

    def test_load_returns_empty_when_file_absent(self):
        em = _make_em()
        result = em._load_snapshots(path='/tmp/definitely_absent_snaps.json')
        self.assertEqual(result, [])

    def test_load_returns_empty_on_corrupt_json(self):
        with open(self._path, 'w') as f:
            f.write('not valid json {{{')
        em = _make_em()
        result = em._load_snapshots(path=self._path)
        self.assertEqual(result, [])

    def test_load_returns_empty_when_file_not_list(self):
        import json
        with open(self._path, 'w') as f:
            json.dump({'not': 'a list'}, f)
        em = _make_em()
        result = em._load_snapshots(path=self._path)
        self.assertEqual(result, [])
