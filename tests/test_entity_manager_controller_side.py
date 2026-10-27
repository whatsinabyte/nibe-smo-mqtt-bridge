"""
test_entity_manager_controller_side.py
======================================
Learning the points a switch shows when it is changed on the controller
itself, not through Home Assistant (EntityManager._controller_side_change and
its use in _fetch_bulk_data).
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import unittest
from unittest.mock import patch

from conftest import _make_em

SWITCH = 1001  # a candidate controlling switch
OTHER = 1002  # a second candidate
STATIC = 2001  # an always-served point
NEW = 3001  # a point the switch makes the controller serve


def _point(value: int, writable: bool = True) -> dict:
    return {
        "title": f"Point value {value}",
        "description": "",
        "metadata": {
            "modbusRegisterType": "MODBUS_HOLDING_REGISTER"
            if writable
            else "MODBUS_INPUT_REGISTER",
            "isWritable": writable,
            "minValue": 0,
            "maxValue": 1 if writable else 100,
            "variableType": "integer",
            "variableSize": "u8",
            "divisor": 1,
            "decimal": 0,
            "unit": "",
        },
        "value": {"integerValue": value, "stringValue": "", "isOk": True},
    }


class _Base(unittest.TestCase):
    """An EntityManager after initial discovery: SWITCH and OTHER are
    unlearned candidate switches at 0, STATIC is served, NEW is not."""

    def setUp(self):
        from nibe_dynamic_map import DynamicPointEntry

        em = _make_em()
        em.initial_discovery_complete = True
        for pid in (SWITCH, OTHER, STATIC):
            em.bulk_data[pid] = {"raw_value": 0, "metadata": {}}
            em.baseline_point_ids.add(pid)
        em.published_configs = {SWITCH, OTHER, STATIC}
        for pid in (SWITCH, OTHER):
            em.dynamic_point_map._table[pid] = DynamicPointEntry(
                point_id=pid,
                title=f"Switch {pid}",
                entity_type="switch",
                unprocessed_values={0, 1},
            )
        self.em = em

    def _response(self, switch=0, other=0, static=True, new=False) -> dict:
        response = {str(SWITCH): _point(switch), str(OTHER): _point(other)}
        if static:
            response[str(STATIC)] = _point(5, writable=False)
        if new:
            response[str(NEW)] = _point(7, writable=False)
        return response


class TestControllerSideChange(_Base):
    def test_one_candidate_changed_to_an_unlearned_value(self):
        self.assertEqual(self.em._controller_side_change(self._response(switch=1)), (SWITCH, 0, 1))

    def test_nothing_changed(self):
        self.assertIsNone(self.em._controller_side_change(self._response()))

    def test_each_point_and_value_opens_a_window_once(self):
        """A switch the controller flips by itself must not keep the poll
        loop at 5 s: each (point, value) gets one window per run."""
        self.assertIsNotNone(self.em._controller_side_change(self._response(switch=1)))
        self.assertIsNone(self.em._controller_side_change(self._response(switch=1)))

    def test_two_candidates_changed(self):
        """Which one shows the new points can't be told."""
        self.assertIsNone(self.em._controller_side_change(self._response(switch=1, other=1)))

    def test_learned_value(self):
        """A learned pair takes the known-dynamic path; no window needed."""
        entry = self.em.dynamic_point_map._table[SWITCH]
        entry.unprocessed_values.discard(1)
        entry.processed_values.add(1)
        self.assertIsNone(self.em._controller_side_change(self._response(switch=1)))

    def test_pending_ha_write(self):
        """An HA write shows its own change in the next poll and opens its
        own window."""
        self.em.pending_writes[SWITCH] = {"value": 1}
        self.assertIsNone(self.em._controller_side_change(self._response(switch=1)))

    def test_queued_ha_write(self):
        self.em._write_queue_depth = 1
        self.assertIsNone(self.em._controller_side_change(self._response(switch=1)))

    def test_partial_point_list(self):
        """A restarting controller serves a partial list with odd values; a
        point served before and missing now means don't trust this poll."""
        self.assertIsNone(self.em._controller_side_change(self._response(switch=1, static=False)))

    def test_a_known_dynamic_point_missing_is_not_a_partial_list(self):
        from nibe_dynamic_map import DynamicPointEntry

        self.em.dynamic_point_map._table[4001] = DynamicPointEntry(
            point_id=4001,
            title="Switch 4001",
            entity_type="switch",
            processed_values={0, 1},
            is_controlling=True,
            dynamic_points_by_value={0: [], 1: [4002]},
        )
        self.em.published_configs.add(4002)
        self.assertEqual(self.em._controller_side_change(self._response(switch=1)), (SWITCH, 0, 1))

    def test_before_initial_discovery(self):
        self.em.initial_discovery_complete = False
        self.assertIsNone(self.em._controller_side_change(self._response(switch=1)))

    def test_a_point_removed_by_firmware_is_no_candidate(self):
        self.em.dynamic_point_map._table[SWITCH].firmware_removed = True
        self.assertIsNone(self.em._controller_side_change(self._response(switch=1)))


class TestFetchBulkDataLearnsControllerSideChange(_Base):
    def _fetch(self, response, detect_changes=False):
        self.em._api.fetch_bulk_points.return_value = response
        with patch.object(self.em, "_publish_dynamic_changes") as publish:
            self.em._fetch_bulk_data(detect_changes=detect_changes)
        return publish

    def test_the_change_opens_a_scan_window_for_the_switch(self):
        self._fetch(self._response(switch=1))
        self.assertTrue(self.em.post_write_active)
        self.assertEqual(self.em._post_write_controlling_point, SWITCH)
        self.assertEqual(self.em._controller_side_point, SWITCH)

    def test_points_arriving_later_in_the_window_are_the_switchs(self):
        """The firmware serves a switch's points about a minute after it
        changes: they arrive in a later poll, inside the window."""
        self._fetch(self._response(switch=1))
        publish = self._fetch(self._response(switch=1, new=True), detect_changes=True)
        publish.assert_called_once()
        new_points, _, controlling = publish.call_args.args
        self.assertEqual([pid for pid, _ in new_points], [NEW])
        self.assertEqual(controlling, SWITCH)

    def test_points_arriving_in_the_same_poll_are_the_switchs(self):
        """With a long poll interval the switch's points can arrive in the
        poll that shows its change — even with no dynamic point known yet,
        which otherwise turns change detection off for the poll."""
        publish = self._fetch(self._response(switch=1, new=True), detect_changes=False)
        publish.assert_called_once()
        new_points, _, controlling = publish.call_args.args
        self.assertEqual([pid for pid, _ in new_points], [NEW])
        self.assertEqual(controlling, SWITCH)

    def test_without_a_change_a_new_point_stays_static(self):
        publish = self._fetch(self._response(new=True), detect_changes=True)
        publish.assert_not_called()
        self.assertFalse(self.em.post_write_active)

    def test_an_open_ha_window_is_kept(self):
        """An HA write's window keeps its own controlling point."""
        with self.em._post_write_lock:
            self.em._post_write_controlling_point = OTHER
            self.em.post_write_active = True
            self.em._post_write_until = 1e12
        publish = self._fetch(self._response(switch=1, new=True), detect_changes=True)
        self.assertEqual(self.em._post_write_controlling_point, OTHER)
        self.assertIsNone(self.em._controller_side_point)
        self.assertEqual(publish.call_args.args[2], OTHER)


class TestChangelogMarksAControllerSideChange(_Base):
    def _triggered_by(self):
        captured = {}
        self.em._update_changelog_history = lambda event: captured.setdefault("event", event)
        self.em.all_points_by_id[SWITCH] = {
            "variableId": SWITCH,
            "display_title": "Activate via API",
            "entity_type": "switch",
            "metadata": {"minValue": 0, "maxValue": 1},
        }
        self.em.bulk_data[SWITCH] = {"raw_value": 1, "metadata": {}}
        point = _point(7, writable=False)
        self.em._publish_dynamic_changes([(NEW, point)], set(), SWITCH)
        return captured["event"]["triggered_by"]

    def test_a_controller_side_window_says_so(self):
        self.em._controller_side_point = SWITCH
        self.assertEqual(self._triggered_by()["changed_on"], "controller")

    def test_an_ha_write_window_does_not(self):
        self.assertNotIn("changed_on", self._triggered_by())


if __name__ == "__main__":
    unittest.main()
