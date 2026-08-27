"""
test_entity_manager_state.py
============================
Per-entity state processing and publishing tests for nibe_entity_manager.py — split out of test_entity_manager.py
for file-size/maintainability. Shared fixtures are in conftest.py.
"""

import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

from conftest import (
    _make_em,
)
from hypothesis import assume, example, given
from hypothesis import strategies as st


class TestValueCacheDeduplication(unittest.TestCase):
    def _setup(self):
        em = _make_em()
        pid = 500
        ei = {
            "point_id": pid,
            "entity_type": "sensor",
            "entity_id": f"nibe_{pid}",
            "state_topic": f"homeassistant/sensor/nibe_{pid}/state",
            "availability_topic": f"homeassistant/sensor/nibe_{pid}/avail",
            "command_topic": None,
            "is_writable": False,
            "display_title": "Outdoor temp",
            "metadata": {
                "minValue": -300,
                "maxValue": 300,
                "divisor": 10,
                "isWritable": False,
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "variableType": "integer",
                "intDefaultValue": 0,
                "unit": "°C",
                "shortUnit": "°C",
            },
        }
        em.active_entities_by_id[pid] = ei
        em.bulk_data[pid] = {
            "raw_value": 206,
            "is_ok": True,
            "string_value": "",
            "metadata": ei["metadata"],
            "display_title": "Outdoor temp",
        }
        return em, pid

    def _state_publish_count(self, em, pid):
        """Count publishes to the state topic only (not availability)."""
        topic = f"homeassistant/sensor/nibe_{pid}/state"
        return sum(
            1
            for c in em.mqtt.publish.call_args_list
            if c.args[0] == topic or (c.args and c.args[0] == topic)
        )

    def test_first_call_publishes(self):
        em, pid = self._setup()
        em._update_entity_state(em.active_entities_by_id[pid])
        self.assertGreater(self._state_publish_count(em, pid), 0)

    def test_same_value_not_republished(self):
        em, pid = self._setup()
        em._update_entity_state(em.active_entities_by_id[pid])
        count = self._state_publish_count(em, pid)
        em._update_entity_state(em.active_entities_by_id[pid])
        self.assertEqual(
            self._state_publish_count(em, pid),
            count,
            "Identical state value must not be republished to state topic",
        )

    def test_changed_value_republished(self):
        em, pid = self._setup()
        em._update_entity_state(em.active_entities_by_id[pid])
        count = self._state_publish_count(em, pid)
        em.bulk_data[pid]["raw_value"] = 210
        em._update_entity_state(em.active_entities_by_id[pid])
        self.assertGreater(self._state_publish_count(em, pid), count)


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

    def _entity_info(
        self,
        point_id=100,
        entity_type="sensor",
        point_data=None,
        state_topic=_UNSET,
        availability_topic=_UNSET,
    ):
        return {
            "point_id": point_id,
            "entity_type": entity_type,
            "point_data": point_data or {},
            "state_topic": (
                f"nibe/state/{point_id}" if state_topic is self._UNSET else state_topic
            ),
            "availability_topic": (
                f"nibe/avail/{point_id}"
                if availability_topic is self._UNSET
                else availability_topic
            ),
        }

    def _metadata(self, variable_size="", divisor=1, change=0, **extra):
        m = {"variableSize": variable_size, "divisor": divisor, "change": change}
        m.update(extra)
        return m

    # -- availability ----------------------------------------------------

    def test_always_publishes_online_availability_first(self):
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="switch"),
            1,
            "",
            self._metadata(),
        )
        em.mqtt.publish.assert_any_call("nibe/avail/100", "online", retain=True)

    # -- sentinel handling -------------------------------------------------

    def test_sentinel_s16_binary_sensor_goes_offline(self):
        """A sentinel value (sensor disconnected/faulted) on a binary_sensor
        marks the entity offline rather than publishing a misleading state."""
        em = _make_em()
        info = self._entity_info(entity_type="binary_sensor")
        em._process_and_publish_state(info, -32768, "", self._metadata(variable_size="s16"))
        em.mqtt.publish.assert_any_call("nibe/avail/100", "offline", retain=True)
        # Must return early — no state_topic publish for the sentinel itself.
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/100"]
        self.assertEqual(state_calls, [])

    def test_sentinel_s16_sensor_publishes_offline_not_zero(self):
        """A sentinel value on any entity type (including regular sensor)
        must publish offline on the availability topic and return without
        publishing a state value. Previously only binary_sensor got this
        treatment and sensors fell through to state '0', showing a
        misleading 0°C in HA for disconnected sensors like BT71."""
        em = _make_em()
        info = self._entity_info(entity_type="sensor")
        em._process_and_publish_state(info, -32768, "", self._metadata(variable_size="s16"))
        # Must publish offline on the availability topic
        em.mqtt.publish.assert_any_call("nibe/avail/100", "offline", retain=True)
        # Must NOT publish a state value
        state_calls = [c for c in em.mqtt.publish.call_args_list if c[0][0] == "nibe/state/100"]
        self.assertEqual(
            len(state_calls), 0, "No state must be published when sentinel value is detected"
        )

    def test_sentinel_u16_sensor_publishes_offline_not_garbage_value(self):
        """The u16 sentinel (65535) was previously completely untested —
        a mutation to the sentinel_values dict's 'u16' key went uncaught.
        A disconnected u16 sensor must go offline, not silently publish
        the raw sentinel integer as a misleading real value."""
        em = _make_em()
        info = self._entity_info(entity_type="sensor")
        em._process_and_publish_state(info, 65535, "", self._metadata(variable_size="u16"))
        em.mqtt.publish.assert_any_call("nibe/avail/100", "offline", retain=True)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/100"]
        self.assertEqual(state_calls, [])

    def test_sentinel_s32_sensor_publishes_offline_not_garbage_value(self):
        """The s32 sentinel (-2147483648) was previously completely untested."""
        em = _make_em()
        info = self._entity_info(entity_type="sensor")
        em._process_and_publish_state(info, -2147483648, "", self._metadata(variable_size="s32"))
        em.mqtt.publish.assert_any_call("nibe/avail/100", "offline", retain=True)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/100"]
        self.assertEqual(state_calls, [])

    def test_sentinel_u32_sensor_publishes_offline_not_garbage_value(self):
        """The u32 sentinel (4294967295) was previously completely untested."""
        em = _make_em()
        info = self._entity_info(entity_type="sensor")
        em._process_and_publish_state(info, 4294967295, "", self._metadata(variable_size="u32"))
        em.mqtt.publish.assert_any_call("nibe/avail/100", "offline", retain=True)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/100"]
        self.assertEqual(state_calls, [])

    def test_non_sentinel_value_not_treated_as_sentinel(self):
        """A value that happens to be large but isn't the exact sentinel
        constant must be processed normally, not misidentified."""
        em = _make_em()
        info = self._entity_info(entity_type="switch")
        em._process_and_publish_state(info, 1, "", self._metadata(variable_size="s16"))
        em.mqtt.publish.assert_any_call("nibe/state/100", "1", retain=True)

    # -- basic entity-type dispatch -----------------------------------------

    def test_switch_truthy_value_is_on(self):
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="switch"), 1, "", self._metadata()
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "1", retain=True)

    def test_switch_zero_value_is_off(self):
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="switch"), 0, "", self._metadata()
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "0", retain=True)

    def test_binary_sensor_zero_is_off_string(self):
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="binary_sensor"), 0, "", self._metadata()
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "OFF", retain=True)

    def test_binary_sensor_nonzero_is_on_string(self):
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="binary_sensor"), 1, "", self._metadata()
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "ON", retain=True)

    def test_text_passes_through_string_value(self):
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="text"),
            0,
            "Hello firmware",
            self._metadata(),
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "Hello firmware", retain=True)

    def test_time_seconds_converted_to_hhmmss(self):
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="time"), 9015, "", self._metadata()
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "02:30:00", retain=True)

    def test_switch_ignores_divisor_unlike_sensor_path(self):
        """mutants 66/67: entity_type == 'switch' literal corrupted to
        'XXswitchXX' / 'SWITCH'. With divisor=1 (the default used by every
        other switch test here), the switch branch ("1"/"0") and the
        fall-through divisor branch (apply_divisor(1, 1) == "1") produce
        the same output, masking the mutation. Using divisor=10 makes them
        diverge: the switch branch must still emit "1" while the
        fall-through divisor branch would emit "0.1"."""
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="switch"),
            1,
            "",
            self._metadata(divisor=10),
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "1", retain=True)

    def test_time_wraps_past_midnight(self):
        """raw_value % 86400 — a value of exactly one day must wrap to 00:00:00,
        not overflow into a 25-hour-style display."""
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="time"), 86400, "", self._metadata()
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "00:00:00", retain=True)

    def test_time_non_numeric_raw_value_falls_back_to_raw_string(self):
        """A non-numeric raw_value must not crash the poll cycle — falls
        back to str(raw_value) instead of raising out of int(raw_value)."""
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="time"),
            "not-a-number",
            "",
            self._metadata(),
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "not-a-number", retain=True)

    def test_time_hour_divisor_exact_boundary(self):
        """mutant 90: secs // 3600 -> secs // 3601. At exactly 3600s
        (1 hour), the real hour divisor gives 3600//3600=1 ('01'), while
        the mutant's 3600//3601=0 ('00')."""
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="time"), 3600, "", self._metadata()
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "01:00:00", retain=True)

    def test_time_minute_modulo_exact_boundary(self):
        """mutant 93: (secs % 3600) // 60 -> (secs % 3601) // 60. At 3660s
        (1h 1m), the real code gives (3660%3600)//60 = 60//60 = 1 ('01'),
        while the mutant gives (3660%3601)//60 = 59//60 = 0 ('00')."""
        em = _make_em()
        em._process_and_publish_state(
            self._entity_info(entity_type="time"), 3660, "", self._metadata()
        )
        em.mqtt.publish.assert_any_call("nibe/state/100", "01:01:00", retain=True)

    def test_plain_sensor_applies_divisor(self):
        em = _make_em()
        info = self._entity_info(point_id=999, entity_type="sensor")
        em._process_and_publish_state(info, 348, "", self._metadata(divisor=10))
        em.mqtt.publish.assert_any_call("nibe/state/999", "34.8", retain=True)

    # -- point-specific firmware decoding ------------------------------------

    def test_point_2685_periodic_increase_date_conversion(self):
        """Days-since-2010-01-01 -> ISO date. 5000 days after 2010-01-01."""
        from datetime import date, timedelta

        expected = (date(2010, 1, 1) + timedelta(days=5000)).isoformat()
        em = _make_em()
        info = self._entity_info(point_id=2685, entity_type="sensor")
        em._process_and_publish_state(info, 5000, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2685", expected, retain=True)

    def test_point_2685_invalid_value_falls_back_to_raw_string(self):
        """An absurd day count that would overflow datetime's range must
        not crash — falls back to the raw value as a string."""
        em = _make_em()
        info = self._entity_info(point_id=2685, entity_type="sensor")
        em._process_and_publish_state(info, 99999999, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2685", "99999999", retain=True)

    def test_point_2453_eb101_firmware_version_decoding(self):
        """Confirmed worked example from the source comment: 12481 -> 3.3.1
        (this installation's actual S2125-12 firmware version)."""
        em = _make_em()
        info = self._entity_info(point_id=2453, entity_type="sensor")
        em._process_and_publish_state(info, 12481, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2453", "3.3.1", retain=True)

    def test_point_14987_uses_same_eb101_decoding_as_2453(self):
        """14987 is documented as an alternate register for the same
        EB101 firmware version — must decode identically to 2453."""
        em = _make_em()
        info = self._entity_info(point_id=14987, entity_type="sensor")
        em._process_and_publish_state(info, 12481, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/14987", "3.3.1", retain=True)

    def test_point_2509_smo_firmware_version_decoding(self):
        """Confirmed worked example: 1035 (0x040B) -> 4.11."""
        em = _make_em()
        info = self._entity_info(point_id=2509, entity_type="sensor")
        em._process_and_publish_state(info, 1035, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2509", "4.11", retain=True)

    def test_point_2453_non_numeric_raw_value_falls_back_to_raw_string(self):
        """A non-numeric raw_value (e.g. firmware sending a stray string)
        must not crash the poll cycle — falls back to str(raw_value)."""
        em = _make_em()
        info = self._entity_info(point_id=2453, entity_type="sensor")
        em._process_and_publish_state(info, "not-a-number", "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2453", "not-a-number", retain=True)

    def test_point_2509_non_numeric_raw_value_falls_back_to_raw_string(self):
        em = _make_em()
        info = self._entity_info(point_id=2509, entity_type="sensor")
        em._process_and_publish_state(info, "not-a-number", "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2509", "not-a-number", retain=True)

    def test_point_2022_heating_and_compressor_running(self):
        """Hand-traced worked example: bit12 (Heating) + bit2+bit4 (compressor
        running) -> 'Heating (Running)'."""
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type="sensor")
        v = (1 << 12) | (1 << 2) | (1 << 4)
        em._process_and_publish_state(info, v, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2022", "Heating (Running)", retain=True)

    def test_point_2022_non_numeric_raw_value_falls_back_to_raw_string(self):
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type="sensor")
        em._process_and_publish_state(info, "not-a-number", "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2022", "not-a-number", retain=True)

    def test_point_2022_idle_when_no_mode_bits_set(self):
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type="sensor")
        em._process_and_publish_state(info, 0, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2022", "Idle", retain=True)

    def test_point_2022_compressor_starting_no_running_bit(self):
        """bit4 set without bit2 -> 'Starting', not 'Running'."""
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type="sensor")
        v = (1 << 12) | (1 << 4)
        em._process_and_publish_state(info, v, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2022", "Heating (Starting)", retain=True)

    def test_point_2022_mode_active_no_compressor_bits_is_preheating(self):
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type="sensor")
        v = 1 << 13  # Hot water mode, no compressor bits
        em._process_and_publish_state(info, v, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2022", "Hot water (Preheating)", retain=True)

    def test_point_2022_multiple_modes_combined_with_plus(self):
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type="sensor")
        v = (1 << 13) | (1 << 12) | (1 << 2) | (1 << 4)  # Hot water + Heating, running
        em._process_and_publish_state(info, v, "", self._metadata())
        published = [
            c.args[1] for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/2022"
        ]
        self.assertEqual(len(published), 1)
        self.assertIn("Hot water", published[0])
        self.assertIn("Heating", published[0])
        self.assertIn("+", published[0])
        self.assertIn("(Running)", published[0])

    def test_point_2022_cooling_mode_bit20(self):
        """mutants 172-175: bit 20 -> 21 / label text corruption. Bit 20
        alone (no compressor bits) must decode to 'Cooling (Preheating)'.
        With the mutant's key renamed to 21, bit 20 matches nothing and
        the whole thing falls through to 'Idle' instead."""
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type="sensor")
        v = 1 << 20
        em._process_and_publish_state(info, v, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/2022", "Cooling (Preheating)", retain=True)

    def test_point_2022_hot_water_boost_mode_bit14(self):
        """mutants 176-179: bit 14 -> 15 / label text corruption. Bit 14
        alone must decode to 'Hot water boost (Preheating)'. With the
        mutant's key renamed to 15, bit 14 matches nothing and the result
        falls through to 'Idle'."""
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type="sensor")
        v = 1 << 14
        em._process_and_publish_state(info, v, "", self._metadata())
        em.mqtt.publish.assert_any_call(
            "nibe/state/2022", "Hot water boost (Preheating)", retain=True
        )

    def test_point_2022_multiple_modes_exact_join_separator(self):
        """mutant 221: ' + '.join(modes) -> 'XX + XX'.join(modes). The
        existing 'combined_with_plus' test only checks assertIn('+', ...),
        which the mutant's separator ('XX + XX') also satisfies (it still
        contains a '+'). This test pins the full expected string —
        independently constructed from the known dict iteration order
        (20, 14, 13, 12) — so only the exact ' + ' separator passes."""
        em = _make_em()
        info = self._entity_info(point_id=2022, entity_type="sensor")
        v = (1 << 13) | (1 << 12) | (1 << 2) | (1 << 4)  # Hot water + Heating, running
        em._process_and_publish_state(info, v, "", self._metadata())
        em.mqtt.publish.assert_any_call(
            "nibe/state/2022", "Hot water + Heating (Running)", retain=True
        )

    # -- select / sensor value-mapping ----------------------------------------

    def test_select_mapped_value_shows_label(self):
        em = _make_em()
        info = self._entity_info(
            point_id=555, entity_type="select", point_data={"description": "0 = Off, 1 = Auto"}
        )
        with patch("nibe_entity_manager.get_value_mapping", return_value={0: "Off", 1: "Auto"}):
            em._process_and_publish_state(info, 1, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/555", "Auto", retain=True)

    def test_select_unmapped_value_falls_back_to_raw_string(self):
        """A raw value not present in the mapping (e.g. firmware added a
        new enum value not yet in our table) must not crash — shows the
        raw number rather than dropping the update."""
        em = _make_em()
        info = self._entity_info(point_id=555, entity_type="select")
        with patch("nibe_entity_manager.get_value_mapping", return_value={0: "Off", 1: "Auto"}):
            em._process_and_publish_state(info, 99, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/555", "99", retain=True)

    def test_sensor_with_mapping_shows_label(self):
        em = _make_em()
        info = self._entity_info(point_id=556, entity_type="sensor")
        with patch("nibe_entity_manager.get_value_mapping", return_value={10: "Heating"}):
            em._process_and_publish_state(info, 10, "", self._metadata())
        em.mqtt.publish.assert_any_call("nibe/state/556", "Heating", retain=True)

    def test_sensor_without_mapping_applies_divisor(self):
        em = _make_em()
        info = self._entity_info(point_id=557, entity_type="sensor")
        with patch("nibe_entity_manager.get_value_mapping", return_value=None):
            em._process_and_publish_state(info, 205, "", self._metadata(divisor=10))
        em.mqtt.publish.assert_any_call("nibe/state/557", "20.5", retain=True)

    # -- publish gating ----------------------------------------------------

    def test_missing_state_topic_logs_and_skips_publish(self):
        """An entity_info missing state_topic must not crash — logs a
        warning and skips the state publish (availability is still sent)."""
        em = _make_em()
        info = self._entity_info(entity_type="switch", state_topic=None)
        em._process_and_publish_state(info, 1, "", self._metadata())
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/100"]
        self.assertEqual(state_calls, [])

    def test_unchanged_value_within_rate_limit_not_republished(self):
        """Calling twice in immediate succession with the same value must
        only publish the state once — the ValueCache rate-limit/dedup gate
        suppresses the redundant second publish."""
        em = _make_em()
        info = self._entity_info(point_id=222, entity_type="sensor")
        em._process_and_publish_state(info, 100, "", self._metadata(divisor=1))
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, 100, "", self._metadata(divisor=1))
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/222"]
        self.assertEqual(state_calls, [])

    def test_force_true_always_republishes(self):
        em = _make_em()
        info = self._entity_info(point_id=223, entity_type="sensor")
        em._process_and_publish_state(info, 100, "", self._metadata(divisor=1))
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, 100, "", self._metadata(divisor=1), force=True)
        em.mqtt.publish.assert_any_call("nibe/state/223", "100", retain=True)

    def test_last_states_updated_after_publish(self):
        em = _make_em()
        info = self._entity_info(point_id=224, entity_type="switch")
        em._process_and_publish_state(info, 1, "", self._metadata())
        self.assertEqual(em.last_states[224], "1")

    def test_should_publish_is_called_with_the_real_point_id_not_a_shared_key(self):
        """value_cache.should_publish(point_id, ...) must be called with
        THIS entity's own point_id — if all points shared one cache key
        (e.g. a mutation to None), two unrelated points would corrupt each
        other's suppression state.

        A naive two-point test doesn't catch this: any point's *first ever*
        call always publishes regardless of should_publish's result, via
        the separate `point_id not in self.last_states` OR-fallback in the
        caller — so the mutation hides behind that fallback on a fresh
        point_id. The corruption only becomes observable on a *repeated*,
        unchanged call for a point whose value happens to have been
        clobbered, under the shared key, by a different point's call in
        between:
          1. B publishes 50 (its own first call — always succeeds either way).
          2. A publishes 999. Under a shared cache key this overwrites what
             was cached for B's key too.
          3. B publishes 50 again (same value, no time has passed). Correctly
             suppressed by the real per-point cache (nothing changed for B).
             But if the cache is shared, the value now on file under B's
             key is A's 999, not B's own last 50 — so the mutant sees a
             949-unit 'change' that never happened and republishes."""
        em = _make_em()
        info_a = self._entity_info(point_id=301, entity_type="sensor")
        info_b = self._entity_info(point_id=302, entity_type="sensor")
        meta = self._metadata(divisor=1, change=1)

        # Each call must be >= min_interval (self.bulk_interval, 30s) apart —
        # otherwise ValueCache.should_publish's own time-gate suppresses the
        # call before it ever reaches the value-diff check, regardless of
        # which cache key it used, masking the mutation just as thoroughly
        # as the near-simultaneous-calls version of this test did.
        with patch("nibe_caching.time.time") as mock_time:
            mock_time.return_value = 1_000_000.0
            em._process_and_publish_state(info_b, 50, "", meta)
            mock_time.return_value = 1_000_100.0
            em._process_and_publish_state(info_a, 999, "", meta)
            mock_time.return_value = 1_000_200.0
            em.mqtt.publish.reset_mock()
            em._process_and_publish_state(info_b, 50, "", meta)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/302"]
        self.assertEqual(
            state_calls,
            [],
            "Unchanged value for point 302 must not republish its state — "
            "a shared/wrong cache key would make this look like a change",
        )


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

    _ENTITY_TYPES: ClassVar[list] = [
        "sensor",
        "switch",
        "binary_sensor",
        "number",
        "select",
        "time",
    ]

    def _entity_info(self, point_id=100, entity_type="sensor"):
        return {
            "point_id": point_id,
            "entity_type": entity_type,
            "state_topic": f"nibe/state/{point_id}",
            "availability_topic": f"nibe/avail/{point_id}",
            "point_data": {},
            "value_mapping": None,
        }

    def _metadata(self, **kw):
        m = {
            "variableSize": "u8",
            "divisor": 1,
            "change": 0,
            "decimal": 0,
            "modbusRegisterType": "MODBUS_INPUT_REGISTER",
            "minValue": 0,
            "maxValue": 100,
        }
        m.update(kw)
        return m

    @given(
        entity_type=st.sampled_from(_ENTITY_TYPES),
        raw_value=st.integers(min_value=-32767, max_value=32767),  # exclude sentinel
    )
    @example(entity_type="sensor", raw_value=0)
    @example(entity_type="binary_sensor", raw_value=0)
    @example(entity_type="switch", raw_value=1)
    @example(entity_type="time", raw_value=9015)  # 02:30:00
    @example(entity_type="number", raw_value=50)
    def test_non_sentinel_always_publishes_online_availability(self, entity_type, raw_value):
        """For any non-sentinel value, availability must be published 'online'
        before the state.  Regression guard: a new entity type added without
        updating the sentinel check would skip the online publish."""
        em = _make_em()
        info = self._entity_info(entity_type=entity_type)
        with patch("nibe_entity_manager.get_value_mapping", return_value=None):
            em._process_and_publish_state(info, raw_value, "", self._metadata())
        avail_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/avail/100"]
        self.assertTrue(avail_calls, f"No availability publish for entity_type={entity_type!r}")
        self.assertEqual(
            avail_calls[0].args[1], "online", f"Expected 'online', got {avail_calls[0].args[1]!r}"
        )

    @given(entity_type=st.sampled_from(_ENTITY_TYPES))
    @example(entity_type="sensor")
    @example(entity_type="binary_sensor")
    @example(entity_type="switch")
    def test_s16_sentinel_publishes_offline_availability(self, entity_type):
        """The s16 sentinel value (-32768) must publish 'offline' for all
        entity types — the sentinel means the sensor is disconnected."""
        em = _make_em()
        info = self._entity_info(entity_type=entity_type)
        em._process_and_publish_state(info, -32768, "", self._metadata(variableSize="s16"))
        avail_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/avail/100"]
        self.assertTrue(avail_calls, "Sentinel value must still publish to availability topic")
        # The LAST availability publish must be 'offline'
        self.assertEqual(
            avail_calls[-1].args[1], "offline", "Sentinel value must publish 'offline'"
        )

    @given(raw_value=st.integers(min_value=-32767, max_value=32767))
    @example(raw_value=0)
    @example(raw_value=1)
    @example(raw_value=255)
    def test_binary_sensor_state_always_on_or_off(self, raw_value):
        """binary_sensor state must always be 'ON' or 'OFF' — never a
        numeric string.  This is an HA protocol requirement: binary_sensor
        entities must report ON/OFF, not 0/1."""
        em = _make_em()
        info = self._entity_info(entity_type="binary_sensor")
        em._process_and_publish_state(info, raw_value, "", self._metadata())
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/100"]
        if state_calls:
            state = state_calls[-1].args[1]
            self.assertIn(state, ("ON", "OFF"), f"binary_sensor state {state!r} is not ON or OFF")

    @given(
        entity_type=st.sampled_from(_ENTITY_TYPES),
        raw_value=st.integers(min_value=-32767, max_value=32767),
    )
    def test_state_payload_always_string(self, entity_type, raw_value):
        """State topic payload must always be a str — paho MQTT requires
        string payloads, and HA's MQTT integration expects string values."""
        em = _make_em()
        info = self._entity_info(entity_type=entity_type)
        with patch("nibe_entity_manager.get_value_mapping", return_value=None):
            em._process_and_publish_state(info, raw_value, "", self._metadata())
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/100"]
        for call in state_calls:
            self.assertIsInstance(
                call.args[1], str, f"state payload {call.args[1]!r} is not a string"
            )

    @given(raw_value=st.integers(min_value=-100_000, max_value=100_000))
    @example(raw_value=0)  # 00:00:00 — midnight
    @example(raw_value=3600)  # 01:00:00 — hour boundary (kills secs // 3601 mutants)
    @example(raw_value=3660)  # 01:01:00 — minute boundary (kills % 3601 mutants)
    @example(raw_value=86400)  # 00:00:00 — wraps exactly one day forward
    @example(raw_value=86399)  # 23:59:00 — last minute before the wrap
    @example(raw_value=-3600)  # negative raw_value must still wrap into 0..86399
    def test_time_hhmmss_decoding_invariants(self, raw_value):
        """'time' entity_type decoding (secs = raw_value % 86400, then
        secs // 3600 : (secs % 3600) // 60 : 00) must hold for any raw_value:
        the emitted string is always HH:MM:00 with 0 <= HH <= 23 and
        0 <= MM <= 59, and it must match Python's own reference computation
        of the same formula — not just "looks like a time string"."""
        em = _make_em()
        info = self._entity_info(entity_type="time")
        em._process_and_publish_state(info, raw_value, "", self._metadata())
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/100"]
        self.assertTrue(state_calls, "time entity_type produced no state publish")
        state = state_calls[-1].args[1]

        secs = raw_value % 86400
        expected = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:00"
        self.assertEqual(state, expected)

        hh, mm, ss = state.split(":")
        self.assertTrue(0 <= int(hh) <= 23, f"hour {hh!r} out of range")
        self.assertTrue(0 <= int(mm) <= 59, f"minute {mm!r} out of range")
        self.assertEqual(ss, "00")


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
            "point_id": point_id,
            "entity_type": "sensor",
            "state_topic": f"nibe/state/{point_id}",
            "availability_topic": f"nibe/avail/{point_id}",
            "point_data": {},
        }

    def _metadata(self, change=5, divisor=10):
        return {"variableSize": "s16", "divisor": divisor, "change": change}

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
        em._process_and_publish_state(info, first, "", meta)
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, first, "", meta)  # identical value
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/1708"]
        self.assertEqual(
            state_calls, [], f"Identical value with threshold {change}: must be suppressed"
        )

    @example(
        first=200, second=206, change=5
    )  # S2125 point 1708: 200→206 = Δ6 ≥ threshold 5 → publish
    @example(
        first=228, second=234, change=5
    )  # S2125 point 4 (BT1 outdoor): 228→234 = Δ6 ≥ threshold 5 → publish
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
        em._process_and_publish_state(info, first, "", meta)
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, second, "", meta)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/1708"]
        self.assertGreater(
            len(state_calls), 0, f"Δ{abs(second - first)} >= threshold {change}: publish must fire"
        )

    # -- zero threshold (default) ------------------------------------------

    def test_zero_change_threshold_always_publishes(self):
        """change=0 (default for most points) must never suppress — every
        poll publishes, which is the existing behaviour for static sensors."""
        em = _make_em()
        em.bulk_interval = 0
        info = self._entity_info()
        meta = self._metadata(change=0)
        em._process_and_publish_state(info, 100, "", meta)
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, 100, "", meta)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/1708"]
        self.assertGreater(
            len(state_calls), 0, "change=0: same value must still publish on every poll"
        )

    # -- missing field fallback --------------------------------------------

    def test_missing_change_field_defaults_to_zero(self):
        """Metadata without a 'change' key must fall back to 0 (no suppression),
        not raise KeyError."""
        em = _make_em()
        em.bulk_interval = 0
        info = self._entity_info()
        meta = {"variableSize": "s16", "divisor": 10}  # no 'change' key
        em._process_and_publish_state(info, 100, "", meta)
        em.mqtt.publish.reset_mock()
        em._process_and_publish_state(info, 100, "", meta)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/1708"]
        self.assertGreater(len(state_calls), 0)


class TestPublishDeviceModesEarlyReturn(unittest.TestCase):
    """_publish_device_modes returns early when api_consecutive_failures > 0."""

    def test_returns_early_on_api_failure(self):
        from nibe_ha_integration import _publish_device_modes

        em = MagicMock()
        pub = MagicMock()
        em.api_consecutive_failures = 1
        _publish_device_modes(em, pub)
        pub.publish_device_modes.assert_not_called()


class TestUpdateEntityStateButtonEarlyReturn(unittest.TestCase):
    """_update_entity_state returns early for button entities (just publishes online)."""

    def test_button_publishes_online_and_returns(self):
        em = _make_em()
        entity_info = {
            "point_id": 100,
            "entity_type": "button",
            "availability_topic": "nibe/avail/100",
            "state_topic": "nibe/state/100",
            "command_topic": None,
            "point_data": {},
        }
        em.bulk_data[100] = {
            "raw_value": 0,
            "string_value": "",
            "is_ok": True,
            "metadata": {},
            "title": "Test",
        }
        em._update_entity_state(entity_info)
        em.mqtt.publish.assert_called_once_with("nibe/avail/100", "online", retain=True)


class TestUpdateEntityStateNoValueMappingsDivisorPath(unittest.TestCase):
    """_update_entity_state falls through to divisor path when no value_mappings."""

    def test_no_value_mappings_uses_divisor(self):
        em = _make_em()
        entity_info = {
            "point_id": 200,
            "entity_type": "number",
            "availability_topic": "nibe/avail/200",
            "state_topic": "nibe/state/200",
            "command_topic": None,
            "point_data": {},
        }
        em.bulk_data[200] = {
            "raw_value": 250,
            "string_value": "",
            "is_ok": True,
            "metadata": {"variableSize": "s16", "divisor": 10},
            "title": "Test",
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/200"]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "25")

    def test_divisor_key_absent_defaults_to_one(self):
        """metadata entirely missing 'divisor' must default to 1 (raw value
        unscaled) — a wrong default (e.g. 2) would silently halve every
        number/sensor state value for firmware that omits the field."""
        em = _make_em()
        entity_info = {
            "point_id": 201,
            "entity_type": "number",
            "availability_topic": "nibe/avail/201",
            "state_topic": "nibe/state/201",
            "command_topic": None,
            "point_data": {},
        }
        em.bulk_data[201] = {
            "raw_value": 250,
            "string_value": "",
            "is_ok": True,
            "metadata": {"variableSize": "s16"},
            "title": "Test",
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/201"]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "250")

    def _active_entity(self, em, entity_info):
        from contextlib import contextmanager

        @contextmanager
        def ctx():
            em.active_entities_by_id[entity_info["point_id"]] = entity_info
            em.mqtt_enabled_points.add(entity_info["point_id"])
            try:
                yield
            finally:
                em.active_entities_by_id.pop(entity_info["point_id"], None)
                em.mqtt_enabled_points.discard(entity_info["point_id"])

        return ctx()


class TestUpdateEntityStateMissingStateTopicWarningDedup(unittest.TestCase):
    """A point with no state_topic must warn once (not on every poll) —
    dedup tracked via self._missing_state_topic_warned."""

    def _entity_info(self, point_id):
        return {
            "point_id": point_id,
            "entity_type": "sensor",
            "availability_topic": f"nibe/avail/{point_id}",
            "state_topic": None,  # deliberately missing
            "command_topic": None,
            "point_data": {},
        }

    def _bulk(self, point_id):
        return {
            "raw_value": 5,
            "string_value": "",
            "is_ok": True,
            "metadata": {"variableSize": "s16", "divisor": 1},
            "title": "Test",
        }

    def test_first_occurrence_warns_and_is_recorded(self):
        em = _make_em()
        point_id = 500
        entity_info = self._entity_info(point_id)
        em.bulk_data[point_id] = self._bulk(point_id)
        with self.assertLogs("nibe.entities", level="WARNING") as cm:
            em.active_entities_by_id[point_id] = entity_info
            em.mqtt_enabled_points.add(point_id)
            em._update_entity_state(entity_info)
        self.assertTrue(any("no state_topic" in m for m in cm.output))
        self.assertIn(point_id, em._missing_state_topic_warned)

    def test_second_occurrence_does_not_re_warn(self):
        em = _make_em()
        point_id = 501
        entity_info = self._entity_info(point_id)
        em.bulk_data[point_id] = self._bulk(point_id)
        em.active_entities_by_id[point_id] = entity_info
        em.mqtt_enabled_points.add(point_id)
        em._update_entity_state(entity_info)  # first call: warns
        with self.assertNoLogs("nibe.entities", level="WARNING"):
            em._update_entity_state(entity_info)  # second call: must not re-warn


class TestUpdateEntityStateAbsentNoPostWrite(unittest.TestCase):
    """When _post_write_active is False, absent points are simply disabled."""

    def test_absent_point_disables_entity(self):
        em = _make_em()
        em.post_write_active = False
        point_id = 9999
        em.mqtt_enabled_points.add(point_id)
        entity_info = {
            "point_id": point_id,
            "entity_type": "sensor",
            "availability_topic": "nibe/avail/9999",
            "state_topic": "nibe/state/9999",
        }
        with patch.object(em, "disable_entity") as mock_disable:
            em._update_entity_state(entity_info)
        mock_disable.assert_called_once_with(point_id, remove_from_wanted=False)

    def test_absent_point_is_discarded_from_baseline_point_ids(self):
        """A point that disappears outside a post-write scan must be removed
        from baseline_point_ids — otherwise, if it was ever indexed as a
        static point (e.g. it first appeared before its real controlling
        switch/select was learned), it would permanently fail the
        `point_id not in self.baseline_point_ids` appearance-detection guard
        in _fetch_bulk_data and could never be routed through the dynamic-
        learning path again, even after a correct HA-driven write to its
        controlling point re-opens a scan window."""
        em = _make_em()
        em.post_write_active = False
        point_id = 9999
        em.mqtt_enabled_points.add(point_id)
        em.baseline_point_ids.add(point_id)
        entity_info = {
            "point_id": point_id,
            "entity_type": "sensor",
            "availability_topic": "nibe/avail/9999",
            "state_topic": "nibe/state/9999",
        }
        with patch.object(em, "disable_entity"):
            em._update_entity_state(entity_info)
        self.assertNotIn(point_id, em.baseline_point_ids)

    def test_absent_point_stays_wanted_and_reappears_on_next_fetch(self):
        """Regression test for GitHub issue #21: a user-enabled point that
        disappears from bulk data outside a post-write scan (e.g. flipped by
        the controller itself, not tracked as a dynamic child) must stay in
        _wanted_points across the automatic disable, then be re-enabled the
        moment it reappears in a later bulk fetch."""
        em = _make_em()
        em.post_write_active = False
        point_id = 9999
        em.mqtt_enabled_points.add(point_id)
        em._wanted_points.add(point_id)
        entity_info = {
            "point_id": point_id,
            "entity_type": "sensor",
            "availability_topic": "nibe/avail/9999",
            "state_topic": "nibe/state/9999",
        }
        em._update_entity_state(entity_info)
        self.assertIn(point_id, em._wanted_points)
        self.assertNotIn(point_id, em.mqtt_enabled_points)

        with patch.object(em, "_enable_entity_locked", return_value=True) as mock_enable:
            em._reconcile_wanted_points({point_id})
        mock_enable.assert_called_once_with(point_id)


class TestUpdateEntityStateValueMappingSelfHealing(unittest.TestCase):
    """_update_entity_state self-healing value_mapping paths."""

    def _active_entity(self, em, entity_info):
        from contextlib import contextmanager

        @contextmanager
        def ctx():
            em.active_entities_by_id[entity_info["point_id"]] = entity_info
            em.mqtt_enabled_points.add(entity_info["point_id"])
            try:
                yield
            finally:
                em.active_entities_by_id.pop(entity_info["point_id"], None)
                em.mqtt_enabled_points.discard(entity_info["point_id"])

        return ctx()

    def test_value_mapping_written_back_into_entity_info_on_cache_miss(self):
        """Self-healing: absent value_mapping triggers get_value_mapping() and
        writes the result back so subsequent polls avoid the lookup.
        Uses point 3745 (language select, MODBUS_HOLDING_REGISTER).
        """
        em = _make_em()
        entity_info = {
            "point_id": 3745,
            "entity_type": "select",
            "availability_topic": "nibe/avail/3745",
            "state_topic": "nibe/state/3745",
            "command_topic": "nibe/cmd/3745",
            "point_data": {},
        }
        em.bulk_data[3745] = {
            "raw_value": 9,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "decimal": 0,
                "unit": "",
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
                "modbusRegisterID": 3745,
                "isWritable": True,
                "minValue": 0,
                "maxValue": 23,
            },
            "title": "Language",
        }
        self.assertNotIn("value_mapping", entity_info)
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertIn(
            "value_mapping", entity_info, "value_mapping must be written back after a cache miss"
        )
        self.assertIsInstance(entity_info["value_mapping"], dict)
        self.assertIn(9, entity_info["value_mapping"])
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/3745"]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "Nederlands")

    def test_select_uses_real_point_data_not_default_for_description_mapping(self):
        """When no manual mapping exists for the point, get_value_mapping()
        must fall back to parsing entity_info['point_data']['description']
        — using the empty-dict .get() default instead of the real point_data
        would silently lose the description and the enum label."""
        em = _make_em()
        point_id = 77771  # not in the manual mapping table
        entity_info = {
            "point_id": point_id,
            "entity_type": "select",
            "availability_topic": f"nibe/avail/{point_id}",
            "state_topic": f"nibe/state/{point_id}",
            "command_topic": f"nibe/cmd/{point_id}",
            "point_data": {"description": "0 = Off, 1 = Auto"},
        }
        em.bulk_data[point_id] = {
            "raw_value": 1,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "decimal": 0,
                "unit": "",
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
                "modbusRegisterID": point_id,
                "isWritable": True,
                "minValue": 0,
                "maxValue": 1,
            },
            "title": "Test select",
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        state_calls = [
            c for c in em.mqtt.publish.call_args_list if c.args[0] == f"nibe/state/{point_id}"
        ]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "Auto")

    def test_sensor_uses_real_point_data_not_default_for_description_mapping(self):
        """Same guarantee as the select case, for the 'sensor' entity_type
        branch's own get_value_mapping() call and value_mapping cache
        write-back."""
        em = _make_em()
        point_id = 77772  # not in the manual mapping table
        entity_info = {
            "point_id": point_id,
            "entity_type": "sensor",
            "availability_topic": f"nibe/avail/{point_id}",
            "state_topic": f"nibe/state/{point_id}",
            "command_topic": None,
            "point_data": {"description": "0 = Off, 1 = Auto"},
        }
        em.bulk_data[point_id] = {
            "raw_value": 1,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "decimal": 0,
                "unit": "",
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "modbusRegisterID": point_id,
                "isWritable": False,
                "minValue": 0,
                "maxValue": 1,
            },
            "title": "Test sensor",
        }
        self.assertNotIn("value_mapping", entity_info)
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertIn("value_mapping", entity_info)
        self.assertEqual(entity_info["value_mapping"], {0: "Off", 1: "Auto"})
        state_calls = [
            c for c in em.mqtt.publish.call_args_list if c.args[0] == f"nibe/state/{point_id}"
        ]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "Auto")

    def test_sensor_uses_the_real_point_id_for_the_manual_mapping_lookup(self):
        """The 'sensor' branch's get_value_mapping(point_id, ...) call must
        use this entity's own point_id — a mutation to that argument (e.g.
        None) would make _lookup_manual_mapping miss point 1762's real
        manual VALUE_MAPPINGS entry, silently falling back to
        parse_description_mapping instead. The existing 'sensor' coverage
        (test_sensor_uses_real_point_data_not_default_for_description_
        mapping) deliberately uses a point_id NOT in the manual table, so a
        None point_id there misses the manual lookup exactly the same way
        the real point_id does — it can't distinguish this mutation. Point
        1762 IS in VALUE_MAPPINGS['input'], mirroring how the 'select'
        branch's equivalent test above (point 3745) is structured."""
        em = _make_em()
        entity_info = {
            "point_id": 1762,
            "entity_type": "sensor",
            "availability_topic": "nibe/avail/1762",
            "state_topic": "nibe/state/1762",
            "point_data": {},
        }
        em.bulk_data[1762] = {
            "raw_value": 20,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "decimal": 0,
                "unit": "",
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "modbusRegisterID": 1762,
                "isWritable": False,
                "minValue": 0,
                "maxValue": 30,
            },
            "title": "Test sensor",
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertEqual(
            entity_info.get("value_mapping"), {10: "Off", 20: "Opening", 30: "Closing"}
        )
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/1762"]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "Opening")

    def test_select_no_mapping_falls_through_to_raw_str(self):
        """select where get_value_mapping() returns None falls through to
        str(raw_value) — branch 1271→1278 / 1247→1254."""
        em = _make_em()
        entity_info = {
            "point_id": 9999,
            "entity_type": "select",
            "availability_topic": "nibe/avail/9999",
            "state_topic": "nibe/state/9999",
            "command_topic": "nibe/cmd/9999",
            "point_data": {},
        }
        em.bulk_data[9999] = {
            "raw_value": 2,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "decimal": 0,
                "unit": "",
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
                "modbusRegisterID": 9999,
                "isWritable": True,
                "minValue": 0,
                "maxValue": 3,
            },
            "title": "Unknown select",
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertNotIn("value_mapping", entity_info)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/9999"]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "2")

    def test_select_raw_value_not_in_mapping_falls_through_to_raw_str(self):
        """select where mapping exists but raw_value not in it — branch 1275→1278."""
        em = _make_em()
        entity_info = {
            "point_id": 3745,
            "entity_type": "select",
            "availability_topic": "nibe/avail/3745",
            "state_topic": "nibe/state/3745",
            "command_topic": "nibe/cmd/3745",
            "point_data": {},
        }
        em.bulk_data[3745] = {
            "raw_value": 99,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "decimal": 0,
                "unit": "",
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
                "modbusRegisterID": 3745,
                "isWritable": True,
                "minValue": 0,
                "maxValue": 23,
            },
            "title": "Language",
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertIn("value_mapping", entity_info)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/3745"]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "99")

    def test_sensor_no_mapping_falls_through_to_divisor(self):
        """sensor where get_value_mapping() returns None falls through to
        apply_divisor — branch 1284→1291."""
        em = _make_em()
        entity_info = {
            "point_id": 9998,
            "entity_type": "sensor",
            "availability_topic": "nibe/avail/9998",
            "state_topic": "nibe/state/9998",
            "command_topic": None,
            "point_data": {},
        }
        em.bulk_data[9998] = {
            "raw_value": 123,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 10,
                "decimal": 1,
                "unit": "°C",
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "modbusRegisterID": 9998,
                "isWritable": False,
                "minValue": -400,
                "maxValue": 400,
            },
            "title": "Unknown sensor",
        }
        with self._active_entity(em, entity_info):
            em._update_entity_state(entity_info)
        self.assertNotIn("value_mapping", entity_info)
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/9998"]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "12.3")


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
            "point_id": 999,
            "entity_type": "sensor",
            "availability_topic": "nibe/avail/999",
            "state_topic": "nibe/state/999",
        }
        em.active_entities_by_id[999] = entity_info
        # bulk_data deliberately does not contain 999
        with patch.object(em, "disable_entity") as mock_disable:
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
        em.post_write_active = True
        # Make point known-dynamic but NOT active (i.e. in the absent set)
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value={500})
        em.active_dynamic_points = set()  # 500 is absent: in known - active
        entity_info = {
            "point_id": 500,
            "entity_type": "sensor",
            "availability_topic": "nibe/avail/500",
            "state_topic": "nibe/state/500",
        }
        em.active_entities_by_id[500] = entity_info
        # bulk_data does not contain 500
        with patch.object(em, "_publish_dynamic_changes") as mock_pub_dyn:
            em._update_entity_state(entity_info)
        mock_pub_dyn.assert_not_called()


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
            em.active_entities_by_id[entity_info["point_id"]] = entity_info
            em.mqtt_enabled_points.add(entity_info["point_id"])
            try:
                yield
            finally:
                em.active_entities_by_id.pop(entity_info["point_id"], None)
                em.mqtt_enabled_points.discard(entity_info["point_id"])

        return ctx()

    def test_select_pre_cached_mapping_skips_lookup(self):
        """1271→1278: mapping already in entity_info → is None False branch."""
        em = _make_em()
        entity_info = {
            "point_id": 3745,
            "entity_type": "select",
            "availability_topic": "nibe/avail/3745",
            "state_topic": "nibe/state/3745",
            "command_topic": "nibe/cmd/3745",
            "point_data": {},
            "value_mapping": {9: "Nederlands"},  # pre-cached from previous poll
        }
        em.bulk_data[3745] = {
            "raw_value": 9,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "decimal": 0,
                "unit": "",
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
                "modbusRegisterID": 3745,
                "isWritable": True,
                "minValue": 0,
                "maxValue": 23,
            },
            "title": "Language",
        }
        with (
            self._active_entity(em, entity_info),
            patch("nibe_entity_manager.get_value_mapping") as mock_gvm,
        ):
            em._update_entity_state(entity_info)
        # The cached mapping must be used; get_value_mapping must NOT be called
        mock_gvm.assert_not_called()
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/3745"]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "Nederlands")

    def test_sensor_pre_cached_mapping_skips_lookup(self):
        """1284→1291: mapping already in entity_info → is None False branch."""
        em = _make_em()
        entity_info = {
            "point_id": 9998,
            "entity_type": "sensor",
            "availability_topic": "nibe/avail/9998",
            "state_topic": "nibe/state/9998",
            "command_topic": None,
            "point_data": {},
            "value_mapping": {5: "Cool"},  # pre-cached
        }
        em.bulk_data[9998] = {
            "raw_value": 5,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "decimal": 0,
                "unit": "",
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "modbusRegisterID": 9998,
                "isWritable": False,
                "minValue": 0,
                "maxValue": 10,
            },
            "title": "Sensor with mapping",
        }
        with (
            self._active_entity(em, entity_info),
            patch("nibe_entity_manager.get_value_mapping") as mock_gvm,
        ):
            em._update_entity_state(entity_info)
        mock_gvm.assert_not_called()
        state_calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == "nibe/state/9998"]
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], "Cool")


class TestUpdateEntityStatePendingWriteEviction(unittest.TestCase):
    """Covers the stale-pending-write eviction branch and the
    write-confirmed-by-API branch inside _update_entity_state.

    Mutants surviving here: `pending_entry.get('timestamp', 0)` -> default 1
    (mutant 30), `self.bulk_data.get(point_id, {})` -> default None
    (mutant 60), and `pending_entry = None` -> `pending_entry = ""` after a
    confirmed write (mutant 92).
    """

    def _entity_info(self, point_id):
        return {
            "point_id": point_id,
            "entity_type": "sensor",
            "availability_topic": f"nibe/avail/{point_id}",
            "state_topic": f"nibe/state/{point_id}",
        }

    def test_stale_pending_write_missing_timestamp_is_evicted(self):
        """mutant 30: pending_entry.get('timestamp', 0) -> default 1.

        With no 'timestamp' key and time.time() mocked to exactly
        _STALE_WRITE_AGE_S + 1, the real default (0) makes age = 61,
        which is > 60 -> stale -> evicted -> normal publishing resumes.
        The mutant's default (1) makes age = 60, which is NOT > 60 ->
        the entry survives -> the function returns early without
        publishing state.
        """
        import nibe_entity_manager as nem

        em = _make_em()
        point_id = 700
        # Deliberately no 'timestamp' key so the .get(...) default is used.
        # 'value' (999) deliberately does NOT match the bulk raw_value (5)
        # below, so the write-confirmed-by-API branch cannot also clear the
        # pending entry — only the stale-age eviction can, isolating the
        # mutation under test.
        em.pending_writes[point_id] = {"cmd_id": "abc", "value": 999}
        em.bulk_data[point_id] = {
            "raw_value": 5,
            "string_value": "",
            "is_ok": True,
            "metadata": {"divisor": 1},
        }
        info = self._entity_info(point_id)
        with patch.object(nem.time, "time", return_value=nem._STALE_WRITE_AGE_S + 1):
            em._update_entity_state(info)
        state_calls = [
            c for c in em.mqtt.publish.call_args_list if c.args[0] == f"nibe/state/{point_id}"
        ]
        self.assertTrue(
            state_calls,
            "Real default of 0 must make the entry stale (age=61>60), "
            "evicting it and allowing normal state publishing to resume",
        )
        self.assertNotIn(point_id, em.pending_writes)

    def test_bulk_data_missing_point_during_pending_check_no_crash(self):
        """mutant 60: self.bulk_data.get(point_id, {}) -> default None.

        A non-stale pending write for a point currently absent from
        bulk_data must not crash: bulk_raw resolves to None via the {}
        default, and the function should simply fall through to the
        'point_id not in self.bulk_data' branch below without raising.
        """
        import time as _time

        em = _make_em()
        point_id = 701
        em.pending_writes[point_id] = {
            "cmd_id": "xyz",
            "value": 9,
            "timestamp": _time.time(),
        }
        # point_id deliberately absent from em.bulk_data
        em.mqtt_enabled_points.add(point_id)
        em.post_write_active = False
        info = self._entity_info(point_id)
        with patch.object(em, "disable_entity") as mock_disable:
            # Must not raise AttributeError (None has no .get) — mutant 60 crashes here.
            em._update_entity_state(info)
        # Original 'value' (9) never matched bulk_raw (None) -> pending
        # write remains active -> function returns before reaching the
        # absent-point/disable branch below.
        mock_disable.assert_not_called()
        self.assertIn(point_id, em.pending_writes)

    def test_confirmed_write_pending_entry_cleared_resumes_publishing(self):
        """mutant 92: pending_entry = None -> pending_entry = "" after a
        confirmed write. `"" is not None` is True, so the mutant keeps
        `pending` True and the function returns early, never publishing
        state. The real code sets None, `pending` becomes False, and
        publishing resumes.
        """
        import time as _time

        em = _make_em()
        point_id = 702
        em.pending_writes[point_id] = {
            "cmd_id": "confirmed",
            "value": 42,
            "timestamp": _time.time(),
        }
        em.bulk_data[point_id] = {
            "raw_value": 42,
            "string_value": "",
            "is_ok": True,
            "metadata": {"divisor": 1},
        }
        info = self._entity_info(point_id)
        em._update_entity_state(info)
        state_calls = [
            c for c in em.mqtt.publish.call_args_list if c.args[0] == f"nibe/state/{point_id}"
        ]
        self.assertTrue(
            state_calls,
            "A confirmed write must clear the pending entry and let normal state publishing resume",
        )
        self.assertNotIn(point_id, em.pending_writes)


class TestUpdateEntityStatePostWriteDynamicDisappearance(unittest.TestCase):
    """Covers the branch where an absent point during a post-write scan is
    routed through _publish_dynamic_changes (not disable_entity).

    Kills mutants that corrupt the arguments passed through: the snapshot
    of _post_write_controlling_point (mutant 98/113), the point removed
    from baseline_point_ids (mutant 110), and the empty-list first arg
    to _publish_dynamic_changes (mutant 111).
    """

    def test_absent_during_post_write_routes_through_publish_dynamic_changes(self):
        em = _make_em()
        point_id = 800
        other_point = 801
        controlling_point = 12345  # distinctive, independent of point_id/other_point
        em.baseline_point_ids.add(point_id)
        em.baseline_point_ids.add(other_point)
        em.mqtt_enabled_points.add(point_id)
        em.post_write_active = True
        em._post_write_controlling_point = controlling_point
        # point_id is not a known dynamic point at all, so
        # (known_dynamic - active_dynamic) does not contain it, and the
        # "not in" guard is True -> routes through _publish_dynamic_changes.
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        em.active_dynamic_points = set()
        info = {
            "point_id": point_id,
            "entity_type": "sensor",
            "availability_topic": f"nibe/avail/{point_id}",
            "state_topic": f"nibe/state/{point_id}",
        }
        em.active_entities_by_id[point_id] = info
        # point_id deliberately absent from em.bulk_data
        with patch.object(em, "_publish_dynamic_changes") as mock_pub_dyn:
            em._update_entity_state(info)
        mock_pub_dyn.assert_called_once_with([], {point_id}, controlling_point)
        self.assertNotIn(
            point_id,
            em.baseline_point_ids,
            "The disappearing point must be discarded from baseline_point_ids",
        )
        self.assertIn(
            other_point,
            em.baseline_point_ids,
            "Only the disappearing point should be discarded, not others",
        )


class TestUpdateEntityStateDataFlowFields(unittest.TestCase):
    """Covers the fields extracted from bulk_data and forwarded to
    _process_and_publish_state (raw_value, string_value, metadata).

    Kills mutants 143 (string_value -> None), 148/150 (metadata default
    {} -> None / missing), and 155 (string_value arg -> None).
    """

    def test_string_value_flows_through_to_text_entity(self):
        """A 'text' entity publishes string_value verbatim — if
        string_value were replaced with None, publish would receive None
        instead of the real firmware string."""
        em = _make_em()
        point_id = 900
        entity_info = {
            "point_id": point_id,
            "entity_type": "text",
            "availability_topic": f"nibe/avail/{point_id}",
            "state_topic": f"nibe/state/{point_id}",
        }
        em.bulk_data[point_id] = {
            "raw_value": 0,
            "string_value": "Firmware string XYZ",
            "is_ok": True,
            "metadata": {},
        }
        em._update_entity_state(entity_info)
        em.mqtt.publish.assert_any_call(
            f"nibe/state/{point_id}", "Firmware string XYZ", retain=True
        )

    def test_missing_metadata_key_defaults_to_empty_dict_not_none(self):
        """When bulk_data lacks a 'metadata' key entirely, the default {}
        must be used so metadata.get(...) calls inside
        _process_and_publish_state don't raise AttributeError on None."""
        em = _make_em()
        point_id = 901
        entity_info = {
            "point_id": point_id,
            "entity_type": "sensor",
            "availability_topic": f"nibe/avail/{point_id}",
            "state_topic": f"nibe/state/{point_id}",
        }
        em.bulk_data[point_id] = {
            "raw_value": 100,
            "string_value": "",
            "is_ok": True,
            # deliberately no 'metadata' key
        }
        # Must not raise — mutant's None default would crash on metadata.get('variableSize', '')
        em._update_entity_state(entity_info)
        em.mqtt.publish.assert_any_call(f"nibe/state/{point_id}", "100", retain=True)
