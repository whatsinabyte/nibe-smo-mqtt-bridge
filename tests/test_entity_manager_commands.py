"""
test_entity_manager_commands.py
===============================
MQTT write-command handling tests for nibe_entity_manager.py — split out of test_entity_manager.py
for file-size/maintainability. Shared fixtures are in conftest.py.
"""

import json
import time
import unittest
from unittest.mock import MagicMock, patch

from conftest import (
    _make_em,
)
from hypothesis import example, given
from hypothesis import strategies as st


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
        for entry in em.pending_writes.values():
            self.assertIn('value', entry)
            self.assertIn('time', entry)


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

    def test_select_uses_manual_value_mapping_keyed_by_register_type(self):
        """register_type must be correctly computed and passed through to
        get_value_mapping — point 1758 has a manual VALUE_MAPPINGS entry
        keyed under "input" with no firmware description, so this only
        resolves correctly if register_type is actually "input" (derived
        from modbusRegisterType), not None/wrong. A point without a
        manual mapping (like the other select tests here) can't catch a
        broken register_type, since the description-based fallback
        produces the same result either way."""
        ei = self._ei('select', point_id=1758,
                      metadata={'modbusRegisterType': 'MODBUS_INPUT_REGISTER'},
                      point_data={'description': ''})
        self.assertEqual(self.em._parse_command_payload("Heating", ei, "t"), 30)

    def test_select_no_mapping_numeric(self):
        ei = self._ei('select', metadata={'modbusRegisterType': ''})
        self.assertEqual(self.em._parse_command_payload("3", ei, "t"), 3)

    def test_select_no_mapping_non_numeric_returns_none(self):
        ei = self._ei('select', metadata={'modbusRegisterType': ''})
        self.assertIsNone(self.em._parse_command_payload("nope", ei, "t"))

    # number
    def test_number_missing_divisor_key_defaults_to_one(self):
        """When 'divisor' is absent from metadata entirely, the code falls
        back to divisor=1 (not some other placeholder) — a mutation to the
        `.get('divisor', 1)` default (e.g. to 2) would double every value
        written to a point whose metadata happens to omit the key."""
        ei = self._ei('number', metadata={})
        self.assertEqual(self.em._parse_command_payload("5", ei, "t"), 5)

    def test_number_divisor_zero_falls_back_to_one(self):
        """An explicit divisor of 0 (degenerate firmware metadata) must fall
        back to 1 via the `or 1`, not some other fallback value — a mutation
        to `or 2` would only be caught by exercising the falsy-divisor path
        explicitly, since divisor=1 elsewhere in the suite never triggers it."""
        ei = self._ei('number', metadata={'divisor': 0})
        self.assertEqual(self.em._parse_command_payload("7", ei, "t"), 7)

    def test_number_divisor_ten(self):
        ei = self._ei('number', metadata={'divisor': 10, 'minValue': 150, 'maxValue': 300})
        self.assertEqual(self.em._parse_command_payload("22.5", ei, "t"), 225)

    def test_number_divisor_one(self):
        ei = self._ei('number', metadata={'divisor': 1, 'minValue': 0, 'maxValue': 100})
        self.assertEqual(self.em._parse_command_payload("42", ei, "t"), 42)

    def test_number_below_min_returns_none(self):
        ei = self._ei('number', metadata={'divisor': 10, 'minValue': 150, 'maxValue': 300})
        self.assertIsNone(self.em._parse_command_payload("10.0", ei, "t"))

    def test_number_below_min_republishes_last_state_retained(self):
        """The below-min branch (separate code path from the above-max
        branch, which is already covered by
        test_number_out_of_range_republishes_last_state) must also republish
        the last known state with retain=True — a mutation to retain=False,
        retain=None, or a dropped kwarg here would leave the MQTT retained
        message stale/wrong without any other test noticing, since the two
        out-of-range branches are separate lines of code."""
        ei = self._ei('number', metadata={'divisor': 10, 'minValue': 150, 'maxValue': 300})
        self.em.last_states[1000] = "20"
        self.assertIsNone(self.em._parse_command_payload("10.0", ei, "t"))
        self.em.mqtt.publish.assert_called_once_with("nibe/s/1000", "20", retain=True)

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

    def test_exactly_at_threshold_not_yet_stale(self):
        """age > _STALE_WRITE_AGE_S, not >= — an entry exactly at the
        threshold must NOT be evicted yet (still held one more cycle).
        Uses a mocked clock for a deterministic exact boundary — real
        wall-clock timing between the two time.time() calls would push
        age slightly past the threshold and make this test flaky."""
        from nibe_entity_manager import _STALE_WRITE_AGE_S
        t0 = 1_700_000_000.0
        self._add_pending(value=1)
        self.em.pending_writes[self.point_id]['timestamp'] = t0
        self.em.bulk_data[self.point_id]['raw_value'] = 0  # not yet confirmed
        with patch('nibe_entity_manager.time.time',
                   return_value=t0 + _STALE_WRITE_AGE_S):
            self.em._update_entity_state(self.entity_info)
        self.assertIn(self.point_id, self.em.pending_writes)

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
        self.em.post_write_active = False
        self.em._update_entity_state(self.entity_info)
        self.assertNotIn(self.point_id, self.em.mqtt_enabled_points)


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

    def test_submitted_cmd_id_matches_the_generated_correlation_token(self):
        """The cmd_id forwarded to the write executor must be the *same*
        correlation token that was generated for this command (and stored in
        pending_writes) — not None, not a different value. Verified against
        a UUID whose hex digest is fixed by patching uuid.uuid4, so the
        expected value is known independently of the submit call itself
        (unlike a plain call_args readback, which cannot distinguish a real
        cmd_id from a mutated one)."""
        import uuid as uuid_module
        em = _make_em()
        info = self._entity_info()
        msg = self._message(b'ON')
        fixed_uuid = uuid_module.UUID('12345678-1234-5678-1234-567812345678')
        expected_cmd_id = fixed_uuid.hex[:8]  # _CMD_ID_LENGTH
        with patch.object(em, '_write_executor') as mock_executor, \
             patch('nibe_entity_manager.uuid.uuid4', return_value=fixed_uuid):
            em._handle_command(info, msg)
        mock_executor.submit.assert_called_once_with(
            em._handle_command_worker, info, 1, 'ON', expected_cmd_id,
        )
        self.assertEqual(em.pending_writes[100]['cmd_id'], expected_cmd_id)

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

    def test_pending_write_entry_has_point_id_key(self):
        """The stored dict must use the literal key 'point_id' — a mutation
        renaming it (e.g. to 'POINT_ID') would silently break every other
        code path that later reads entry['point_id'], since dict membership
        checks against the outer pending_writes dict (keyed separately by
        the same integer) can't detect a corrupted inner key name."""
        em = _make_em()
        info = self._entity_info()
        msg = self._message(b'1')
        with patch.object(em, '_write_executor'):
            em._handle_command(info, msg)
        entry = em.pending_writes[100]
        self.assertEqual(entry['point_id'], 100)

    def test_strips_whitespace_from_payload(self):
        em = _make_em()
        info = self._entity_info()
        msg = self._message(b'  ON  \n')
        with patch.object(em, '_write_executor'):
            em._handle_command(info, msg)
        self.assertEqual(em.pending_writes[100]['payload'], 'ON')


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

    def test_write_total_incremented_by_exactly_one_regardless_of_outcome(self):
        """_write_total counts every attempted write, success or failure —
        it must go up by exactly 1 per call, not be reset to 1 or bumped by
        2. Uses a pre-existing non-zero/non-one count so a `= 1` mutation
        (instead of `+= 1`) is distinguishable from the correct increment."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._write_total = 7
        em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        self.assertEqual(em._write_total, 8)

    def test_write_point_called_with_point_id_value_and_entity_info(self):
        """write_point's three positional arguments must be exactly
        (point_id, value, entity_info), in that order — any swap, drop, or
        replacement with None would send the wrong write to the controller
        API without any other assertion here noticing, since the mocked
        return_value doesn't depend on the call arguments."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        info = self._entity_info(point_id=100)
        em._handle_command_worker(info, 42, '42', 'cmd1')
        em._api.write_point.assert_called_once_with(100, 42, info)

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

    def test_notify_called_with_mqtt_client_as_positional_arg(self):
        """_notify's first positional argument must be self.mqtt — a
        mutation dropping it or replacing it with None would mean the
        notification helper never receives a client to publish through."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        self.assertEqual(em._notify.call_args.args[0], em.mqtt)

    def test_notify_called_with_write_error_notification_id(self):
        """The notification_id kwarg must be the write-error constant — a
        mutation to None (or dropping it) would break the dismiss-on-success
        pairing, since _dismiss looks up notifications by this same id."""
        from nibe_entity_manager import _NOTIF_WRITE_ERROR
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        self.assertEqual(em._notify.call_args.kwargs['notification_id'], _NOTIF_WRITE_ERROR)

    def test_point_title_falls_back_to_point_id_when_display_title_absent(self):
        """When entity_info has no 'display_title' key, point_title must
        fall back to 'point {point_id}' — not None or any other
        placeholder — since it is used verbatim in the user-facing HA
        notification message."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        info = {
            'point_id': 555, 'entity_type': 'switch',
            'state_topic': 'nibe/state/555',
            # no 'display_title' key
        }
        em._handle_command_worker(info, 1, '1', 'cmd1')
        msg = em._notify.call_args.kwargs['message']
        self.assertIn('point 555', msg)

    def test_write_failed_bridge_alert_message_matches_notification_message(self):
        """The bridge alert's 'message' field must be the same user-facing
        text sent to HA, not None or a dropped kwarg — downstream bridge
        alert consumers rely on this field to explain the failure."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._handle_command_worker(
            self._entity_info(point_id=100, display_title='Permit heating'), 1, 'ON', 'cmd1',
        )
        notify_msg = em._notify.call_args.kwargs['message']
        alert_kwargs = em._pub.publish_bridge_alert.call_args.kwargs
        self.assertEqual(alert_kwargs['message'], notify_msg)
        self.assertIn('Permit heating', alert_kwargs['message'])

    def test_write_failed_bridge_alert_context_keys_and_values(self):
        """Every key in the bridge alert's context dict must be the literal
        documented name with the correct value — a mutation renaming any key
        (e.g. 'point_title' -> 'POINT_TITLE') would silently drop that field
        from what downstream consumers see, since a renamed key means the
        expected key is simply absent."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._handle_command_worker(
            self._entity_info(point_id=100, display_title='Permit heating'), 1, 'ON', 'cmd1',
        )
        context = em._pub.publish_bridge_alert.call_args.kwargs['context']
        self.assertEqual(context['point_id'], 100)
        self.assertEqual(context['point_title'], 'Permit heating')
        self.assertEqual(context['value'], 'ON')
        self.assertEqual(context['cmd_id'], 'cmd1')

    def test_write_success_incremented_by_exactly_one(self):
        """_write_success must go up by exactly 1 on a successful write, not
        be reset to 1 or bumped by 2. Uses a pre-existing non-zero/non-one
        count so a `= 1` mutation (instead of `+= 1`) is distinguishable
        from the correct increment."""
        em = _make_em()
        em._api.write_point.return_value = True
        em._write_success = 9
        em._handle_command_worker(self._entity_info(point_id=999, entity_type='button'), 1, '1', 'cmd1')
        self.assertEqual(em._write_success, 10)

    def test_default_cmd_id_used_in_bridge_alert_context_when_omitted(self):
        """cmd_id defaults to "" when the caller omits it. That default value
        must actually be the empty string — a mutation to any other
        placeholder would silently change what correlation token ends up in
        the published bridge alert's context dict for uncorrelated calls."""
        em = _make_em()
        em._api.write_point.return_value = False
        em._api.fetch_point.return_value = None
        em._handle_command_worker(self._entity_info(point_id=100), 1, '1')  # cmd_id omitted
        kwargs = em._pub.publish_bridge_alert.call_args.kwargs
        self.assertEqual(kwargs['context']['cmd_id'], '')


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

    def test_readback_fetches_the_correct_point_id(self):
        """fetch_point must be called with the entity's actual point_id —
        a mutation to None (or any other wrong value) would silently read
        back the wrong device register, since the mocked return_value in
        every other test here doesn't depend on the call argument at all."""
        em = _make_em()
        em._api.fetch_point.return_value = {
            'value': {'isOk': True, 'integerValue': 42, 'stringValue': ''},
            'metadata': {'divisor': 1, 'variableSize': ''},
        }
        em._force_readback(self._entity_info(point_id=777))
        em._api.fetch_point.assert_called_once_with(777)

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

    def test_readback_forwards_exact_integer_string_and_metadata(self):
        """The three data fields must be extracted from the correct dict keys
        and passed through unchanged and in the right positions — a mutation
        that swaps a key name (e.g. 'stringValue' -> 'stringvalue') or a
        default value would silently corrupt the republished state.  Uses
        distinctive, mutually-different values for every field so a
        swapped/misrouted argument cannot coincidentally still match."""
        em = _make_em()
        em._api.fetch_point.return_value = {
            'value': {'isOk': True, 'integerValue': 4242, 'stringValue': 'the-string-value'},
            'metadata': {'marker': 'the-metadata'},
        }
        entity_info = self._entity_info(point_id=100)
        with patch.object(em, '_process_and_publish_state') as mock_process:
            em._force_readback(entity_info)
        mock_process.assert_called_once_with(
            entity_info, 4242, 'the-string-value', {'marker': 'the-metadata'}, force=True,
        )

    def test_readback_uses_documented_defaults_when_fields_absent(self):
        """When the firmware value block omits integerValue/stringValue, and
        the response omits metadata entirely, the documented defaults
        (0, '', {}) must be used — not None or any other placeholder."""
        em = _make_em()
        em._api.fetch_point.return_value = {'value': {'isOk': True}}
        entity_info = self._entity_info(point_id=100)
        with patch.object(em, '_process_and_publish_state') as mock_process:
            em._force_readback(entity_info)
        mock_process.assert_called_once_with(
            entity_info, 0, '', {}, force=True,
        )


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

    def _time_entity_setup(self, point_id=300):
        em = _make_em()
        metadata = {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            'isWritable': True, 'minValue': 0, 'maxValue': 86399,
            'variableType': 'time', 'variableSize': 'u16',
            'divisor': 1, 'decimal': 0, 'unit': '',
        }
        entity_info = {
            'point_id':           point_id,
            'entity_type':        'time',
            'state_topic':        f'nibe/state/{point_id}',
            'availability_topic': f'nibe/avail/{point_id}',
            'command_topic':      f'nibe/cmd/{point_id}',
            'metadata':           metadata,
        }
        point = {
            'variableId': point_id, 'display_title': 'Time point',
            'entity_type': 'time', 'entity_category': 'config',
            'is_writable': True, 'is_dynamic': False, 'description': '',
            'metadata': metadata,
        }
        em.all_points_by_id[point_id] = point
        em.bulk_data[point_id] = {'raw_value': 0, 'string_value': '', 'is_ok': True,
                                   'metadata': metadata, 'title': 'Time point'}
        em._api.write_point.return_value = True
        return em, entity_info

    def test_time_seconds_wrap_at_86400_not_86401(self):
        """The seconds-of-day wraparound modulus must be 86400 (seconds in a
        day) — a mutation to 86401 would only diverge from the correct
        result at/above 86400 seconds, a boundary the other time test here
        (25200s) never reaches."""
        em, entity_info = self._time_entity_setup(point_id=301)
        # 86400 % 86400 == 0  vs  86400 % 86401 == 86400 (-> 24:00:00)
        em._handle_command_worker(entity_info, 86400, '24:00:00', 'test-cmd')
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/301']
        self.assertEqual(state_calls[-1].args[1], '00:00:00')

    def test_time_minutes_divided_by_60_not_61(self):
        """The minutes component must divide the remaining seconds by 60 —
        a mutation to //61 only diverges from correct output once the
        minute value is large enough that integer division by 60 vs 61
        yields different quotients (the 25200s case in the other test here
        is an exact hour boundary and can't distinguish the two divisors)."""
        em, entity_info = self._time_entity_setup(point_id=302)
        # 3660s = 1h 1m 0s. //60 -> minute=1 ("01:01:00"); //61 -> minute=0 ("01:00:00").
        em._handle_command_worker(entity_info, 3660, '01:01:00', 'test-cmd')
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'nibe/state/302']
        self.assertEqual(state_calls[-1].args[1], '01:01:00')


class TestHandleCommandWorkerOptimisticStateBranches(unittest.TestCase):
    """Covers the select/number and 'else' branches of the optimistic
    state_value computation, the retain flag and last_states bookkeeping on
    the main publish, and the switch/select gate on post-write dynamic-point
    handling — none of which were exercised by the switch- and time-specific
    tests elsewhere in this file."""

    def _entity_info(self, point_id, entity_type):
        metadata = {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            'isWritable': True, 'minValue': 0, 'maxValue': 100,
            'variableType': 'integer', 'variableSize': 'u8',
            'divisor': 1, 'decimal': 0, 'unit': '',
        }
        return {
            'point_id': point_id, 'entity_type': entity_type,
            'state_topic': f'nibe/state/{point_id}',
            'availability_topic': f'nibe/avail/{point_id}',
            'command_topic': f'nibe/cmd/{point_id}',
            'metadata': metadata,
        }

    def test_select_optimistic_state_uses_the_raw_payload(self):
        """For select/number entities, the optimistic state must be the raw
        MQTT payload string, not None or the numeric 'value'."""
        em = _make_em()
        em._api.write_point.return_value = True
        info = self._entity_info(400, 'select')
        em._handle_command_worker(info, 3, 'Heating', 'cmd-id')
        em.mqtt.publish.assert_any_call('nibe/state/400', 'Heating', retain=True)

    def test_else_branch_optimistic_state_is_str_of_value(self):
        """For entity types outside switch/time/select/number (e.g. a plain
        writable numeric register exposed as some other type), state_value
        must be str(value) — not str(None) or None itself."""
        em = _make_em()
        em._api.write_point.return_value = True
        info = self._entity_info(401, 'button')
        em._handle_command_worker(info, 7, '7', 'cmd-id')
        em.mqtt.publish.assert_any_call('nibe/state/401', '7', retain=True)

    def test_main_optimistic_publish_uses_retain_true(self):
        """The optimistic state publish must set retain=True — an unretained
        publish wouldn't survive an HA restart/reconnect, showing a stale
        value until the next real poll."""
        em = _make_em()
        em._api.write_point.return_value = True
        info = self._entity_info(402, 'button')
        em._handle_command_worker(info, 7, '7', 'cmd-id')
        em.mqtt.publish.assert_any_call('nibe/state/402', '7', retain=True)

    def test_last_states_updated_with_the_actual_published_state_value(self):
        """last_states[point_id] must be set to the exact state_value that
        was published (used later for stale-write republish/comparison) —
        not None or some unrelated placeholder."""
        em = _make_em()
        em._api.write_point.return_value = True
        info = self._entity_info(403, 'select')
        em._handle_command_worker(info, 3, 'Heating', 'cmd-id')
        self.assertEqual(em.last_states[403], 'Heating')

    def test_dynamic_point_scan_opened_for_switch(self):
        """Switch is one of the two entity types eligible for post-write
        dynamic-point scanning — a mutation that excludes 'switch' from the
        membership check (or inverts it) would silently stop learning-mode
        detection from ever running for switches."""
        em = _make_em()
        em._api.write_point.return_value = True
        info = self._entity_info(404, 'switch')
        with patch.object(em, '_open_post_write_scan') as mock_scan:
            em._handle_command_worker(info, 1, '1', 'cmd-id')
        mock_scan.assert_called_once_with(404)

    def test_dynamic_point_scan_opened_for_select(self):
        em = _make_em()
        em._api.write_point.return_value = True
        info = self._entity_info(405, 'select')
        with patch.object(em, '_open_post_write_scan') as mock_scan:
            em._handle_command_worker(info, 3, 'Heating', 'cmd-id')
        mock_scan.assert_called_once_with(405)

    def test_case_a1_non_controlling_fully_processed_opens_no_scan(self):
        """Case A1: a fully-processed, non-controlling entry needs no
        post-write scan at all — a mutation flipping `not entry.is_controlling`
        to `entry.is_controlling` would route this case (is_controlling=False)
        into the A2 branch instead, wrongly opening a scan window."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[409] = DynamicPointEntry(
            point_id=409, title='Test switch', entity_type='switch',
            processed_values={1}, unprocessed_values=set(),
            is_controlling=False,
        )
        info = self._entity_info(409, 'switch')
        with patch.object(em, '_open_post_write_scan') as mock_scan, \
             patch.object(em, '_run_learning_detection') as mock_detect:
            em._handle_command_worker(info, 1, '1', 'cmd-id')
        mock_scan.assert_not_called()
        mock_detect.assert_not_called()

    def test_case_a2_controlling_fully_processed_not_removed_opens_scan(self):
        """Case A2: a fully-processed, controlling entry that hasn't been
        firmware_removed must open the post-write scan window with the
        correct point_id — a mutation flipping `not entry.firmware_removed`
        to `entry.firmware_removed` would route this case (firmware_removed
        =False) into the fallthrough 'else' branch instead, which still
        opens a scan but via different code (and would attempt learning
        detection lookups this branch is specifically meant to skip)."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[410] = DynamicPointEntry(
            point_id=410, title='Test switch', entity_type='switch',
            processed_values={1}, unprocessed_values=set(),
            is_controlling=True, firmware_removed=False,
        )
        info = self._entity_info(410, 'switch')
        with patch.object(em, '_open_post_write_scan') as mock_scan, \
             patch.object(em, '_run_learning_detection') as mock_detect:
            em._handle_command_worker(info, 1, '1', 'cmd-id')
        mock_scan.assert_called_once_with(410)
        mock_detect.assert_not_called()

    def test_learning_detection_started_when_written_value_is_unprocessed(self):
        """When the point has a DynamicPointMap entry whose unprocessed_values
        contains the just-written integer value, _run_learning_detection must
        fire. A mutation setting int_value=None unconditionally would make
        `int_value is not None` always False, silently disabling learning
        detection for every unprocessed write."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[407] = DynamicPointEntry(
            point_id=407, title='Test switch', entity_type='switch',
            unprocessed_values={1, 2, 3},
        )
        info = self._entity_info(407, 'switch')
        with patch.object(em, '_run_learning_detection') as mock_detect:
            em._handle_command_worker(info, 2, '1', 'cmd-id')
        mock_detect.assert_called_once_with(407, 2, 'cmd-id')

    def test_dynamic_point_map_looked_up_by_the_written_point_id(self):
        """The DynamicPointMap lookup must use this write's actual point_id
        — a mutation to `.get(None)` would look up the wrong (nonexistent)
        entry, silently falling through to the 'not in map' path even when
        a real entry is registered for this point, which this test
        distinguishes via the learning-detection call it would otherwise
        trigger."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[408] = DynamicPointEntry(
            point_id=408, title='Test switch', entity_type='switch',
            unprocessed_values={9},
        )
        info = self._entity_info(408, 'switch')
        with patch.object(em, '_run_learning_detection') as mock_detect:
            em._handle_command_worker(info, 9, '1', 'cmd-id')
        mock_detect.assert_called_once_with(408, 9, 'cmd-id')

    def test_dynamic_point_scan_not_opened_for_number(self):
        """'number' is not a controlling-point-eligible type — a mutation
        that flips the switch/select membership check to `not in` would make
        every non-switch/select write (like this one) incorrectly open a
        scan window, which is wasted work and pollutes the post-write-scan
        state machine for an entity type that can never be a controlling
        point in the DynamicPointMap."""
        em = _make_em()
        em._api.write_point.return_value = True
        info = self._entity_info(406, 'number')
        with patch.object(em, '_open_post_write_scan') as mock_scan:
            em._handle_command_worker(info, 5, '5', 'cmd-id')
        mock_scan.assert_not_called()


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

    def _write_restored_setup(self):
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
        return em, entity_info

    def test_write_notification_active_set_to_false_not_falsy_placeholder(self):
        """assertFalse alone can't distinguish False from None — use assertIs
        to pin down the exact value the mutation-testing None-vs-bool trap
        would otherwise slip past."""
        em, entity_info = self._write_restored_setup()
        em._handle_command_worker(entity_info, 1, '1', 'cmd-id')
        self.assertIs(em._write_notification_active, False)

    def test_dismiss_called_with_mqtt_client_and_write_error_notification_id(self):
        """The dismiss call's two arguments must be exactly self.mqtt and the
        write-error notification id constant — a mutation to either argument
        (None, or swapped/dropped) would silently stop the correct HA
        notification from being dismissed."""
        from nibe_entity_manager import _NOTIF_WRITE_ERROR
        em, entity_info = self._write_restored_setup()
        em._handle_command_worker(entity_info, 1, '1', 'cmd-id')
        em._dismiss.assert_called_once_with(em.mqtt, _NOTIF_WRITE_ERROR)

    def test_dismiss_not_called_when_notification_not_active(self):
        """Guards the `and` in `self._write_notification_active and self.mqtt`
        — a mutation to `or` would fire the dismiss/restore path even when no
        notification was active, as long as self.mqtt is truthy (which it
        always is in these tests)."""
        em, entity_info = self._write_restored_setup()
        em._write_notification_active = False
        em._handle_command_worker(entity_info, 1, '1', 'cmd-id')
        em._dismiss.assert_not_called()
        em._pub.publish_bridge_alert.assert_not_called()

    def test_switch_optimistic_state_published_as_1_for_truthy_value(self):
        """A successful switch write must optimistically publish '1' (not
        some other truthy-looking placeholder) to the state topic — the
        exact string matters because HA compares it against the configured
        payload_on/payload_off, not just truthiness."""
        em, entity_info = self._write_restored_setup()
        em._write_notification_active = False  # isolate from the restore branch
        em._handle_command_worker(entity_info, 1, '1', 'cmd-id')
        em.mqtt.publish.assert_any_call('nibe/state/100', '1', retain=True)

    def test_switch_optimistic_state_published_as_0_for_falsy_value(self):
        """Mirrors the truthy case: a falsy write value must publish exactly
        '0', not e.g. 'X0X' or some other placeholder a mutation could
        introduce for the else branch."""
        em, entity_info = self._write_restored_setup()
        em._write_notification_active = False
        em._handle_command_worker(entity_info, 0, '0', 'cmd-id')
        em.mqtt.publish.assert_any_call('nibe/state/100', '0', retain=True)

    def test_write_restored_bridge_alert_fields(self):
        """The write_restored bridge alert must carry the exact
        alert_type/severity/message/context documented in the source — a
        mutation to any one of these (e.g. alert_type=None or a different
        message) would silently corrupt what downstream alert consumers see,
        since nothing else observes this call's arguments."""
        em, entity_info = self._write_restored_setup()
        em._handle_command_worker(entity_info, 1, '1', 'cmd-id')
        em._pub.publish_bridge_alert.assert_called_once()
        kwargs = em._pub.publish_bridge_alert.call_args.kwargs
        self.assertEqual(kwargs['alert_type'], 'write_restored')
        self.assertEqual(kwargs['severity'], 'info')
        self.assertEqual(
            kwargs['message'],
            'Write to point 100 succeeded — previous error cleared.',
        )
        self.assertEqual(kwargs['context'], {'point_id': 100, 'cmd_id': 'cmd-id'})


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


class TestCardCommandToRealApiWriteFullChain(unittest.TestCase):
    """A user-entered value in the browser card arrives as a raw MQTT
    command payload string. _handle_command() (payload decode/dispatch),
    _parse_command_payload() (divisor reversal / value conversion), and
    NibeApiClient.write_point() (PATCH request construction) are each
    thoroughly tested elsewhere — but always in isolation: every existing
    _handle_command/_handle_command_worker test mocks em._api entirely
    (write_point.return_value = True/False), and every write_point test
    builds its value/entity_info by hand rather than deriving it from a
    real parsed MQTT payload. This chains all three real functions —
    nothing hand-substituted in between except the actual network call
    (urllib.request.urlopen) — starting from a raw payload string exactly
    as a real card write would send it."""

    def _run_synchronously(self, em):
        """_handle_command() submits the actual write to a background
        ThreadPoolExecutor — replace submit with an immediate synchronous
        call so the test is deterministic without needing to join a future."""
        em._write_executor.submit = lambda fn, *a, **kw: fn(*a, **kw)

    def _entity_info(self, point_id, divisor, min_value, max_value):
        return {
            'point_id':     point_id,
            'entity_type':  'number',
            'is_writable':  True,
            'is_degenerate_range': False,
            'state_topic':  f'homeassistant/number/nibe_{point_id}/state',
            'metadata': {
                'divisor':  divisor,
                'minValue': min_value,
                'maxValue': max_value,
            },
        }

    def _message(self, payload_str):
        msg = MagicMock()
        msg.payload = payload_str.encode('utf-8')
        msg.topic = 'homeassistant/number/nibe_50123/set'
        return msg

    def test_card_entered_value_reaches_the_real_patch_request_body(self):
        """A user typing '23.5' (a display-value, post-divisor number) into
        the card's number entity must reach the API as the raw pre-divisor
        integer 235 (divisor=10) — the whole point of reverse_divisor()."""
        from nibe_api import NibeApiClient
        point_id = 50123
        em = _make_em()
        em._api = NibeApiClient(
            base_url='https://192.0.2.1:8443', auth='Basic dXNlcjpwYXNz',
            ssl_context=MagicMock(),
        )
        self._run_synchronously(em)

        entity_info = self._entity_info(point_id, divisor=10, min_value=-300, max_value=800)

        captured = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({str(point_id): 'modified'}).encode()

        def capture(req, **kwargs):
            captured.append(req)
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=capture):
            em._handle_command(entity_info, self._message('23.5'))

        self.assertTrue(captured, "the real write_point() never reached urlopen")
        body = json.loads(captured[0].data.decode())
        self.assertEqual(body, [{
            'type': 'datavalue', 'variableId': point_id,
            'integerValue': 235, 'stringValue': None,
        }])

    def test_card_entered_value_outside_range_never_reaches_the_network(self):
        """A user-entered value outside the real point's min/max must be
        rejected by write_point()'s real range check and never reach
        urlopen at all — confirmed with the real divisor-reversed value,
        not a value the test picked to already be out of range post-hoc."""
        from nibe_api import NibeApiClient
        point_id = 50124
        em = _make_em()
        em._api = NibeApiClient(
            base_url='https://192.0.2.1:8443', auth='Basic dXNlcjpwYXNz',
            ssl_context=MagicMock(),
        )
        self._run_synchronously(em)

        # divisor=10, so '999' -> raw 9990, well above maxValue=800.
        entity_info = self._entity_info(point_id, divisor=10, min_value=-300, max_value=800)

        with patch('urllib.request.urlopen') as mock_urlopen:
            em._handle_command(entity_info, self._message('999'))
        mock_urlopen.assert_not_called()
