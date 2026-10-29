"""
test_entity_manager_controller_side.py
======================================
Learning the points a switch shows outside the scan window of an HA write:
when it is changed on the controller itself (_controller_side_change), and
when its points arrive after the window ended (_late_arrival), and the
guards both share (_attributable_poll).
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import unittest
from unittest.mock import patch

import nibe_entity_manager
from conftest import _make_em, _set_window

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
        """A learned pair takes the known-dynamic path; no window needed —
        only a late-arrival period, for points it shows that aren't known."""
        entry = self.em.dynamic_point_map._table[SWITCH]
        entry.unprocessed_values.discard(1)
        entry.processed_values.add(1)
        self.assertIsNone(self.em._controller_side_change(self._response(switch=1)))
        self.assertEqual((self.em._late_tail.point, self.em._late_tail.value), (SWITCH, 1))
        self.assertTrue(self.em._late_tail.controller_side)

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
        self.assertEqual(self.em._window.point, SWITCH)
        self.assertTrue(self.em._window.controller_side)

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
            _set_window(self.em, point=OTHER, until=1e12)
        publish = self._fetch(self._response(switch=1, new=True), detect_changes=True)
        self.assertEqual(self.em._window.point, OTHER)
        self.assertFalse(self.em._window.controller_side)
        self.assertEqual(publish.call_args.args[2], OTHER)


class TestChangelogMarksAControllerSideChange(_Base):
    def _triggered_by(self, controller_side=False):
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
        self.em._publish_dynamic_changes(
            [(NEW, point)], set(), SWITCH, controller_side=controller_side
        )
        return captured["event"]["triggered_by"]

    def test_a_controller_side_window_says_so(self):
        self.assertEqual(self._triggered_by(controller_side=True)["changed_on"], "controller")

    def test_an_ha_write_window_does_not(self):
        self.assertNotIn("changed_on", self._triggered_by())


class TestLateArrival(_Base):
    """Points a switch shows that arrive after its scan window ended."""

    def setUp(self):
        super().setUp()
        self.em.bulk_data[SWITCH]["raw_value"] = 1
        self.em._start_late_tail(SWITCH, 1, False, after=0)

    def test_new_points_are_the_switchs(self):
        self.assertEqual(
            self.em._late_arrival(self._response(switch=1, new=True)), (SWITCH, 1, False, [NEW])
        )
        self.assertIsNone(self.em._late_tail)

    def test_nothing_new_keeps_the_period(self):
        self.assertIsNone(self.em._late_arrival(self._response(switch=1)))
        self.assertIsNotNone(self.em._late_tail)

    def test_the_period_ends(self):
        with patch("nibe_entity_manager.time.time", return_value=self.em._late_tail.until + 1):
            self.assertIsNone(self.em._late_arrival(self._response(switch=1, new=True)))
        self.assertIsNone(self.em._late_tail)

    def test_lasts_the_late_arrival_time_plus_a_poll(self):
        with patch("nibe_entity_manager.time.time", return_value=1000.0):
            self.em._start_late_tail(SWITCH, 1, False, after=90)
        self.assertEqual(
            self.em._late_tail.until, 1000 + 90 + nibe_entity_manager._LATE_ARRIVAL_S + 30
        )

    def test_an_expired_period_does_not_judge_the_poll(self):
        """A partial point list after the period ended must not block
        attribution: there is nothing left to protect."""
        with patch("nibe_entity_manager.time.time", return_value=self.em._late_tail.until + 1):
            self.em._late_arrival(self._response(switch=1, static=False, new=True))
        self.assertEqual(self.em._attribution_blocked_until, 0.0)

    def test_the_switchs_own_change_keeps_the_period(self):
        """Only another switch or select changing ends it."""
        self.em.bulk_data[SWITCH]["raw_value"] = 0
        self.assertEqual(
            self.em._late_arrival(self._response(switch=1, new=True)), (SWITCH, 1, False, [NEW])
        )

    def test_the_switch_set_back(self):
        self.assertIsNone(self.em._late_arrival(self._response(switch=0, new=True)))
        self.assertIsNone(self.em._late_tail)

    def test_another_switch_changed(self):
        """Which one shows the new point can't be told any more."""
        self.assertIsNone(self.em._late_arrival(self._response(switch=1, other=1, new=True)))
        self.assertIsNone(self.em._late_tail)

    def test_a_known_dynamic_point_is_not_new(self):
        from nibe_dynamic_map import DynamicPointEntry

        self.em.dynamic_point_map._table[4001] = DynamicPointEntry(
            point_id=4001,
            title="Switch 4001",
            entity_type="switch",
            processed_values={0, 1},
            is_controlling=True,
            dynamic_points_by_value={0: [], 1: [NEW]},
        )
        self.assertIsNone(self.em._late_arrival(self._response(switch=1, new=True)))

    def test_an_indexed_point_is_not_new(self):
        self.em.baseline_point_ids.add(NEW)
        self.assertIsNone(self.em._late_arrival(self._response(switch=1, new=True)))

    def test_a_write_to_another_point_pending(self):
        self.em.pending_writes[OTHER] = {"value": 1}
        self.assertIsNone(self.em._late_arrival(self._response(switch=1, new=True)))
        self.assertIsNotNone(self.em._late_tail)

    def test_the_switchs_own_write_pending(self):
        self.em.pending_writes[SWITCH] = {"value": 1}
        self.assertIsNotNone(self.em._late_arrival(self._response(switch=1, new=True)))

    def test_a_partial_point_list(self):
        """A restarting controller: whatever it serves next is its own."""
        self.assertIsNone(self.em._late_arrival(self._response(switch=1, static=False, new=True)))
        self.assertIsNone(self.em._late_tail)
        self.assertGreater(self.em._attribution_blocked_until, 0)

    def test_a_firmware_version_change(self):
        """New points that come with a firmware update are the firmware's."""
        self.em.bulk_data[2509] = {"raw_value": 1038, "metadata": {}}
        self.em.published_configs.add(2509)
        response = self._response(switch=1, new=True)
        response["2509"] = _point(1039, writable=False)
        self.assertIsNone(self.em._late_arrival(response))
        self.assertIsNone(self.em._late_tail)

    def test_an_unchanged_firmware_version(self):
        self.em.bulk_data[2509] = {"raw_value": 1038, "metadata": {}}
        self.em.published_configs.add(2509)
        response = self._response(switch=1, new=True)
        response["2509"] = _point(1038, writable=False)
        self.assertEqual(self.em._late_arrival(response), (SWITCH, 1, False, [NEW]))

    def test_a_failed_poll(self):
        self.em._handle_api_failure("timeout")
        self.assertIsNone(self.em._late_tail)
        self.em._start_late_tail(SWITCH, 1, False, after=0)
        self.assertIsNone(self.em._late_arrival(self._response(switch=1, new=True)))
        with patch(
            "nibe_entity_manager.time.time",
            return_value=self.em._attribution_blocked_until + 1,
        ):
            self.em._start_late_tail(SWITCH, 1, False, after=0)
            self.assertIsNotNone(self.em._late_arrival(self._response(switch=1, new=True)))

    def test_no_controller_side_window_while_blocked(self):
        self.em._attribution_blocked_until = 1e12
        self.assertIsNone(self.em._controller_side_change(self._response(switch=0, other=1)))


class TestLatePeriodStarts(_Base):
    def test_when_a_scan_window_ends(self):
        self.em.bulk_data[SWITCH]["raw_value"] = 1
        _set_window(self.em, point=SWITCH, until=1.0)
        with patch.object(self.em, "_fetch_bulk_data", return_value=True):
            self.em.update_all_states()
        self.assertFalse(self.em.post_write_active)
        self.assertEqual((self.em._late_tail.point, self.em._late_tail.value), (SWITCH, 1))
        self.assertFalse(self.em._late_tail.controller_side)

    def test_when_a_controller_side_window_ends(self):
        self.em.bulk_data[SWITCH]["raw_value"] = 1
        _set_window(self.em, point=SWITCH, controller_side=True, until=1.0)
        with patch.object(self.em, "_fetch_bulk_data", return_value=True):
            self.em.update_all_states()
        self.assertTrue(self.em._late_tail.controller_side)

    def test_when_a_switch_learned_as_showing_nothing_is_written(self):
        """It opens no window, but points it does show after all are still
        linked when they come."""
        from nibe_dynamic_map import DynamicPointEntry

        self.em.dynamic_point_map._table[SWITCH] = DynamicPointEntry(
            point_id=SWITCH,
            title="Switch",
            entity_type="switch",
            processed_values={0, 1},
            unprocessed_values=set(),
            is_controlling=False,
            dynamic_points_by_value={0: [], 1: []},
        )
        self.em._api.write_point.return_value = True
        info = {
            "point_id": SWITCH,
            "entity_type": "switch",
            "metadata": {"modbusRegisterType": "MODBUS_HOLDING_REGISTER"},
            "point_data": {},
            "is_degenerate_range": False,
            "state_topic": f"nibe/state/{SWITCH}",
        }
        with patch.object(self.em, "_open_post_write_scan") as scan:
            self.em._handle_command_worker(info, 1, "1", "cmd-id")
        scan.assert_not_called()
        self.assertEqual((self.em._late_tail.point, self.em._late_tail.value), (SWITCH, 1))


class TestFetchBulkDataLinksLatePoints(_Base):
    def test_a_switch_learned_as_showing_nothing_is_corrected(self):
        """The trap this exists for: a window that ended before the points
        came recorded "shows nothing" for both values of a switch."""
        self.em.dynamic_point_map.record_outcome(SWITCH, 1, [])
        entry = self.em.dynamic_point_map._table[SWITCH]
        self.assertFalse(entry.is_controlling)
        self.em.bulk_data[SWITCH]["raw_value"] = 1
        self.em._start_late_tail(SWITCH, 1, False, after=0)
        self.em._api.fetch_bulk_points.return_value = self._response(switch=1, new=True)
        with patch.object(self.em, "_update_changelog_history"):
            self.em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(entry.dynamic_points_by_value[1], [NEW])
        self.assertTrue(entry.is_controlling)
        self.assertIn(NEW, self.em.active_dynamic_points)
        self.assertEqual(self.em._window.point, SWITCH)


class TestQuickOnOff(_Base):
    """A learned switch turned on and straight off again. The REST API's point
    list lags up to a minute behind its values (docs/known-firmware-quirks.md),
    so the points "on" shows can first appear in a poll in which the switch
    already reads off. Recording them then would teach the bridge that off
    shows them too."""

    def setUp(self):
        super().setUp()
        entry = self.em.dynamic_point_map.get(SWITCH)
        entry.processed_values = {0, 1}
        entry.unprocessed_values = set()
        entry.is_controlling = True
        entry.dynamic_points_by_value = {1: [NEW], 0: []}

    def _write(self, value):
        self.em._api.write_point.return_value = True
        info = {"point_id": SWITCH, "entity_type": "switch", "point_data": {}}
        self.em._handle_command_worker(info, value, str(value), f"w{value}")

    def test_points_of_on_arriving_after_off_are_not_recorded_under_off(self):
        self._write(1)
        self._write(0)
        self.em._api.fetch_bulk_points.return_value = self._response(switch=0, new=True)
        with patch.object(self.em, "_update_changelog_history"):
            self.em._fetch_bulk_data(detect_changes=True)
        entry = self.em.dynamic_point_map.get(SWITCH)
        self.assertEqual(entry.dynamic_points_by_value[0], [])
        self.assertEqual(entry.dynamic_points_by_value[1], [NEW])
        self.assertIn(NEW, self.em.active_dynamic_points, "the point is still shown")


class TestOtherSwitchDuringWindow(_Base):
    """While SWITCH's window is open, OTHER is changed on the controller's
    display: what appears can be either's, so nothing is recorded."""

    def setUp(self):
        super().setUp()
        self.em.bulk_data[SWITCH]["raw_value"] = 1
        self.em._open_post_write_scan(SWITCH, learning_value=1, value=1)

    def _fetch(self, response):
        self.em._api.fetch_bulk_points.return_value = response
        with patch.object(self.em, "_update_changelog_history"):
            self.em._fetch_bulk_data(detect_changes=True)

    def test_points_appearing_then_are_not_recorded_under_the_window_switch(self):
        self._fetch(self._response(switch=1, other=1, new=True))
        self.assertIsNone(self.em._window.value)
        self.assertNotIn(1, self.em.dynamic_point_map.get(SWITCH).dynamic_points_by_value)
        self.assertIn(NEW, self.em.active_dynamic_points, "the point is still shown")

    def test_the_change_spoils_the_window_for_later_polls_too(self):
        self._fetch(self._response(switch=1, other=1))
        self._fetch(self._response(switch=1, other=1, new=True))
        self.assertNotIn(1, self.em.dynamic_point_map.get(SWITCH).dynamic_points_by_value)

    def test_the_value_being_learned_stays_to_be_learned(self):
        self._fetch(self._response(switch=1, other=1))
        self.assertIsNone(self.em._window.learning_value)
        self.assertTrue(self.em._learning_window_ended.is_set(), "waiting writes go ahead")

    def test_a_switch_learned_as_showing_nothing_does_not_spoil_it(self):
        entry = self.em.dynamic_point_map.get(OTHER)
        entry.processed_values, entry.unprocessed_values = {0, 1}, set()
        entry.is_controlling = False
        entry.dynamic_points_by_value = {0: [], 1: []}
        self._fetch(self._response(switch=1, other=1, new=True))
        self.assertEqual(self.em.dynamic_point_map.get(SWITCH).dynamic_points_by_value[1], [NEW])

    def test_a_window_replaced_meanwhile_is_left_alone(self):
        """A write to another switch replaced the window after this poll read
        it: that newer window isn't the one to spoil."""
        stale = self.em._window
        self.em._open_post_write_scan(OTHER, value=1)
        result = self.em._other_switch_spoils_window(self._response(switch=1, other=1), stale)
        self.assertIs(result, stale)
        self.assertEqual((self.em._window.point, self.em._window.value), (OTHER, 1))

    def test_nothing_else_changing_records_as_before(self):
        self._fetch(self._response(switch=1, new=True))
        self.assertEqual(self.em.dynamic_point_map.get(SWITCH).dynamic_points_by_value[1], [NEW])


if __name__ == "__main__":
    unittest.main()
