"""
Direct tests for the parts _fetch_bulk_data and _publish_dynamic_changes are
built from. The tests that drive those two functions as a whole
(test_entity_manager_dynamic.py, test_entity_manager_polling.py and friends)
cover the same behaviour end to end; these pin each part on its own, with
only the inputs it takes.
"""

import unittest
from unittest.mock import patch

from conftest import _make_em, _set_window
from hypothesis import example, given
from hypothesis import strategies as st
from nibe_dynamic_map import DynamicPointEntry


def _switch_entry(point_id=10613, by_value=None):
    return DynamicPointEntry(
        point_id=point_id,
        title="SG Ready activation",
        entity_type="switch",
        unprocessed_values={0, 1} - set(by_value or {}),
        processed_values=set(by_value or {}),
        dynamic_points_by_value=dict(by_value or {}),
    )


class TestWindowOpenedByPoll(unittest.TestCase):
    def test_controller_side_change_opens_a_window_for_its_new_value(self):
        em = _make_em()
        with (
            patch.object(em, "_controller_side_change", return_value=(5, 0, 1)),
            patch.object(em, "_late_arrival") as late,
            patch.object(em, "_open_learning_window", return_value="window") as opened,
        ):
            self.assertEqual(em._window_opened_by_poll({}), "window")
        opened.assert_called_once_with(5, True, 1)
        late.assert_not_called()

    def test_late_arrival_opens_a_window_when_no_switch_changed(self):
        em = _make_em()
        with (
            patch.object(em, "_controller_side_change", return_value=None),
            patch.object(em, "_late_arrival", return_value=(5, 1, False, [7])),
            patch.object(em, "_open_learning_window", return_value="window") as opened,
        ):
            self.assertEqual(em._window_opened_by_poll({}), "window")
        opened.assert_called_once_with(5, False, 1)

    def test_nothing_to_learn_opens_nothing(self):
        em = _make_em()
        with (
            patch.object(em, "_controller_side_change", return_value=None),
            patch.object(em, "_late_arrival", return_value=None),
            patch.object(em, "_open_learning_window") as opened,
        ):
            self.assertIsNone(em._window_opened_by_poll({}))
        opened.assert_not_called()

    def test_window_refused_is_none(self):
        em = _make_em()
        with (
            patch.object(em, "_controller_side_change", return_value=(5, 0, 1)),
            patch.object(em, "_open_learning_window", return_value=None),
        ):
            self.assertIsNone(em._window_opened_by_poll({}))


class TestUpdateBulkEntry(unittest.TestCase):
    def test_new_point_gets_an_entry(self):
        em = _make_em()
        point = {
            "title": "Outdoor temp",
            "description": "",
            "metadata": {"divisor": 10},
            "value": {"integerValue": 55, "stringValue": "", "isOk": True},
        }
        metadata, title, _ = em._update_bulk_entry(100, point, 1000.0)
        self.assertEqual(title, "Outdoor temp")
        self.assertEqual(metadata["divisor"], 10)
        entry = em.bulk_data[100]
        self.assertEqual(
            (entry["raw_value"], entry["is_ok"], entry["timestamp"]), (55, True, 1000.0)
        )

    def test_explicit_nulls_do_not_raise(self):
        em = _make_em()
        metadata, _, _ = em._update_bulk_entry(100, {"value": None, "metadata": None}, 1.0)
        self.assertEqual(em.bulk_data[100]["raw_value"], 0)
        self.assertIsInstance(metadata, dict)

    def test_existing_entry_is_updated_in_place(self):
        em = _make_em()
        em._update_bulk_entry(100, {"value": {"integerValue": 1}}, 1.0)
        entry = em.bulk_data[100]
        em._update_bulk_entry(100, {"value": {"integerValue": 2}}, 2.0)
        self.assertIs(em.bulk_data[100], entry)
        self.assertEqual((entry["raw_value"], entry["timestamp"]), (2, 2.0))


class TestIsDynamicAppearance(unittest.TestCase):
    def test_every_point_in_a_window_is_dynamic(self):
        em = _make_em()
        with patch.object(em, "_index_static_point") as indexed:
            self.assertTrue(em._is_dynamic_appearance(400, {}, "T", "", in_window=True))
        indexed.assert_not_called()

    def test_known_dynamic_point_outside_a_window_is_dynamic(self):
        em = _make_em()
        em.dynamic_point_map._table[10613] = _switch_entry(by_value={1: [400]})
        with patch.object(em, "_index_static_point") as indexed:
            self.assertTrue(em._is_dynamic_appearance(400, {}, "T", "", in_window=False))
        indexed.assert_not_called()

    def test_unknown_point_outside_a_window_is_indexed_as_static(self):
        em = _make_em()
        self.assertFalse(em._is_dynamic_appearance(400, {}, "Static", "", in_window=False))
        self.assertIn(400, em.all_points_by_id)
        self.assertFalse(em.all_points_by_id[400]["is_dynamic"])
        self.assertIn(400, em.baseline_point_ids)
        em._pub.publish_point_metadata.assert_called_once_with(em.all_points_by_id[400])

    @given(in_window=st.booleans(), known=st.booleans())
    def test_dynamic_in_a_window_or_when_known_otherwise_indexed_static(self, in_window, known):
        em = _make_em()
        if known:
            em.dynamic_point_map._table[10613] = _switch_entry(by_value={1: [400]})
        dynamic = em._is_dynamic_appearance(400, {}, "T", "", in_window=in_window)
        self.assertEqual(dynamic, in_window or known)
        if dynamic:
            self.assertNotIn(400, em.all_points_by_id)
            self.assertNotIn(400, em.baseline_point_ids)
        else:
            self.assertFalse(em.all_points_by_id[400]["is_dynamic"])
            self.assertIn(400, em.baseline_point_ids)

    def test_new_permanent_switch_gets_a_map_entry(self):
        em = _make_em()
        with patch.object(em, "_index_static_point", return_value="switch") as indexed:
            indexed.side_effect = lambda pid, *a: (
                em.all_points_by_id.__setitem__(
                    pid, {"variableId": pid, "entity_type": "switch", "metadata": {}}
                )
                or "switch"
            )
            with patch.object(em.dynamic_point_map, "populate_from_bulk") as populated:
                em._is_dynamic_appearance(400, {}, "Switch", "", in_window=False)
        populated.assert_called_once()
        self.assertEqual(populated.call_args.args[1], {400: "switch"})


ids = st.integers(min_value=1, max_value=30)
id_sets = st.frozensets(ids, max_size=10)


class TestDynamicDisappearances(unittest.TestCase):
    def test_only_active_known_dynamic_points_missing_from_the_poll(self):
        em = _make_em()
        em.dynamic_point_map._table[10613] = _switch_entry(by_value={1: [400, 401, 402]})
        em.active_dynamic_points = {400, 401, 999}
        self.assertEqual(em._dynamic_disappearances({401, 1}), {400})

    @given(shown=id_sets, active=id_sets, served=id_sets)
    @example(
        shown=frozenset({3260, 4694, 10614}),
        active=frozenset({3260, 4694, 10614}),
        served=frozenset(),
    )
    def test_exactly_the_active_known_points_not_served(self, shown, active, served):
        em = _make_em()
        em.dynamic_point_map._table[10613] = _switch_entry(by_value={1: sorted(shown)})
        em.active_dynamic_points = set(active)
        self.assertEqual(em._dynamic_disappearances(set(served)), (shown & active) - served)


class TestHandleDynamicChanges(unittest.TestCase):
    def _window(self, em, **changes):
        _set_window(em, **changes)
        return em._window

    def test_publishes_with_the_window_read_at_the_start(self):
        em = _make_em()
        window = self._window(em, point=10613, value=1, controller_side=True)
        with (
            patch.object(em, "_publish_dynamic_changes") as published,
            patch.object(em, "_release_learning_write"),
        ):
            em._handle_dynamic_changes(window, [(400, {})], set(), point_set_changed=True)
        published.assert_called_once_with(
            [(400, {})], set(), 10613, controller_side=True, controlling_value=1
        )

    def test_marks_saw_points_only_on_the_same_points_open_window(self):
        em = _make_em()
        window = self._window(em, point=10613)
        with (
            patch.object(em, "_publish_dynamic_changes"),
            patch.object(em, "_release_learning_write"),
        ):
            em._handle_dynamic_changes(window, [(400, {})], set(), point_set_changed=True)
        self.assertTrue(em._window.saw_points)

        em._window = None
        self._window(em, point=6984)
        with (
            patch.object(em, "_publish_dynamic_changes"),
            patch.object(em, "_release_learning_write"),
        ):
            em._handle_dynamic_changes(window, [(400, {})], set(), point_set_changed=True)
        self.assertFalse(em._window.saw_points)

    def test_point_set_change_releases_the_waiting_write_only_in_a_window(self):
        em = _make_em()
        window = self._window(em, point=10613)
        with patch.object(em, "_release_learning_write") as released:
            em._handle_dynamic_changes(window, [], set(), point_set_changed=True)
            released.assert_called_once_with()
            released.reset_mock()
            em._handle_dynamic_changes(window, [], set(), point_set_changed=False)
            em._handle_dynamic_changes(None, [], set(), point_set_changed=True)
            released.assert_not_called()

    def test_nothing_changed_publishes_nothing(self):
        em = _make_em()
        with patch.object(em, "_publish_dynamic_changes") as published:
            em._handle_dynamic_changes(None, [], set(), point_set_changed=False)
        published.assert_not_called()

    def test_disappearance_outside_a_window_has_no_controller(self):
        em = _make_em()
        with patch.object(em, "_publish_dynamic_changes") as published:
            em._handle_dynamic_changes(None, [], {400}, point_set_changed=True)
        published.assert_called_once_with(
            [], {400}, None, controller_side=False, controlling_value=None
        )


class TestRecordWindowOutcome(unittest.TestCase):
    def _em(self, current):
        em = _make_em()
        em.dynamic_point_map._table[10613] = _switch_entry()
        em.bulk_data[10613] = {"raw_value": current}
        return em

    def test_records_under_the_value_the_switch_still_reads(self):
        em = self._em(current=1)
        with patch.object(em, "_persist_dynamic_map") as persisted:
            em._record_window_outcome([3260, 4694], 10613, 1)
        self.assertEqual(em.dynamic_point_map[10613].dynamic_points_by_value[1], [3260, 4694])
        persisted.assert_called_once_with()

    def test_nothing_recorded_when_the_switch_reads_another_value(self):
        em = self._em(current=0)
        with patch.object(em, "_persist_dynamic_map") as persisted:
            em._record_window_outcome([3260], 10613, 1)
        self.assertEqual(em.dynamic_point_map[10613].dynamic_points_by_value, {})
        persisted.assert_not_called()

    def test_nothing_recorded_for_an_ambiguous_window(self):
        em = self._em(current=1)
        with patch.object(em, "_persist_dynamic_map"):
            em._record_window_outcome([3260], 10613, None)
        self.assertEqual(em.dynamic_point_map[10613].dynamic_points_by_value, {})

    def test_points_still_shown_stay_recorded_with_the_new_value(self):
        em = self._em(current=2)
        entry = em.dynamic_point_map[10613]
        entry.unprocessed_values = {2}
        entry.dynamic_points_by_value = {1: [500]}
        em.active_dynamic_points = {500}
        em.bulk_data[500] = {"raw_value": 0}
        with patch.object(em, "_persist_dynamic_map"):
            em._record_window_outcome([600], 10613, 2)
        self.assertEqual(entry.dynamic_points_by_value[2], [500, 600])

    def test_unknown_controller_gets_an_entry_spanning_its_range(self):
        em = _make_em()
        em.all_points_by_id[77] = {
            "display_title": "Mode",
            "entity_type": "select",
            "metadata": {"minValue": 0, "maxValue": 3},
        }
        em.bulk_data[77] = {"raw_value": 2}
        with patch.object(em, "_persist_dynamic_map"):
            em._record_window_outcome([800], 77, 2)
        entry = em.dynamic_point_map[77]
        self.assertEqual((entry.title, entry.entity_type), ("Mode", "select"))
        self.assertEqual(entry.unprocessed_values, {0, 1, 3})
        self.assertEqual(entry.dynamic_points_by_value, {2: [800]})

    def test_no_window_or_nothing_enabled_records_nothing(self):
        em = self._em(current=1)
        with patch.object(em, "_persist_dynamic_map") as persisted:
            em._record_window_outcome([3260], None, 1)
            em._record_window_outcome([], 10613, 1)
        persisted.assert_not_called()


class TestTriggeredBy(unittest.TestCase):
    def test_no_controller(self):
        self.assertIsNone(_make_em()._triggered_by(None, False))

    def test_names_the_switch_its_value_and_where_it_was_changed(self):
        em = _make_em()
        em.all_points_by_id[10613] = {"display_title": "SG Ready activation"}
        em.bulk_data[10613] = {"raw_value": 1}
        self.assertEqual(
            em._triggered_by(10613, True),
            {"id": 10613, "title": "SG Ready activation", "value": 1, "changed_on": "controller"},
        )

    def test_unknown_value_and_written_from_ha(self):
        em = _make_em()
        self.assertEqual(em._triggered_by(5, False), {"id": 5, "title": "Point 5"})


if __name__ == "__main__":
    unittest.main()
