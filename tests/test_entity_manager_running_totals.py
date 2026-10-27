"""
test_entity_manager_running_totals.py
=====================================
A running total (published total_increasing: energy, operating hours,
compressor starts) that drops below its last value is published unavailable
instead (EntityManager._running_total_dropped, GitHub issue #132).
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import nibe_entity_manager
from conftest import _make_em

POINT = 28393  # Tot. consumption, kWh, divisor 10
STATE = f"nibe/state/{POINT}"
AVAILABLE = f"nibe/available/{POINT}"
METADATA = {"variableSize": "s32", "divisor": 10, "unit": "kWh"}


def _info(running_total: bool = True) -> dict:
    return {
        "point_id": POINT,
        "entity_type": "sensor",
        "state_topic": STATE,
        "availability_topic": AVAILABLE,
        "point_data": {"display_title": "Tot. consumption"},
        "is_running_total": running_total,
        "value_mapping": {},
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.em = _make_em()
        self.now = 1_000_000.0

    def _publish(self, raw, info=None, metadata=METADATA):
        self.em.mqtt.publish.reset_mock()
        with patch("nibe_entity_manager.time.time", return_value=self.now):
            self.em._process_and_publish_state(info or _info(), raw, "", metadata)
        return self.em.mqtt.publish.call_args_list

    def _state_published(self, calls, value):
        return call(STATE, value, retain=True) in calls

    def _offline(self, calls):
        return call(AVAILABLE, "offline", retain=True) in calls

    def _unavailable(self):
        """Availability is only sent when it changes (_availability_sent)."""
        return self.em._availability_sent.get(AVAILABLE) == "offline"


class TestRunningTotalDrop(_Base):
    def test_rising_values_are_published(self):
        self.assertTrue(self._state_published(self._publish(20639), "2063.9"))
        self.assertTrue(self._state_published(self._publish(20641), "2064.1"))

    def test_a_drop_is_published_unavailable_not_as_a_value(self):
        """The restart placeholder 0 must not reach Home Assistant, which
        would count the whole total again when the real value returns."""
        self._publish(20641)
        calls = self._publish(0)
        self.assertTrue(self._offline(calls))
        self.assertFalse(self._state_published(calls, "0"))

    def test_the_real_value_back_is_published_again(self):
        self._publish(20641)
        self._publish(0)
        calls = self._publish(20643)
        self.assertIn(call(AVAILABLE, "online", retain=True), calls)
        self.assertTrue(self._state_published(calls, "2064.3"))

    def test_the_same_value_is_not_a_drop(self):
        self._publish(20641)
        self.assertFalse(self._offline(self._publish(20641)))

    def test_a_lower_value_that_lasts_is_accepted_as_a_reset(self):
        self._publish(20641)
        self._publish(0)
        self.now += nibe_entity_manager._RUNNING_TOTAL_RESET_S - 1
        calls = self._publish(3)
        self.assertTrue(self._unavailable())
        self.assertFalse(self._state_published(calls, "0.3"))
        self.now += 1
        with patch.object(self.em, "_save_running_totals") as save:
            calls = self._publish(4)
        self.assertTrue(self._state_published(calls, "0.4"))
        save.assert_called_once()
        # From the reset on, the new count is the reference.
        self.assertTrue(self._state_published(self._publish(5), "0.5"))

    def test_the_reset_time_counts_from_the_first_lower_value(self):
        self._publish(20641)
        self._publish(0)
        self._publish(20642)  # back: the next drop starts afresh
        self.now += nibe_entity_manager._RUNNING_TOTAL_RESET_S
        calls = self._publish(0)
        self.assertTrue(self._unavailable())
        self.assertFalse(self._state_published(calls, "0"))

    def test_compared_with_the_divisor_applied(self):
        """A firmware update that only changes a counter's divisor lowers the
        raw value but not the total."""
        self._publish(20640)
        calls = self._publish(2064, metadata={**METADATA, "divisor": 1})
        self.assertFalse(self._offline(calls))
        self.assertFalse(self._unavailable())

    def test_not_a_running_total(self):
        """A countdown (time to defrosting, time to start) is a measurement:
        every value passes, down included."""
        self._publish(20641, info=_info(running_total=False))
        calls = self._publish(0, info=_info(running_total=False))
        self.assertFalse(self._offline(calls))
        self.assertTrue(self._state_published(calls, "0"))

    def test_the_first_value_after_a_start_without_a_reference(self):
        self.assertTrue(self._state_published(self._publish(0), "0"))


class TestRunningTotalsOnDisk(_Base):
    def setUp(self):
        super().setUp()
        fd, self.path = tempfile.mkstemp()
        os.close(fd)

    def tearDown(self):
        os.unlink(self.path)

    def test_a_drop_right_after_a_restart(self):
        """A power cut restarts the bridge with the controller: its first
        value can be the placeholder, with nothing in memory to compare."""
        self._publish(20641)
        self.em._save_running_totals(self.path)
        restarted = _make_em()
        restarted._load_running_totals(self.path)
        self.em = restarted
        self.assertTrue(self._offline(self._publish(0)))

    def test_saved_at_most_every_hour(self):
        self._publish(20641)
        with (
            patch("nibe_entity_manager.time.time", return_value=self.now),
            patch.object(self.em, "_fetch_bulk_data", return_value=True),
            patch.object(self.em, "_save_running_totals") as save,
        ):
            self.em.active_entities_by_id = {POINT: _info()}
            self.em.bulk_data[POINT] = {
                "raw_value": 20641,
                "is_ok": True,
                "metadata": METADATA,
                "string_value": "",
            }
            self.em.update_all_states()
            self.em._running_totals_saved_at = self.now
            self.em.update_all_states(force=True)
        save.assert_called_once()

    def test_loaded_at_startup_before_the_first_fetch(self):
        with open(nibe_entity_manager._RUNNING_TOTALS_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({str(POINT): 2064.1}))

        def first_fetch(**_kwargs):
            self.assertEqual(self.em._running_totals, {POINT: 2064.1})
            return False

        with patch.object(self.em, "_fetch_bulk_data", side_effect=first_fetch) as fetch:
            self.em.discover_points()
        fetch.assert_called_once()

    def test_the_file_holds_the_values(self):
        self._publish(20641)
        self.em._save_running_totals(self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {str(POINT): 2064.1})

    def test_nothing_saved_when_empty(self):
        self.em._save_running_totals(self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "")

    def test_a_missing_or_broken_file(self):
        self.em._load_running_totals("/nonexistent/running_totals.json")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json")
        self.em._load_running_totals(self.path)
        self.assertEqual(self.em._running_totals, {})


class TestDiscoveryMarksRunningTotals(unittest.TestCase):
    """The flag follows the discovery config's state_class, through the real
    classification and publisher."""

    def _entity_info(self, point_id, title, unit):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher

        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        em = _make_em()
        em._pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt,
            device_info={"identifiers": ["nibe_test"], "name": "Test"},
            device_id="nibe_test",
            device_name="Test Device",
        )
        em._api.fetch_bulk_points.return_value = {
            str(point_id): {
                "title": title,
                "description": "",
                "value": {"integerValue": 20641, "stringValue": "", "isOk": True},
                "metadata": {
                    "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                    "variableType": "integer",
                    "variableSize": "s32",
                    "minValue": 0,
                    "maxValue": 0,
                    "divisor": 10,
                    "decimal": 1,
                    "unit": unit,
                    "shortUnit": unit,
                    "isWritable": False,
                    "change": 1,
                },
            }
        }
        self.assertTrue(em._fetch_bulk_data(detect_changes=False))
        return em._pub.publish_entity_discovery(em.all_points_by_id[point_id], em.bulk_data)

    def test_an_energy_total(self):
        self.assertTrue(self._entity_info(28393, "Tot. consumption", "kWh")["is_running_total"])

    def test_a_countdown(self):
        """Time to start counts down: a measurement, not a running total."""
        info = self._entity_info(2501, "Compressor, time to start (EB101-EP14)", "min")
        self.assertFalse(info["is_running_total"])


if __name__ == "__main__":
    unittest.main()
