"""
test_entity_manager_dynamic.py
==============================
Dynamic point detection and reconciliation tests for nibe_entity_manager.py — split out of test_entity_manager.py
for file-size/maintainability. Shared fixtures are in conftest.py.
"""

import itertools
import json
import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

from conftest import (
    _make_em,
)
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)


class TestDynamicPointEntryComputedProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointEntry computed methods."""

    _entry_strategy = st.fixed_dictionaries(
        {
            "point_id": st.integers(min_value=1, max_value=99999),
            "title": st.text(max_size=40),
            "entity_type": st.sampled_from(["switch", "select"]),
            "processed_values": st.sets(st.integers(min_value=0, max_value=20)),
            "unprocessed_values": st.sets(st.integers(min_value=0, max_value=20)),
            "is_controlling": st.one_of(st.none(), st.booleans()),
            "firmware_removed": st.booleans(),
        }
    )

    @given(_entry_strategy)
    def test_is_fully_processed_iff_unprocessed_empty_and_processed_nonempty(self, kwargs):
        from nibe_dynamic_map import DynamicPointEntry

        entry = DynamicPointEntry(**dict(kwargs.items()))
        expected = len(entry.unprocessed_values) == 0 and len(entry.processed_values) > 0
        self.assertEqual(entry.is_fully_processed(), expected)

    @given(_entry_strategy)
    def test_all_known_dynamic_points_is_union_of_dpbv(self, kwargs):
        from nibe_dynamic_map import DynamicPointEntry

        entry = DynamicPointEntry(**dict(kwargs.items()))
        expected = set()
        for pts in entry.dynamic_points_by_value.values():
            expected.update(pts)
        self.assertEqual(entry.all_known_dynamic_points(), expected)

    @given(_entry_strategy, st.integers(min_value=0, max_value=20))
    def test_dynamic_points_for_value_none_iff_not_in_dpbv(self, kwargs, value):
        from nibe_dynamic_map import DynamicPointEntry

        entry = DynamicPointEntry(**dict(kwargs.items()))
        result = entry.dynamic_points_for_value(value)
        if value in entry.dynamic_points_by_value:
            self.assertIsNotNone(result)
            self.assertEqual(result, entry.dynamic_points_by_value[value])
        else:
            self.assertIsNone(result)

    @given(_entry_strategy)
    def test_all_known_dynamic_points_never_raises(self, kwargs):
        from nibe_dynamic_map import DynamicPointEntry

        entry = DynamicPointEntry(**dict(kwargs.items()))
        result = entry.all_known_dynamic_points()
        self.assertIsInstance(result, set)

    @given(_entry_strategy)
    def test_default_is_controlling_is_none(self, kwargs):
        """A freshly constructed entry (no explicit is_controlling) starts as None."""
        from nibe_dynamic_map import DynamicPointEntry

        kwargs_no_ctrl = {k: v for k, v in kwargs.items() if k != "is_controlling"}
        entry = DynamicPointEntry(**kwargs_no_ctrl)
        self.assertIsNone(entry.is_controlling)

    @given(_entry_strategy)
    def test_default_firmware_removed_is_false(self, kwargs):
        from nibe_dynamic_map import DynamicPointEntry

        kwargs_no_fr = {k: v for k, v in kwargs.items() if k != "firmware_removed"}
        entry = DynamicPointEntry(**kwargs_no_fr)
        self.assertFalse(entry.firmware_removed)


class TestDynamicPoints(unittest.TestCase):
    def setUp(self):
        self.em = _make_em()

    def _seed(self, pid, entity_type="number", is_dynamic=False):
        self.em.all_points_by_id[pid] = {
            "variableId": pid,
            "display_title": f"Point {pid}",
            "entity_type": entity_type,
            "is_dynamic": is_dynamic,
            "metadata": {},
            "entity_category": "diagnostic",
            "is_writable": False,
        }

    def _seed_dynamic_map_entry(self, controlling_pid, dynamic_pids, value=1):
        """Add a known controlling entry to the dynamic_point_map."""
        from nibe_dynamic_map import DynamicPointEntry

        self.em.dynamic_point_map._table[controlling_pid] = DynamicPointEntry(
            point_id=controlling_pid,
            title=f"Switch {controlling_pid}",
            entity_type="switch",
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

    def test_disappeared_deindex_invalidate_and_mqtt_clear_use_the_real_point(self):
        """A disappeared point's cleanup calls must all target the real
        point_id, and the retained MQTT clear must be an empty payload
        (to actually clear the retained topic) with retain=True (so the
        clear itself persists) — not a wrong point_id, non-empty payload,
        or non-retained publish, any of which would leave stale HA state."""
        from nibe_mqtt_publisher import BrowserTopic

        self._seed(6983)
        self.em.active_dynamic_points.add(6983)
        with patch.object(self.em, "_deindex_point") as mock_deindex:
            self.em._publish_dynamic_changes([], {6983})
        mock_deindex.assert_called_once_with(6983)
        self.em._pub.invalidate_config_hash.assert_called_once_with(6983)
        self.em.mqtt.publish.assert_any_call(
            BrowserTopic.META_TEMPLATE.format(id=6983), "", retain=True
        )

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
            "title": "New point",
            "description": "",
            "metadata": {
                "divisor": 1,
                "unit": "kW",
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "isWritable": False,
                "variableType": "integer",
                "variableSize": "s16",
                "minValue": 0,
                "maxValue": 100,
                "shortUnit": "kW",
                "modbusRegisterID": 1000,
                "intDefaultValue": 0,
                "change": 1,
                "stringDefaultValue": "",
            },
            "value": {"isOk": True, "integerValue": 10, "stringValue": ""},
        }
        self.em._publish_dynamic_changes([(pid, fake_point_data)], set())
        self.assertIn(pid, self.em.active_dynamic_points)

    def test_appeared_point_without_controlling_id_does_not_create_bogus_map_entry(self):
        """`if controlling and enabled_new_pids:` — an `and`->`or` mutation
        would enter this block even when controlling_point_id is None (the
        default, e.g. a firmware-triggered appearance with no write in
        progress), calling self.dynamic_point_map.get(None) and creating a
        garbage DynamicPointEntry keyed at None. No prior test asserts
        dynamic_point_map._table has no such entry."""
        pid = 7002
        self.em.initial_discovery_complete = True
        fake_point_data = {
            "title": "New point",
            "description": "",
            "metadata": {
                "divisor": 1,
                "unit": "kW",
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "isWritable": False,
                "variableType": "integer",
                "variableSize": "s16",
                "minValue": 0,
                "maxValue": 100,
                "shortUnit": "kW",
                "modbusRegisterID": 1000,
                "intDefaultValue": 0,
                "change": 1,
                "stringDefaultValue": "",
            },
            "value": {"isOk": True, "integerValue": 10, "stringValue": ""},
        }
        self.em._publish_dynamic_changes([(pid, fake_point_data)], set())
        self.assertNotIn(None, self.em.dynamic_point_map._table)

    def test_appeared_point_entity_type_dict_uses_real_keys_and_values(self):
        """The dict built for _get_cached_entity_type (and processed for
        _index_point) must use the real key names ('metadata', 'title',
        'description') with the real title/description text — a mistyped
        key would silently fall back to defaults inside detect_entity_type."""
        pid = 7005
        self.em.initial_discovery_complete = True
        fake_point_data = {
            "title": "Real Dynamic Title",
            "description": "Real dynamic description",
            "metadata": {
                "divisor": 1,
                "unit": "kW",
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "isWritable": False,
                "variableType": "integer",
                "variableSize": "s16",
                "minValue": 0,
                "maxValue": 100,
            },
            "value": {"isOk": True, "integerValue": 10, "stringValue": ""},
        }
        with patch.object(
            self.em, "_get_cached_entity_type", return_value=("sensor", "diagnostic")
        ) as mock_detect:
            self.em._publish_dynamic_changes([(pid, fake_point_data)], set())
        detect_arg = mock_detect.call_args.args[0]
        self.assertEqual(detect_arg["variableId"], pid)
        self.assertEqual(detect_arg["title"], "Real Dynamic Title")
        self.assertEqual(detect_arg["description"], "Real dynamic description")
        self.assertEqual(detect_arg["metadata"]["unit"], "kW")
        indexed = self.em.all_points_by_id[pid]
        self.assertEqual(indexed["display_title"], "Real Dynamic Title")
        self.assertEqual(indexed["description"], "Real dynamic description")

    def test_appeared_point_with_explicit_null_metadata_does_not_crash(self):
        """A newly-appeared dynamic point with "metadata": null (present but
        None, not just absent) must not crash the whole bulk-fetch cycle —
        .get(key, {}) only supplies its default when the key is missing.
        It must also be processed correctly (not just silently dropped) —
        null metadata is treated as {} (no isWritable, etc.), and the point
        still ends up active, matching how a point with genuinely absent
        metadata would be handled."""
        pid = 7003
        self.em.initial_discovery_complete = True
        fake_point_data = {
            "title": "Null metadata point",
            "description": "",
            "metadata": None,
            "value": {"isOk": True, "integerValue": 10, "stringValue": ""},
        }
        self.em._publish_dynamic_changes([(pid, fake_point_data)], set())  # must not raise
        self.assertIn(pid, self.em.active_dynamic_points)
        self.assertIn(pid, self.em.all_points_by_id)

    def test_appeared_point_not_added_when_enable_fails(self):
        """If discovery publish fails for a newly-appeared dynamic point,
        it must not be marked active — that would desync the bridge's own
        state from what HA actually knows about."""
        pid = 7002
        self.em.initial_discovery_complete = True
        fake_point_data = {
            "title": "New point",
            "description": "",
            "metadata": {
                "divisor": 1,
                "unit": "kW",
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "isWritable": False,
                "variableType": "integer",
                "variableSize": "s16",
                "minValue": 0,
                "maxValue": 100,
                "shortUnit": "kW",
                "modbusRegisterID": 1000,
                "intDefaultValue": 0,
                "change": 1,
                "stringDefaultValue": "",
            },
            "value": {"isOk": True, "integerValue": 10, "stringValue": ""},
        }
        with patch.object(self.em, "_enable_entity_locked", return_value=False):
            self.em._publish_dynamic_changes([(pid, fake_point_data)], set())
        self.assertNotIn(pid, self.em.active_dynamic_points)

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
        """Malformed JSON must be rejected cleanly, not partially applied —
        active_dynamic_points must remain unchanged, not just 'not raise'."""
        from nibe_entity_manager import EntityManager

        self.em.initial_discovery_complete = False
        EntityManager._setup_dynamic_map_loading(self.em)
        self.em.active_dynamic_points = {123}  # pre-existing state must survive
        msg = MagicMock()
        msg.payload = b"not valid json {{{"
        self.em._on_active_dynamic_message(None, None, msg)  # must not raise
        self.assertEqual(self.em.active_dynamic_points, {123})

    def test_known_dynamic_not_classified_as_static_outside_scan_window(self):
        """is_known_dynamic guard: a point in the dynamic map must not be
        routed to the static path regardless of scan window state.
        Verifies the guard condition directly without invoking _fetch_bulk_data."""
        controlling_pid = 1001
        dynamic_pid = 22001
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
            "variableId": pid,
            "display_title": f"Point {pid}",
            "entity_type": "number",
            "is_dynamic": True,
            "metadata": {},
            "entity_category": "diagnostic",
            "is_writable": False,
        }
        self.em.active_dynamic_points.add(pid)

    def test_persist_called_before_changelog_on_disappearance(self):
        """_persist_active_dynamic must be called before _update_changelog_history."""
        pid = 6983
        self._seed(pid)
        call_order = []

        original_persist = self.em._persist_active_dynamic
        original_changelog = self.em._update_changelog_history

        def mock_persist():
            call_order.append("persist")
            original_persist()

        def mock_changelog(event):
            call_order.append("changelog")
            original_changelog(event)

        self.em._persist_active_dynamic = mock_persist
        self.em._update_changelog_history = mock_changelog

        self.em._publish_dynamic_changes([], {pid})

        persist_idx = call_order.index("persist")
        changelog_idx = call_order.index("changelog")
        self.assertLess(
            persist_idx, changelog_idx, "persist must be called before changelog (write-ahead)"
        )

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
            p for t, p in published_payloads if t == BrowserTopic.ACTIVE_DYNAMIC
        ]
        self.assertTrue(
            len(active_dynamic_publishes) > 0, "ACTIVE_DYNAMIC should have been published"
        )
        first_payload = json.loads(active_dynamic_publishes[0])
        self.assertNotIn(
            pid, first_payload, "Disappeared point must not appear in first persist call"
        )

    def test_appeared_point_persisted_in_active_set(self):
        """When a point appears it must end up in active_dynamic_points."""
        pid = 7001
        self.em.initial_discovery_complete = True
        fake_point_data = {
            "title": "New point",
            "description": "",
            "metadata": {
                "divisor": 1,
                "unit": "kW",
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "isWritable": False,
                "variableType": "integer",
                "variableSize": "s16",
                "minValue": 0,
                "maxValue": 100,
                "shortUnit": "kW",
                "modbusRegisterID": 1000,
                "intDefaultValue": 0,
                "change": 1,
                "stringDefaultValue": "",
            },
            "value": {"isOk": True, "integerValue": 10, "stringValue": ""},
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

    CONTROL_PIDS: ClassVar[list] = [10, 20, 30]  # switch/select controlling points
    DYNAMIC_PIDS: ClassVar[list] = [1000, 1001, 1002, 1003, 1004]  # points that appear/disappear
    SELECT_PID = 40  # 3-value select — record_outcome's rule below is
    # restricted to 2-value switches, so a dedicated
    # entry is needed to reach the "no inverse
    # inference" branch for selects with >2 options.

    @initialize()
    def setup(self):
        from nibe_dynamic_map import DynamicPointEntry, DynamicPointMap

        self.map = DynamicPointMap()
        self.DPE = DynamicPointEntry
        # Pre-populate entries for the control pids
        for pid in self.CONTROL_PIDS:
            entry = DynamicPointEntry(
                point_id=pid,
                title=f"Switch {pid}",
                entity_type="switch",
                unprocessed_values={0, 1},
            )
            self.map._table[pid] = entry
        self.map._table[self.SELECT_PID] = DynamicPointEntry(
            point_id=self.SELECT_PID,
            title="Select 40",
            entity_type="select",
            unprocessed_values={0, 1, 2},
        )
        # Independent model of what expected_active_dynamic_points() should
        # return, built only from this machine's own rule inputs (never read
        # back from self.map._table) so the invariant below is a genuine
        # black-box check rather than a mirror of the production algorithm.
        # model_last_recorded[pid][value] = the dynamic pids most recently
        # passed to record_outcome(pid, value, ...) — this is safe to predict
        # exactly because record_outcome always overwrites
        # dynamic_points_by_value[value] unconditionally for the *explicit*
        # value passed in, and its switch-inverse inference (nibe_dynamic_map.py
        # :400-411) only ever fills in the *other*, not-yet-recorded value for
        # a 2-value entry — it never touches the value that was just recorded.
        self.model_last_recorded = {}  # pid -> {value: frozenset(dynamic_pids)}
        self.model_removed = set()  # pids currently firmware_removed (per our own calls)
        # Every pid actually present as a table entry — starts as the
        # pre-populated switches + select, and grows whenever populate_from_bulk
        # adds a genuinely new entry (tracked by observing the real table's
        # keys before/after the call, not by re-predicting populate_from_bulk's
        # own filtering logic).
        self.model_table_pids = set(self.CONTROL_PIDS) | {self.SELECT_PID}

    # ── Rules ────────────────────────────────────────────────────────────────

    @rule(
        control_pid=st.sampled_from(CONTROL_PIDS),
        value=st.integers(min_value=0, max_value=1),
        dynamic_pids=st.lists(
            st.sampled_from(DYNAMIC_PIDS),
            min_size=0,
            max_size=3,
            unique=True,
        ),
    )
    def record_outcome(self, control_pid, value, dynamic_pids):
        self.map.record_outcome(control_pid, value, dynamic_pids)
        self.model_last_recorded.setdefault(control_pid, {})[value] = frozenset(dynamic_pids)

    @rule(
        value=st.integers(min_value=0, max_value=2),
        dynamic_pids=st.lists(
            st.sampled_from(DYNAMIC_PIDS),
            min_size=0,
            max_size=3,
            unique=True,
        ),
    )
    def record_outcome_select(self, value, dynamic_pids):
        """record_outcome's switch-inverse inference (nibe_dynamic_map.py:
        405-419) only fires when len(processed | unprocessed) == 2 — the
        record_outcome rule above only ever targets 2-value switches, so a
        select with 3 options never reached the 'each value independent, no
        inference' branch. Confirm recording one value on this 3-value
        select never silently completes the other, still-unobserved values
        the way it would for a switch."""
        entry = self.map._table[self.SELECT_PID]
        other_unprocessed_before = entry.unprocessed_values - {value}

        self.map.record_outcome(self.SELECT_PID, value, dynamic_pids)

        assert value not in entry.unprocessed_values, (
            f"value {value} still in unprocessed_values after recording it"
        )
        assert other_unprocessed_before <= entry.unprocessed_values, (
            f"record_outcome({self.SELECT_PID}, {value}, ...) on a 3-value "
            f"select auto-completed other value(s) "
            f"{other_unprocessed_before - entry.unprocessed_values} via "
            f"switch-style inverse inference, which must only apply to "
            f"exactly-2-value entries"
        )
        self.model_last_recorded.setdefault(self.SELECT_PID, {})[value] = frozenset(dynamic_pids)

    @rule(control_pid=st.sampled_from(CONTROL_PIDS))
    def mark_firmware_removed(self, control_pid):
        self.map.mark_firmware_removed(control_pid)
        self.model_removed.add(control_pid)

    @rule()
    def restore_from_bulk(self):
        """Restore every known table pid (control pids, select, and any
        entries populate_from_bulk has added since) as if they all appeared
        in a bulk fetch."""
        self.map.restore_from_bulk(set(self.model_table_pids))
        self.model_removed -= self.model_table_pids

    @rule(
        bulk_subset=st.lists(
            st.sampled_from(CONTROL_PIDS + [SELECT_PID]),
            min_size=0,
            max_size=len(CONTROL_PIDS) + 1,
            unique=True,
        ),
    )
    def mark_absent_as_firmware_removed(self, bulk_subset):
        """mark_absent_as_firmware_removed (nibe_dynamic_map.py:328-...) is the
        symmetric inverse of restore_from_bulk: every known point NOT present
        in bulk_subset gets firmware_removed=True, and the method returns
        exactly the set of pids newly marked (already-removed pids are
        excluded from the return value). Never exercised by this machine
        before. Compares against model_table_pids (not a static set) so
        entries populate_from_bulk adds later are correctly included in the
        domain — omitting them would make them permanently 'absent' with no
        way to become newly-marked here, which isn't what this rule intends
        to model."""
        bulk_set = set(bulk_subset)
        absent = self.model_table_pids - bulk_set
        expected_newly_marked = absent - self.model_removed
        result = self.map.mark_absent_as_firmware_removed(bulk_set)
        assert result == expected_newly_marked, (
            f"mark_absent_as_firmware_removed({bulk_set}) returned "
            f"{result}, expected {expected_newly_marked} (absent={absent}, "
            f"already-removed={self.model_removed})"
        )
        self.model_removed |= absent

    @rule()
    def flush(self):
        """Flush the map — resets all entries to unprocessed."""
        all_points = {
            pid: {
                "variableId": pid,
                "display_title": f"Switch {pid}",
                "metadata": {"minValue": 0, "maxValue": 1},
            }
            for pid in self.CONTROL_PIDS
        }
        all_points[self.SELECT_PID] = {
            "variableId": self.SELECT_PID,
            "display_title": "Select 40",
            "metadata": {"minValue": 0, "maxValue": 2},
        }
        types = dict.fromkeys(self.CONTROL_PIDS, "switch")
        types[self.SELECT_PID] = "select"
        self.map.flush(all_points, types)
        # flush() wipes dynamic_points_by_value/is_controlling for every
        # entry (nibe_dynamic_map.py:445-448) but leaves firmware_removed
        # untouched, so only the recorded-outcome model needs resetting.
        # The select's metadata range (0..2) must be passed through here too
        # — otherwise flush would default it to 0..1 and silently turn it
        # into a 2-value entry, wrongly enabling the switch-style inverse
        # inference that record_outcome_select exists to rule out.
        self.model_last_recorded.clear()

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
            assert not overlap, f"Entry {pid}: unprocessed ∩ processed = {overlap}"

    @invariant()
    def is_fully_processed_consistent(self):
        """is_fully_processed() ↔ unprocessed_values == ∅"""
        for pid, entry in self.map._table.items():
            expected = len(entry.unprocessed_values) == 0
            actual = entry.is_fully_processed()
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
            via_set = pid in all_known
            assert via_method == via_set, (
                f"is_known_dynamic({pid})={via_method} but pid in all_known_ids={via_set}"
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
    def expected_active_dynamic_points_matches_recorded_outcomes(self):
        """expected_active_dynamic_points() must reproduce exactly the dynamic
        pids most recently recorded for a (pid, value) pair via record_outcome,
        when queried with only that pid's current value — and must return
        nothing at all for a pid we've marked firmware_removed, regardless of
        what was ever recorded for it."""
        for pid, value_map in self.model_last_recorded.items():
            for value, expected_pids in value_map.items():
                result = self.map.expected_active_dynamic_points({pid: value})
                if pid in self.model_removed:
                    assert result == set(), (
                        f"expected_active_dynamic_points({{{pid}: {value}}}) "
                        f"= {result}, but pid {pid} is firmware_removed and "
                        f"should contribute nothing"
                    )
                else:
                    assert result == set(expected_pids), (
                        f"expected_active_dynamic_points({{{pid}: {value}}}) "
                        f"= {result}, expected exactly {set(expected_pids)} "
                        f"(last recorded via record_outcome({pid}, {value}, ...))"
                    )

    @invariant()
    def controlling_entry_returns_entry_iff_known(self):
        """controlling_entry_for_dynamic(pid) returns entry iff is_known_dynamic(pid)."""
        for pid in self.DYNAMIC_PIDS:
            entry = self.map.controlling_entry_for_dynamic(pid)
            known = self.map.is_known_dynamic(pid)
            if known:
                assert entry is not None, (
                    f"controlling_entry_for_dynamic({pid}) is None but is_known_dynamic={known}"
                )
            else:
                assert entry is None, (
                    f"controlling_entry_for_dynamic({pid}) is not None but is_known_dynamic={known}"
                )

    @rule(
        pids=st.lists(
            st.sampled_from(CONTROL_PIDS + DYNAMIC_PIDS),
            min_size=0,
            max_size=6,
            unique=True,
        ),
        entity_types=st.dictionaries(
            st.sampled_from(CONTROL_PIDS + DYNAMIC_PIDS),
            st.sampled_from(["switch", "select"]),
            max_size=6,
        ),
    )
    def populate_from_bulk(self, pids, entity_types):
        """populate_from_bulk is the production entry point — call it with
        realistic all_points_by_id and entity_type_map inputs to exercise
        the new-entry detection and min/max range recording paths.

        entity_types' keys must be drawn from the same domain as pids (not
        just CONTROL_PIDS, which are always already in the table from setup()
        and therefore always hit the 'point_id in self._table: continue'
        guard) — otherwise every candidate is skipped and the actual
        new-entry-added branch (nibe_dynamic_map.py:292-302) is never
        reached, silently making this rule dead code."""
        all_points = {
            pid: {
                "variableId": pid,
                "display_title": f"Point {pid}",
                "metadata": {"minValue": 0, "maxValue": 1},
            }
            for pid in pids
        }
        before = set(self.map._table.keys())
        self.map.populate_from_bulk(all_points, entity_types)
        after = set(self.map._table.keys())
        # Track any genuinely new entries (observed from the real table, not
        # re-predicted) so the mark_absent_as_firmware_removed / restore_from_bulk
        # rules' pid domain stays accurate even as populate_from_bulk grows
        # the table beyond the original CONTROL_PIDS/SELECT_PID set.
        self.model_table_pids |= after - before


DynamicPointMapStatefulTest = DynamicPointMapMachine.TestCase


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
            "variableId": 5110,
            "display_title": "Prevent condensation climate system 1",
            "entity_type": "switch",
            "entity_category": "config",
            "is_writable": True,
            "is_dynamic": False,
            "metadata": {
                "minValue": 0,
                "maxValue": 1,
                "divisor": 1,
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
            },
        }
        em.bulk_data[5110] = {"raw_value": 1, "display_value": "1"}
        return em

    def _dynamic_point_data(self, pid):
        return (
            pid,
            {
                "variableId": pid,
                "title": f"Dynamic point {pid}",
                "description": "",
                "metadata": {
                    "minValue": 0,
                    "maxValue": 100,
                    "divisor": 1,
                    "unit": "°C",
                    "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                    "isWritable": False,
                    "variableType": "integer",
                    "variableSize": "u16",
                    "modbusRegisterID": pid,
                    "shortUnit": "",
                    "decimal": 0,
                    "change": 0,
                    "intDefaultValue": 0,
                },
            },
        )

    def test_triggered_by_populated_when_controlling_point_set(self):
        """Core fix: change_event['triggered_by'] must reflect the
        controlling point when _post_write_controlling_point is set."""
        em = self._make_em()

        captured = {}
        original_update = em._update_changelog_history

        def capture_changelog(change_event):
            captured["event"] = change_event
            original_update(change_event)

        em._update_changelog_history = capture_changelog

        em._publish_dynamic_changes(
            new_points=[self._dynamic_point_data(50827)],
            disappeared_points=set(),
            controlling_point_id=5110,
        )

        trig = captured["event"].get("triggered_by")
        self.assertIsNotNone(trig, "triggered_by must not be None when controlling point is known")
        self.assertEqual(trig["id"], 5110)
        self.assertEqual(trig["title"], "Prevent condensation climate system 1")

    def test_triggered_by_includes_value_when_bulk_data_available(self):
        """triggered_by should include the written value (from bulk_data)
        so the card can show 'value written: 1' in the changelog entry."""
        em = self._make_em()
        em.bulk_data[5110] = {"raw_value": 1}

        captured = {}
        original_update = em._update_changelog_history

        def capture_changelog(change_event):
            captured["event"] = change_event
            original_update(change_event)

        em._update_changelog_history = capture_changelog

        em._publish_dynamic_changes(
            new_points=[self._dynamic_point_data(50827)],
            disappeared_points=set(),
            controlling_point_id=5110,
        )
        trig = captured["event"].get("triggered_by")
        self.assertIn("value", trig)
        self.assertEqual(trig["value"], 1)

    def test_triggered_by_none_when_no_controlling_point(self):
        """Startup and periodic-poll changes have no controlling point —
        triggered_by must remain None so the card doesn't show a spurious
        'triggered by' line."""
        em = self._make_em()

        captured = {}
        original_update = em._update_changelog_history

        def capture_changelog(change_event):
            captured["event"] = change_event
            original_update(change_event)

        em._update_changelog_history = capture_changelog

        em._publish_dynamic_changes(
            new_points=[self._dynamic_point_data(50827)],
            disappeared_points=set(),
            controlling_point_id=None,
        )
        trig = captured["event"].get("triggered_by")
        self.assertIsNone(trig)

    def test_triggered_by_persisted_in_changelog_history(self):
        """triggered_by must survive the round-trip through
        _update_changelog_history so the changelog modal can display it."""
        em = self._make_em()

        em._publish_dynamic_changes(
            new_points=[self._dynamic_point_data(50827)],
            disappeared_points=set(),
            controlling_point_id=5110,
        )
        entry = em.change_history[0]
        trig = entry.get("triggered_by")
        self.assertIsNotNone(trig)
        self.assertEqual(trig["id"], 5110)

    def test_changelog_entry_reuses_the_change_events_own_timestamp(self):
        """change_event['timestamp']/['iso_timestamp'] must be the exact
        values captured when the event was built — if the dict keys were
        ever wrong, _update_changelog_history's .get(key, time.time())
        fallback would silently recompute a LATER timestamp instead of
        reusing the original one. An increasing time.time() sequence makes
        that distinguishable: the very first call is the event's own
        creation time; any later call returns a strictly greater value."""
        import itertools

        em = self._make_em()
        iso_values = ["iso-first", "iso-later", "iso-later", "iso-later"]
        # log_discovery.info(...) fires unconditionally near the top of
        # _publish_dynamic_changes, before change_event is built. If some
        # other test in the suite leaves the 'nibe.discovery' logger at
        # INFO/DEBUG (order-dependent under pytest-randomly), that log
        # call's LogRecord creation calls the patched time.time() too,
        # consuming the first value from the sequence below and making
        # this test's "very first call" assumption false. Patching the
        # logger out removes that incidental consumer.
        with (
            patch("nibe_entity_manager.time.time", side_effect=itertools.count(1000.0, 1.0)),
            patch("nibe_entity_manager._fmt_ts", side_effect=iso_values),
            patch("nibe_entity_manager.log_discovery"),
        ):
            em._publish_dynamic_changes(
                new_points=[self._dynamic_point_data(50827)],
                disappeared_points=set(),
                controlling_point_id=5110,
            )
        entry = em.change_history[0]
        self.assertEqual(entry["timestamp"], 1000.0)
        self.assertEqual(entry["iso_timestamp"], "iso-first")

    def test_uses_snapshot_not_live_attribute_when_they_differ(self):
        """Regression test for the post-write misattribution race: the write
        executor doesn't block for the 90s scan window, so a second write
        (to a different controlling switch) can overwrite
        _post_write_controlling_point while an earlier scan cycle is still
        being processed. _publish_dynamic_changes must attribute changes to
        the controlling_point_id it was called with — a caller-supplied
        snapshot — not whatever self._post_write_controlling_point holds by
        the time this runs."""
        em = self._make_em()
        em.all_points_by_id[5111] = {
            "variableId": 5111,
            "display_title": "Some other switch",
            "entity_type": "switch",
            "entity_category": "config",
            "is_writable": True,
            "is_dynamic": False,
            "metadata": {
                "minValue": 0,
                "maxValue": 1,
                "divisor": 1,
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
            },
        }
        # Simulate a second write already having overwritten the live
        # attribute by the time this call happens.
        em._post_write_controlling_point = 5111

        em._publish_dynamic_changes(
            new_points=[self._dynamic_point_data(50827)],
            disappeared_points=set(),
            controlling_point_id=5110,  # the snapshot from THIS write's cycle
        )
        entry = em.change_history[0]
        trig = entry.get("triggered_by")
        self.assertEqual(
            trig["id"],
            5110,
            "must use the passed-in snapshot, not the live (possibly-since-overwritten) attribute",
        )
        self.assertEqual(trig["title"], "Prevent condensation climate system 1")


class TestUpdateEntityStateDynamicDisappearance(unittest.TestCase):
    """_update_entity_state routes post-write disappearance as dynamic change."""

    def test_post_write_absent_point_triggers_dynamic_change(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = True
        point_id = 9001
        em.mqtt_enabled_points.add(point_id)
        em.baseline_point_ids.add(point_id)
        em.active_dynamic_points.add(point_id)
        # point_id is NOT in bulk_data → triggers disappearance
        entity_info = {
            "point_id": point_id,
            "entity_type": "switch",
            "availability_topic": f"nibe/avail/{point_id}",
            "state_topic": f"nibe/state/{point_id}",
            "command_topic": None,
            "point_data": {},
        }
        with patch.object(em, "_publish_dynamic_changes") as mock_dyn:
            em._update_entity_state(entity_info)
        mock_dyn.assert_called_once()
        _, disappeared, *_rest = mock_dyn.call_args.args
        self.assertIn(point_id, disappeared)


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
        with patch.object(em, "publish_enabled_state"):
            em._publish_dynamic_changes([], {9999})
        # No dynamic event publish should happen since change_event is empty
        dynamic_calls = [c for c in em.mqtt.publish.call_args_list if "dynamic" in str(c)]
        self.assertEqual(dynamic_calls, [])


class TestPublishDynamicChangesMissingDescriptionKey(unittest.TestCase):
    """A new dynamic point whose API payload omits 'description' entirely
    must fall back to '' (not None) — detect_entity_type()'s HOLDING-register
    enum-syntax check does `'=' in description`, which crashes on None."""

    def test_missing_description_key_does_not_crash_holding_register_point(self):
        em = _make_em()
        point_data = {
            "variableId": 60001,
            "title": "No description key",
            "metadata": {
                "minValue": 0,
                "maxValue": 1,
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
                "isWritable": True,
            },
        }
        em._publish_dynamic_changes(
            new_points=[(60001, point_data)],
            disappeared_points=set(),
            controlling_point_id=None,
        )
        self.assertEqual(em.all_points_by_id[60001]["description"], "")


class TestPublishDynamicChangesDisablesEnabledPoint(unittest.TestCase):
    """_publish_dynamic_changes calls disable_entity for disappeared points that are enabled."""

    def _make_point(self, point_id):
        return {
            "variableId": point_id,
            "display_title": f"Point {point_id}",
            "entity_type": "switch",
            "entity_category": "config",
            "is_dynamic": True,
            "is_writable": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
            },
            "description": "",
        }

    def test_disappeared_enabled_point_calls_disable_entity(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 7777
        em.all_points_by_id[point_id] = self._make_point(point_id)
        em.mqtt_enabled_points.add(point_id)
        em.active_dynamic_points.add(point_id)
        # _publish_dynamic_changes acquires _em_lock and calls _disable_entity_locked
        # directly rather than the public disable_entity wrapper.
        with (
            patch.object(em, "_disable_entity_locked") as mock_disable,
            patch.object(em, "publish_enabled_state"),
            patch.object(em, "_persist_active_dynamic"),
        ):
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
            "variableId": point_id,
            "display_title": "Point 7777",
            "entity_type": "switch",
            "entity_category": "config",
            "is_dynamic": True,
            "is_writable": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
            },
            "description": "",
        }
        em.mqtt_enabled_points.add(point_id)
        em.active_dynamic_points.add(point_id)
        # Patch em._notify to raise a non-ValueError/TypeError/AttributeError
        # exception to exercise the bare `except Exception` branch (lines 1825-1826).
        em._notify = MagicMock(side_effect=OSError("unexpected"))
        with (
            patch.object(em, "publish_enabled_state"),
            patch.object(em, "disable_entity"),
            patch.object(em, "_persist_active_dynamic"),
        ):
            em._publish_dynamic_changes([], {point_id})  # must not raise


class TestPublishDynamicChangesProcessedDictAndIndexing(unittest.TestCase):
    """The `processed` dict built for each new point (fed to _index_point and
    _pub.publish_point_metadata) must use the real key names and real values —
    guards against key-name/default mutations in that block."""

    def test_processed_dict_has_correct_keys_and_isWritable_default(self):
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8001
        point_data = {
            "title": "Real Title",
            "description": "Real Description",
            "metadata": {},  # isWritable deliberately absent -> must default False
        }
        with (
            patch.object(em, "_get_cached_entity_type", return_value=("sensor", "diagnostic")),
            patch.object(em, "_index_point") as mock_index,
        ):
            em._publish_dynamic_changes([(pid, point_data)], set())
        indexed = mock_index.call_args.args[0]
        self.assertEqual(indexed["variableId"], pid)
        self.assertEqual(indexed["display_title"], "Real Title")
        self.assertEqual(indexed["description"], "Real Description")
        self.assertEqual(indexed["metadata"], {})
        self.assertEqual(indexed["entity_type"], "sensor")
        self.assertEqual(indexed["entity_category"], "diagnostic")
        self.assertIs(indexed["is_writable"], False)
        self.assertIs(indexed["is_dynamic"], True)

    def test_publish_point_metadata_called_with_same_processed_dict(self):
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8002
        point_data = {
            "title": "Meta Title",
            "description": "",
            "metadata": {"isWritable": True},
        }
        em._publish_dynamic_changes([(pid, point_data)], set())
        em._pub.publish_point_metadata.assert_called_once()
        published = em._pub.publish_point_metadata.call_args.args[0]
        self.assertEqual(published["variableId"], pid)
        self.assertIs(published["is_writable"], True)

    def test_title_defaults_to_point_fallback_when_missing(self):
        """title = clean_string(point_data.get('title', f'Point {point_id}')) —
        when the title key is genuinely absent, the fallback must use the
        real point_id, not a swapped/None value."""
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8003
        point_data = {"description": "", "metadata": {}}
        with patch.object(em, "_index_point") as mock_index:
            em._publish_dynamic_changes([(pid, point_data)], set())
        indexed = mock_index.call_args.args[0]
        self.assertEqual(indexed["display_title"], f"Point {pid}")


class TestPublishDynamicChangesChangeEventStructure(unittest.TestCase):
    """The published DYNAMIC-topic change_event and its 'added'/'removed'
    entries must contain the real values, not swapped/None/wrong keys."""

    def _capture_dynamic_publish(self, em):
        from nibe_entity_manager import BrowserTopic

        for call in em.mqtt.publish.call_args_list:
            args = call.args
            if args and args[0] == BrowserTopic.DYNAMIC:
                return json.loads(args[1])
        return None

    def test_triggered_by_title_falls_back_to_title_key_when_no_display_title(self):
        """ctrl_title = cp.get('display_title') or cp.get('title', ...) —
        the second tier ('title' key) must be reachable and use the real
        value, not a wrong key or the f'Point {id}' default."""
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 8020
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "title": "Fallback Ctrl Title",
            "entity_type": "switch",
            "metadata": {},
        }
        em.bulk_data[controlling_pid] = {"raw_value": 1}
        pid = 8021
        point_data = {"title": "Dyn", "description": "", "metadata": {}}
        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        event = self._capture_dynamic_publish(em)
        self.assertEqual(event["triggered_by"]["title"], "Fallback Ctrl Title")

    def test_triggered_by_title_falls_back_to_point_id_when_controlling_point_unknown(self):
        """When the controlling point isn't in all_points_by_id at all
        (cp defaults to {}), both fallback tiers miss and ctrl_title must
        become the final f'Point {id}' placeholder — not None or a crash."""
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 8022  # deliberately never added to all_points_by_id
        pid = 8023
        point_data = {"title": "Dyn", "description": "", "metadata": {}}
        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        event = self._capture_dynamic_publish(em)
        self.assertEqual(event["triggered_by"]["title"], f"Point {controlling_pid}")

    def test_publish_point_list_receives_the_real_point_index(self):
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8009
        point_data = {"title": "Marker Point", "description": "", "metadata": {}}
        em._publish_dynamic_changes([(pid, point_data)], set())
        em._pub.publish_point_list.assert_called_once_with(em.all_points_by_id)

    def test_added_entry_has_real_id_title_type_and_is_dynamic(self):
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8010
        point_data = {
            "title": "Distinct Added Title",
            "description": "",
            "metadata": {},
        }
        with patch.object(
            em, "_get_cached_entity_type", return_value=("binary_sensor", "diagnostic")
        ):
            em._publish_dynamic_changes([(pid, point_data)], set())
        event = self._capture_dynamic_publish(em)
        self.assertIsNotNone(event, "DYNAMIC topic must be published")
        self.assertEqual(len(event["added"]), 1)
        added = event["added"][0]
        self.assertEqual(added["id"], pid)
        self.assertEqual(added["title"], "Distinct Added Title")
        self.assertEqual(added["type"], "binary_sensor")
        self.assertIs(added["is_dynamic"], True)
        self.assertEqual(event["removed"], [])
        self.assertEqual(event["source"], "firmware")

    def test_removed_entry_has_real_id_title_and_type_from_all_points(self):
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8011
        em.all_points_by_id[pid] = {
            "variableId": pid,
            "display_title": "Distinct Removed Title",
            "entity_type": "switch",
            "entity_category": "config",
            "is_dynamic": True,
            "is_writable": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
            },
            "description": "",
        }
        em.mqtt_enabled_points.add(pid)
        em.active_dynamic_points.add(pid)
        with patch.object(em, "_disable_entity_locked"):
            em._publish_dynamic_changes([], {pid})
        event = self._capture_dynamic_publish(em)
        self.assertIsNotNone(event)
        self.assertEqual(len(event["removed"]), 1)
        removed = event["removed"][0]
        self.assertEqual(removed["id"], pid)
        self.assertEqual(removed["title"], "Distinct Removed Title")
        self.assertEqual(removed["type"], "switch")
        self.assertIs(removed["is_dynamic"], True)
        self.assertEqual(event["added"], [])

    def test_dynamic_topic_published_not_retained(self):
        """The DYNAMIC-topic publish must use retain=False — this is a
        transient change-notification, not a state topic to be
        re-delivered to late-joining subscribers."""
        from nibe_entity_manager import BrowserTopic

        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8012
        point_data = {"title": "T", "description": "", "metadata": {}}
        em._publish_dynamic_changes([(pid, point_data)], set())
        matching = [
            c
            for c in em.mqtt.publish.call_args_list
            if c.args and c.args[0] == BrowserTopic.DYNAMIC
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn("retain", matching[0].kwargs)
        self.assertIs(matching[0].kwargs["retain"], False)


class TestPublishDynamicChangesEnabledStateCalls(unittest.TestCase):
    """publish_enabled_state() is called once per truthy new_points/
    disappeared_points branch — both fire independently, so a single
    _publish_dynamic_changes call with BOTH additions and removals must
    call it twice."""

    def test_called_twice_when_both_new_and_disappeared_present(self):
        em = _make_em()
        em.initial_discovery_complete = True
        added_pid, removed_pid = 8020, 8021
        em.all_points_by_id[removed_pid] = {
            "variableId": removed_pid,
            "display_title": "Removed",
            "entity_type": "switch",
            "entity_category": "config",
            "is_dynamic": True,
            "is_writable": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
            },
            "description": "",
        }
        em.active_dynamic_points.add(removed_pid)
        point_data = {"title": "Added", "description": "", "metadata": {}}
        with patch.object(em, "publish_enabled_state") as mock_publish_state:
            em._publish_dynamic_changes([(added_pid, point_data)], {removed_pid})
        self.assertEqual(mock_publish_state.call_count, 2)

    def test_called_once_when_only_disappeared_points_present(self):
        """`if new_points: ...` and `if disappeared_points: ...` are two
        independent guards on the same call — with only disappeared_points
        truthy, publish_enabled_state must fire exactly once (not zero,
        not twice)."""
        em = _make_em()
        em.initial_discovery_complete = True
        # 9999 disappeared but is not in all_points_by_id -> no-op removal
        # bookkeeping-wise, but disappeared_points itself is still truthy.
        with patch.object(em, "publish_enabled_state") as mock_publish_state:
            em._publish_dynamic_changes([], {9999})
        mock_publish_state.assert_called_once()


class TestPublishDynamicChangesControllingPointMapEntry(unittest.TestCase):
    """When a controlling_point_id is supplied and new_points is non-empty,
    a first-seen controlling point must get a DynamicPointEntry created with
    the real min/max defaults and record_outcome must be called with the
    real controlling raw value and new point ids."""

    def test_new_controlling_entry_uses_real_min_max_defaults(self):
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9001
        # No minValue/maxValue in metadata -> must default to 0 and 1
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "display_title": "Ctrl Switch",
            "entity_type": "switch",
            "metadata": {},
        }
        em.bulk_data[controlling_pid] = {"raw_value": 1}
        pid = 9002
        point_data = {"title": "Dyn", "description": "", "metadata": {}}

        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        entry = em.dynamic_point_map.get(controlling_pid)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.unprocessed_values, set())  # value 1 got processed
        self.assertIn(pid, entry.dynamic_points_by_value.get(1, []))

    def test_new_controlling_entry_uses_real_min_max_from_metadata(self):
        """When the controlling point's metadata DOES have minValue/maxValue
        set to non-default, distinctive values, the created entry's
        unprocessed_values range must reflect them — a mistyped metadata
        or minValue/maxValue lookup key would silently fall back to the
        0/1 defaults instead, and a value=1 test point wouldn't reveal
        that (0/1 happens to already include 1)."""
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9010
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "display_title": "Ctrl Select",
            "entity_type": "select",
            "metadata": {"minValue": 5, "maxValue": 8},
        }
        em.bulk_data[controlling_pid] = {"raw_value": 6}
        pid = 9011
        point_data = {"title": "Dyn", "description": "", "metadata": {}}

        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        entry = em.dynamic_point_map.get(controlling_pid)
        self.assertIsNotNone(entry)
        # Range 5-8 minus the processed value 6 leaves {5, 7, 8} unprocessed.
        self.assertEqual(entry.unprocessed_values, {5, 7, 8})
        self.assertEqual(entry.point_id, controlling_pid)
        self.assertEqual(entry.title, "Ctrl Select")
        self.assertEqual(entry.entity_type, "select")

    def test_unknown_controlling_point_falls_back_without_crashing(self):
        """If the controlling point somehow isn't in all_points_by_id (e.g.
        a race with deindexing), the {} fallback must be used — not None,
        which would crash the very next .get('metadata', {}) call."""
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9040  # deliberately never added to all_points_by_id
        em.bulk_data[controlling_pid] = {"raw_value": 1}
        pid = 9041
        point_data = {"title": "Dyn", "description": "", "metadata": {}}

        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        entry = em.dynamic_point_map.get(controlling_pid)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.title, f"Point {controlling_pid}")
        self.assertEqual(entry.entity_type, "switch")

    def test_unknown_controlling_point_min_max_default_to_0_and_1(self):
        """minValue/maxValue must default to 0/1 specifically — using a
        3-value range (0,1,2) so the switch inverse-marking shortcut
        (which only applies to exactly-2-value entries) doesn't mask a
        wrong minValue/maxValue default."""
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9042
        em.bulk_data[controlling_pid] = {"raw_value": 2}
        pid = 9043
        point_data = {"title": "Dyn", "description": "", "metadata": {}}
        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        entry = em.dynamic_point_map.get(controlling_pid)
        self.assertIsNotNone(entry)
        # range(0, 2) = {0, 1} minus processed value 2 (not even in range,
        # since maxValue defaults to 1) -> unprocessed stays {0, 1}. A wrong
        # minValue default (e.g. 1) would instead leave just {1}.
        self.assertEqual(entry.unprocessed_values, {0, 1})

    def test_existing_controlling_entry_is_not_recreated(self):
        """When a DynamicPointEntry for the controlling point already
        exists, it must be reused, not silently replaced with a fresh one —
        recreating it would discard the accumulated learning-mode state
        (processed_values/unprocessed_values/is_controlling) from prior
        write cycles."""
        from nibe_dynamic_map import DynamicPointEntry

        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9030
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "display_title": "Ctrl Switch",
            "entity_type": "switch",
            "metadata": {"minValue": 0, "maxValue": 1},
        }
        em.bulk_data[controlling_pid] = {"raw_value": 1}
        # Pre-populate an entry as if a prior write cycle already fully
        # processed value 0 (leaving only 1 unprocessed) and had already
        # determined this point IS controlling.
        em.dynamic_point_map._table[controlling_pid] = DynamicPointEntry(
            point_id=controlling_pid,
            title="Ctrl Switch",
            entity_type="switch",
            processed_values={0},
            unprocessed_values={1},
            dynamic_points_by_value={0: [12345]},
            is_controlling=True,
        )

        pid = 9031
        point_data = {"title": "Dyn", "description": "", "metadata": {}}
        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )

        entry = em.dynamic_point_map.get(controlling_pid)
        # If the entry had been recreated, processed_values would be reset
        # to empty and the prior value-0 recording would be lost.
        self.assertIn(0, entry.processed_values)
        self.assertIn(12345, entry.dynamic_points_by_value.get(0, []))

    def test_new_controlling_entry_title_falls_back_to_title_key_then_default(self):
        """title = display_title or title-key or f'Point {id}' — each
        fallback tier must be reachable and use the real value, not a
        wrong key or inverted 'and' short-circuit."""
        em = _make_em()
        em.initial_discovery_complete = True

        # Tier 2: no display_title, but 'title' key present.
        pid_a = 9020
        em.all_points_by_id[pid_a] = {
            "variableId": pid_a,
            "entity_type": "switch",
            "title": "Fallback Title",
            "metadata": {},
        }
        em.bulk_data[pid_a] = {"raw_value": 1}
        em._publish_dynamic_changes(
            [(9021, {"title": "Dyn", "description": "", "metadata": {}})],
            set(),
            controlling_point_id=pid_a,
        )
        self.assertEqual(em.dynamic_point_map.get(pid_a).title, "Fallback Title")

        # Tier 3: neither display_title nor title present -> f'Point {id}'.
        pid_b = 9022
        em.all_points_by_id[pid_b] = {"variableId": pid_b, "entity_type": "switch", "metadata": {}}
        em.bulk_data[pid_b] = {"raw_value": 1}
        em._publish_dynamic_changes(
            [(9023, {"title": "Dyn2", "description": "", "metadata": {}})],
            set(),
            controlling_point_id=pid_b,
        )
        self.assertEqual(em.dynamic_point_map.get(pid_b).title, f"Point {pid_b}")

    def test_record_outcome_uses_real_controlling_raw_value(self):
        """controlling_raw = bulk_data.get(controlling, {}).get('raw_value', 1) —
        must reflect the actual bulk_data value, not the (1) default, when
        bulk_data has a distinct value."""
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9003
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "display_title": "Ctrl Select",
            "entity_type": "select",
            "metadata": {"minValue": 0, "maxValue": 3},
        }
        em.bulk_data[controlling_pid] = {"raw_value": 2}  # distinctive, non-default
        pid = 9004
        point_data = {"title": "Dyn2", "description": "", "metadata": {}}

        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        entry = em.dynamic_point_map.get(controlling_pid)
        self.assertIn(pid, entry.dynamic_points_by_value.get(2, []))
        self.assertNotIn(2, entry.unprocessed_values)

    def test_enable_failure_not_recorded_in_dynamic_point_map(self):
        """Regression: record_outcome used to be called with the raw
        new_points list, including points whose _enable_entity_locked call
        failed. Those points are deliberately left out of
        active_dynamic_points (per the comment at the `continue` above) so
        a future poll can retry activation — but self.published_configs is
        unconditionally set to the full current-poll id set every cycle in
        _fetch_bulk_data, so a point that already appeared once in bulk
        data can never re-enter new_points on a later poll. If
        record_outcome still recorded it as 'known' despite the failed
        enable, dynamic_point_map would believe it's an expected dynamic
        point HA was never actually told about, with no path left to ever
        retry — the promised retry would silently never happen. Only
        successfully-enabled points must be recorded."""
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9005
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "display_title": "Ctrl Switch",
            "entity_type": "switch",
            "metadata": {},
        }
        em.bulk_data[controlling_pid] = {"raw_value": 1}
        failing_pid = 9006
        succeeding_pid = 9007
        new_points = [
            (failing_pid, {"title": "Fails", "description": "", "metadata": {}}),
            (succeeding_pid, {"title": "Succeeds", "description": "", "metadata": {}}),
        ]

        real_enable = em._enable_entity_locked

        def fake_enable(point_id):
            if point_id == failing_pid:
                return False
            return real_enable(point_id)

        with patch.object(em, "_enable_entity_locked", side_effect=fake_enable):
            em._publish_dynamic_changes(
                new_points,
                set(),
                controlling_point_id=controlling_pid,
            )

        entry = em.dynamic_point_map.get(controlling_pid)
        recorded_pids = entry.dynamic_points_by_value.get(1, [])
        self.assertIn(succeeding_pid, recorded_pids)
        self.assertNotIn(
            failing_pid,
            recorded_pids,
            "a point whose discovery publish failed must not be recorded "
            "as a known dynamic point — it was never actually enabled in HA",
        )
        self.assertNotIn(failing_pid, em.active_dynamic_points)

    def test_controlling_raw_defaults_to_one_when_bulk_data_missing(self):
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9005
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "display_title": "Ctrl Switch2",
            "entity_type": "switch",
            "metadata": {},
        }
        # deliberately no bulk_data entry for controlling_pid
        pid = 9006
        point_data = {"title": "Dyn3", "description": "", "metadata": {}}

        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        entry = em.dynamic_point_map.get(controlling_pid)
        self.assertIn(pid, entry.dynamic_points_by_value.get(1, []))


class TestPublishDynamicChangesNotificationContent(unittest.TestCase):
    """The HA persistent-notification message for added/removed points must
    contain the real title/point id/count/triggered-by text — not swapped
    or blanked-out content."""

    def test_added_notification_contains_real_title_and_id_and_count(self):
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8030
        point_data = {
            "title": "Unique Added Notification Title",
            "description": "",
            "metadata": {},
        }
        with patch.object(em, "_read_applied_mode_from_file", return_value="menus"):
            em._publish_dynamic_changes([(pid, point_data)], set())
        em._notify.assert_called_once()
        self.assertEqual(
            em._notify.call_args.args[0],
            em.mqtt,
            "_notify must be called with the real mqtt client, not None",
        )
        kwargs = em._notify.call_args.kwargs
        self.assertEqual(kwargs["notification_id"], "nibe_dashboard_updated")
        self.assertIn("Unique Added Notification Title", kwargs["message"])
        self.assertIn(f"point {pid}", kwargs["message"])
        self.assertIn("1 new setting(s)", kwargs["message"])
        self.assertIn("Nibe Menus", kwargs["title"])

    def test_removed_notification_contains_real_title_and_count(self):
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8031
        em.all_points_by_id[pid] = {
            "variableId": pid,
            "display_title": "Unique Removed Notification Title",
            "entity_type": "switch",
            "entity_category": "config",
            "is_dynamic": True,
            "is_writable": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
            },
            "description": "",
        }
        em.active_dynamic_points.add(pid)
        with patch.object(em, "_read_applied_mode_from_file", return_value="menus"):
            em._publish_dynamic_changes([], {pid})
        em._notify.assert_called_once()
        kwargs = em._notify.call_args.kwargs
        self.assertIn("Unique Removed Notification Title", kwargs["message"])
        self.assertIn(f"point {pid}", kwargs["message"])
        self.assertIn("1 setting(s)", kwargs["message"])

    def test_notification_wording_when_not_in_menus_mode_does_not_reference_menus_dashboard(self):
        """The Nibe Menus dashboard only exists when mode == 'menus' (see
        nibe_lovelace.py's provision_lovelace_ui docstring) — in any other
        mode the notification must point at the Nibe Bridge dashboard
        (provisioned in every mode) instead, not a dashboard that was never
        created."""
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8032
        point_data = {
            "title": "Non-Menus Mode Point",
            "description": "",
            "metadata": {},
        }
        with patch.object(em, "_read_applied_mode_from_file", return_value="none"):
            em._publish_dynamic_changes([(pid, point_data)], set())
        em._notify.assert_called_once()
        kwargs = em._notify.call_args.kwargs
        self.assertNotIn("Nibe Menus", kwargs["title"])
        self.assertNotIn("Nibe Menus", kwargs["message"])
        self.assertIn("/nibe-bridge", kwargs["message"])
        self.assertIn("Non-Menus Mode Point", kwargs["message"])
        self.assertIn(f"point {pid}", kwargs["message"])

    def test_notification_wording_when_applied_mode_file_absent_uses_non_menus_wording(self):
        """No applied-mode record (e.g. fresh install, or a test/dev
        environment with no /data/applied_mode) must default to the
        dashboard-agnostic wording, not assume 'menus' — read_applied_mode_
        from_file() returns None in that case, which must not equal 'menus'."""
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 8033
        point_data = {
            "title": "No Mode Record Point",
            "description": "",
            "metadata": {},
        }
        with patch.object(em, "_read_applied_mode_from_file", return_value=None):
            em._publish_dynamic_changes([(pid, point_data)], set())
        kwargs = em._notify.call_args.kwargs
        self.assertNotIn("Nibe Menus", kwargs["title"])
        self.assertIn("/nibe-bridge", kwargs["message"])

    def test_notification_includes_triggered_by_line_when_controlling_point_set(self):
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9010
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "display_title": "The Controlling Switch",
            "entity_type": "switch",
            "metadata": {},
        }
        em.bulk_data[controlling_pid] = {"raw_value": 1}
        pid = 9011
        point_data = {"title": "Triggered Point", "description": "", "metadata": {}}
        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        kwargs = em._notify.call_args.kwargs
        self.assertIn("Triggered by", kwargs["message"])
        self.assertIn("The Controlling Switch", kwargs["message"])

    def test_no_triggered_by_line_when_no_controlling_point(self):
        em = _make_em()
        em.initial_discovery_complete = True
        pid = 9012
        point_data = {"title": "Untriggered Point", "description": "", "metadata": {}}
        with patch.object(em, "_read_applied_mode_from_file", return_value="menus"):
            em._publish_dynamic_changes([(pid, point_data)], set())
        kwargs = em._notify.call_args.kwargs
        self.assertNotIn("Triggered by", kwargs["message"])
        # ctrl_menu/menu_line must stay '' when there's no controlling point
        # (trig is None, so the `if trig:` block that would otherwise set
        # ctrl_menu never runs) — a wrong initial value would leak an
        # " in <garbage>" fragment into the message header. Likewise
        # ctrl_line (built from `if ctrl_title else ''`) must contribute
        # nothing — checked by requiring the exact header text immediately
        # followed by the double-newline before the added/removed sections.
        self.assertIn(
            "The Nibe Menus dashboard was updated:\n\n1 new setting(s)",
            kwargs["message"],
        )

    def test_notification_includes_real_menu_entry_when_controlling_point_has_one(self):
        """When the controlling point is mapped in point_to_menu_map, the
        notification's menu_line must use that REAL (menu_id, menu_name)
        entry — a wrong lookup key or a hardcoded None would silently drop
        the menu location from the message."""
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9013
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "display_title": "Ctrl",
            "entity_type": "switch",
            "metadata": {},
        }
        em.bulk_data[controlling_pid] = {"raw_value": 1}
        em.point_to_menu_map[controlling_pid] = ("7", "Distinctive Menu Name")
        pid = 9014
        point_data = {"title": "Dyn", "description": "", "metadata": {}}
        em._publish_dynamic_changes(
            [(pid, point_data)],
            set(),
            controlling_point_id=controlling_pid,
        )
        kwargs = em._notify.call_args.kwargs
        self.assertIn("menu 7 — Distinctive Menu Name", kwargs["message"])

    def test_no_menu_fragment_when_controlling_point_has_no_menu_mapping(self):
        """A controlling point with no point_to_menu_map entry must leave
        ctrl_menu/menu_line empty — not a placeholder — since the message
        format only inserts ' in {ctrl_menu}' when ctrl_menu is truthy."""
        em = _make_em()
        em.initial_discovery_complete = True
        controlling_pid = 9015
        em.all_points_by_id[controlling_pid] = {
            "variableId": controlling_pid,
            "display_title": "Ctrl",
            "entity_type": "switch",
            "metadata": {},
        }
        em.bulk_data[controlling_pid] = {"raw_value": 1}
        # No entry in em.point_to_menu_map for controlling_pid.
        pid = 9016
        point_data = {"title": "Dyn", "description": "", "metadata": {}}
        with patch.object(em, "_read_applied_mode_from_file", return_value="menus"):
            em._publish_dynamic_changes(
                [(pid, point_data)],
                set(),
                controlling_point_id=controlling_pid,
            )
        kwargs = em._notify.call_args.kwargs
        self.assertIn("The Nibe Menus dashboard was updated:", kwargs["message"])

    def test_added_and_removed_in_same_call_both_appear_in_one_notification(self):
        """Regression: added and removed both used the same hardcoded
        notification_id ('nibe_dashboard_updated'). notify_ha() creates-or-
        REPLACES a persistent notification by that ID, so sending two
        separate notifications (one for added, one for removed) in the same
        call — plausible whenever a mode-select point swaps one dynamic set
        for another in a single poll cycle — silently dropped whichever was
        sent first. Both must now be combined into one notification."""
        em = _make_em()
        em.initial_discovery_complete = True
        added_pid = 8040
        added_point_data = {
            "title": "Newly Added Point",
            "description": "",
            "metadata": {},
        }
        removed_pid = 8041
        em.all_points_by_id[removed_pid] = {
            "variableId": removed_pid,
            "display_title": "Newly Removed Point",
            "entity_type": "switch",
            "entity_category": "config",
            "is_dynamic": True,
            "is_writable": True,
            "metadata": {
                "variableSize": "u8",
                "divisor": 1,
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
            },
            "description": "",
        }
        em.active_dynamic_points.add(removed_pid)
        em._publish_dynamic_changes([(added_pid, added_point_data)], {removed_pid})
        # Exactly one notification, not two — and it must mention both sides.
        em._notify.assert_called_once()
        kwargs = em._notify.call_args.kwargs
        self.assertEqual(kwargs["notification_id"], "nibe_dashboard_updated")
        self.assertIn("Newly Added Point", kwargs["message"])
        self.assertIn("Newly Removed Point", kwargs["message"])


class TestDynamicLearningDetection(unittest.TestCase):
    """_run_learning_detection: size-change exit and deadline exit."""

    def _em_with_bulk(self, initial_size=1):
        em = _make_em()
        for i in range(initial_size):
            em.bulk_data[i] = {
                "raw_value": 0,
                "string_value": "",
                "is_ok": True,
                "metadata": {},
                "title": f"P{i}",
            }
        return em

    def test_size_change_exits_loop_early(self):
        em = self._em_with_bulk(initial_size=2)
        sleep_calls = [0]

        def fake_sleep(t):
            sleep_calls[0] += 1
            em.bulk_data[999] = {
                "raw_value": 1,
                "string_value": "",
                "is_ok": True,
                "metadata": {},
                "title": "New",
            }

        with (
            patch("nibe_entity_manager.time.sleep", side_effect=fake_sleep),
            patch("nibe_entity_manager.time.time", return_value=0.0),
            patch.object(em.dynamic_point_map, "record_outcome") as mock_rec,
            patch.object(em, "_persist_dynamic_map"),
        ):
            em._run_learning_detection(5, 1, "test")
        self.assertEqual(sleep_calls[0], 1)
        mock_rec.assert_called_once_with(5, 1, [999])

    def test_deadline_exit_records_empty_outcome(self):
        em = self._em_with_bulk(initial_size=1)
        # _post_write_until, deadline, loop check — exactly 3 real time.time()
        # calls, padded with a repeating tail so a 4th call (if one ever
        # happens) returns 999.0 forever rather than raising StopIteration.
        #
        # log_commands.info() is explicitly mocked out below (not just left
        # to ambient logger state) because LogRecord creation internally
        # calls time.time() *whenever the logger's effective level allows
        # the info() call through* — which depends on global logging state
        # other tests may have left behind. Previously this test only
        # padded the iterator to survive that extra call without crashing;
        # it didn't stop the extra call from silently shifting deadline's
        # value to 999.0 + 90 while the loop's own "now" stayed pinned at
        # 999.0 forever — deadline could never be reached, and since
        # time.sleep is also mocked to a no-op, the loop spun as a genuine
        # CPU-bound infinite loop until pytest-timeout killed it. That
        # depended on test execution order (whether the logger happened to
        # be INFO-enabled already), so it passed locally under one
        # pytest-randomly seed and hung for real under another — this is
        # what actually happened in CI. Mocking log_commands.info()
        # unconditionally removes the ambiguity: exactly 3 time.time()
        # calls, every time, regardless of what ran before this test.
        time_seq = itertools.chain([0.0, 0.0, 999.0], itertools.repeat(999.0))
        with (
            patch("nibe_entity_manager.time.sleep"),
            patch("nibe_entity_manager.time.time", side_effect=time_seq),
            patch("nibe_entity_manager.log_commands"),
            patch.object(em.dynamic_point_map, "record_outcome") as mock_rec,
            patch.object(em, "_persist_dynamic_map"),
        ):
            em._run_learning_detection(10, 0, "test")
        mock_rec.assert_called_once_with(10, 0, [])

    def test_post_write_until_and_poll_interval_use_correct_values(self):
        """_post_write_until must be time.time() + _POST_WRITE_SCAN_S (not None
        or a subtraction), and time.sleep must be called with the configured
        post_write_interval (not None)."""
        em = self._em_with_bulk(initial_size=1)
        em.post_write_interval = 7.5
        captured_sleep_args = []

        def fake_sleep(t):
            captured_sleep_args.append(t)
            em.bulk_data[999] = {
                "raw_value": 1,
                "string_value": "",
                "is_ok": True,
                "metadata": {},
                "title": "New",
            }

        with (
            patch("nibe_entity_manager.time.sleep", side_effect=fake_sleep),
            patch("nibe_entity_manager.time.time", return_value=1000.0),
            patch.object(em.dynamic_point_map, "record_outcome"),
            patch.object(em, "_persist_dynamic_map"),
        ):
            em._run_learning_detection(5, 1, "test")
        self.assertEqual(em._post_write_until, 1000.0 + 90)
        self.assertEqual(captured_sleep_args, [7.5])

    def test_deadline_uses_addition_not_subtraction(self):
        """deadline = time.time() + _POST_WRITE_SCAN_S. If it were a
        subtraction, the loop would exit on its first iteration instead of
        continuing to poll."""
        em = self._em_with_bulk(initial_size=1)
        time_seq = itertools.chain([100.0, 100.0, 50.0, 999.0], itertools.repeat(999.0))
        with (
            patch("nibe_entity_manager.time.sleep") as mock_sleep,
            patch("nibe_entity_manager.time.time", side_effect=time_seq),
            patch("nibe_entity_manager.log_commands"),
            patch.object(em.dynamic_point_map, "record_outcome"),
            patch.object(em, "_persist_dynamic_map"),
        ):
            em._run_learning_detection(5, 1, "test")
        self.assertEqual(mock_sleep.call_count, 2)

    def test_deadline_boundary_is_reached_when_equal(self):
        """The exit check is `time.time() >= deadline` (inclusive), not a
        strict `>`. At exact equality the loop must exit on the first
        iteration."""
        em = self._em_with_bulk(initial_size=1)
        time_seq = itertools.chain([100.0, 100.0, 190.0], itertools.repeat(999.0))
        with (
            patch("nibe_entity_manager.time.sleep") as mock_sleep,
            patch("nibe_entity_manager.time.time", side_effect=time_seq),
            patch("nibe_entity_manager.log_commands"),
            patch.object(em.dynamic_point_map, "record_outcome"),
            patch.object(em, "_persist_dynamic_map"),
        ):
            em._run_learning_detection(5, 1, "test")
        self.assertEqual(mock_sleep.call_count, 1)


class TestSetupDynamicMapLoadingCallbacks(unittest.TestCase):
    """on_dynamic_map_message and on_active_dynamic_message handlers."""

    def _make_message(self, payload):
        msg = MagicMock()
        msg.payload = payload
        return msg

    def _make_em_with_dynamic_loading(self):
        """EM with real _setup_dynamic_map_loading (not patched out)."""
        with (
            patch("nibe_entity_manager.EntityManager.resubscribe_all"),
            patch("nibe_entity_manager.EntityManager._setup_history_loading"),
        ):
            from nibe_entity_manager import EntityManager

            em = EntityManager(
                api_client=MagicMock(),
                publisher=MagicMock(),
                notify_fn=MagicMock(),
                dismiss_fn=MagicMock(),
                mqtt_client=MagicMock(),
            )
        em.device_info = {}
        em.device_name = "Test"
        return em

    def test_wanted_points_message_restores_set(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        em._on_wanted_points_message(None, None, self._make_message(b"[1, 2, 3]"))
        self.assertEqual(em._wanted_points, {1, 2, 3})

    def test_wanted_points_message_ignored_after_discovery_complete(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = True
        em._on_wanted_points_message(None, None, self._make_message(b"[1, 2, 3]"))
        self.assertEqual(em._wanted_points, set())

    def test_dynamic_map_ignored_after_discovery_complete(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = True
        with patch.object(em.dynamic_point_map, "deserialise") as mock_deser:
            em._on_dynamic_map_message(None, None, self._make_message(b"{}"))
        mock_deser.assert_not_called()

    def test_dynamic_map_empty_payload_skipped(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with patch.object(em.dynamic_point_map, "deserialise") as mock_deser:
            em._on_dynamic_map_message(None, None, self._make_message(b""))
        mock_deser.assert_not_called()

    def test_dynamic_map_plain_json_loads(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with patch.object(em.dynamic_point_map, "deserialise") as mock_deser:
            em._on_dynamic_map_message(None, None, self._make_message(b"{}"))
        mock_deser.assert_called_once_with("{}")

    def test_dynamic_map_bad_payload_does_not_crash(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with patch.object(em.dynamic_point_map, "deserialise", side_effect=RuntimeError("boom")):
            em._on_dynamic_map_message(None, None, self._make_message(b"{}"))

    def test_active_dynamic_ignored_after_discovery_complete(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = True
        em._on_active_dynamic_message(None, None, self._make_message(b"[1,2,3]"))
        self.assertFalse(em.active_dynamic_points)

    def test_active_dynamic_empty_payload_skipped(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        em._on_active_dynamic_message(None, None, self._make_message(b""))
        self.assertFalse(em.active_dynamic_points)

    def test_active_dynamic_non_list_skipped(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        em._on_active_dynamic_message(None, None, self._make_message(json.dumps({"a": 1}).encode()))
        self.assertFalse(em.active_dynamic_points)

    def test_active_dynamic_valid_payload_updates_set(self):
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        em._on_active_dynamic_message(
            None, None, self._make_message(json.dumps([100, 200]).encode())
        )
        self.assertIn(100, em.active_dynamic_points)
        self.assertIn(200, em.active_dynamic_points)

    def test_dynamic_map_success_logs_count(self):
        """The success log call must carry the real deserialise() count, not None
        or an unrelated value — guards against the count argument being dropped
        or replaced."""
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with (
            patch.object(em.dynamic_point_map, "deserialise", return_value=7),
            patch("nibe_entity_manager.log_discovery") as mock_log,
        ):
            em._on_dynamic_map_message(None, None, self._make_message(b"{}"))
        mock_log.info.assert_called_once_with("Restored DynamicPointMap from MQTT: %d entries", 7)

    def test_dynamic_map_failure_logs_the_exception(self):
        """The warning() call on parse failure must be given the actual exception
        object raised by deserialise(), not None or a literal — otherwise the
        operator loses the diagnostic reason in the logs."""
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        boom = RuntimeError("bad table")
        with (
            patch.object(em.dynamic_point_map, "deserialise", side_effect=boom),
            patch("nibe_entity_manager.log_discovery") as mock_log,
        ):
            em._on_dynamic_map_message(None, None, self._make_message(b"{}"))
        mock_log.warning.assert_called_once_with(
            "Could not restore DynamicPointMap from MQTT — "
            "will try file fallback or start fresh: %s",
            boom,
        )

    def test_active_dynamic_success_logs_real_count_and_sorted_ids(self):
        """The success log call must report the real loaded-count and the
        actually-sorted id list, not a swapped/None argument."""
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with patch("nibe_entity_manager.log_discovery") as mock_log:
            em._on_active_dynamic_message(
                None, None, self._make_message(json.dumps([300, 100, 200]).encode())
            )
        mock_log.info.assert_called_once_with(
            "Restored %d active dynamic point(s) from MQTT: %s",
            3,
            [100, 200, 300],
        )

    def test_active_dynamic_arbitrary_exception_does_not_escape(self):
        """Regression: on_active_dynamic_message's except clause was
        narrowed to a fixed list of decode/parse exception types, unlike its
        sibling on_dynamic_map_message which deliberately catches Exception
        because both callbacks run on paho's MQTT network thread — any
        exception that escapes here permanently kills that thread and stops
        all future MQTT message delivery for the process's life. Simulate an
        exception type outside the old narrow list (e.g. a bug in
        active_dynamic_points.update) and confirm it's still swallowed."""
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with patch("nibe_entity_manager.log_discovery") as mock_log:
            mock_log.info.side_effect = RuntimeError("boom")
            em._on_active_dynamic_message(
                None, None, self._make_message(json.dumps([1, 2]).encode())
            )  # must not raise

    def test_active_dynamic_failure_logs_the_exception(self):
        """A malformed but non-crashing payload path (e.g. int() raising) must
        log the real exception object, not None or a literal."""
        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False
        with patch("nibe_entity_manager.log_discovery") as mock_log:
            em._on_active_dynamic_message(
                None, None, self._make_message(json.dumps(["not-an-int"]).encode())
            )
        self.assertEqual(mock_log.warning.call_count, 1)
        args = mock_log.warning.call_args.args
        self.assertEqual(args[0], "Could not restore active_dynamic_points from MQTT: %s")
        self.assertIsInstance(args[1], ValueError)

    def test_subscribes_and_registers_callbacks_on_correct_topics(self):
        """Verifies subscribe()/message_callback_add() are wired to the real
        DYNAMIC_MAP / ACTIVE_DYNAMIC topics and their matching handlers — not
        None, a swapped topic, or a swapped callback."""
        from nibe_entity_manager import BrowserTopic

        em = self._make_em_with_dynamic_loading()

        subscribe_topics = [c.args[0] for c in em.mqtt.subscribe.call_args_list]
        self.assertEqual(
            subscribe_topics,
            [BrowserTopic.DYNAMIC_MAP, BrowserTopic.ACTIVE_DYNAMIC, BrowserTopic.WANTED_POINTS],
        )

        cb_calls = em.mqtt.message_callback_add.call_args_list
        self.assertEqual(len(cb_calls), 3)
        self.assertEqual(cb_calls[0].args[0], BrowserTopic.DYNAMIC_MAP)
        self.assertEqual(cb_calls[0].args[1], em._on_dynamic_map_message)
        self.assertEqual(cb_calls[1].args[0], BrowserTopic.ACTIVE_DYNAMIC)
        self.assertEqual(cb_calls[1].args[1], em._on_active_dynamic_message)
        self.assertEqual(cb_calls[2].args[0], BrowserTopic.WANTED_POINTS)
        self.assertEqual(cb_calls[2].args[1], em._on_wanted_points_message)

    """_reconcile_dynamic_points returns early when initial discovery not complete."""

    def test_returns_early_before_discovery_complete(self):
        em = _make_em()
        em.initial_discovery_complete = False
        with patch.object(em.dynamic_point_map, "expected_active_dynamic_points") as mock_exp:
            em._reconcile_dynamic_points()
        mock_exp.assert_not_called()


class TestReconcileDynamicPointsCases(unittest.TestCase):
    """_reconcile_dynamic_points: case 1 (activate), case 2 (remove absent),
    case 3 (stale entry removal)."""

    def _bulk_entry(self, point_id):
        return {
            "raw_value": 1,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
                "isWritable": False,
                "variableType": "integer",
                "variableSize": "u8",
                "divisor": 1,
            },
            "title": f"Point {point_id}",
            "description": "",
        }

    def test_expected_and_present_not_enabled_gets_activated(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 1001
        em.bulk_data[point_id] = self._bulk_entry(point_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        # _reconcile_dynamic_points acquires _em_lock and calls _enable_entity_locked directly
        with (
            patch.object(em, "_enable_entity_locked") as mock_enable,
            patch.object(em, "_index_point"),
        ):
            em._reconcile_dynamic_points()
        mock_enable.assert_called_once_with(point_id)

    def test_current_raw_values_computed_and_passed_to_expected_active(self):
        """current_raw_values (built from bulk_data) must actually be
        computed correctly and passed to expected_active_dynamic_points —
        every other test here mocks that method's return value but never
        inspects what it was called WITH, so a mutation to the dict
        comprehension itself (or to the argument passed) previously went
        completely uncaught."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.bulk_data[1001] = self._bulk_entry(1001)
        em.bulk_data[1001]["raw_value"] = 42
        em.bulk_data[2002] = self._bulk_entry(2002)
        em.bulk_data[2002]["raw_value"] = 7
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value=set())
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        em._reconcile_dynamic_points()
        em.dynamic_point_map.expected_active_dynamic_points.assert_called_once_with(
            {1001: 42, 2002: 7}
        )

    def test_activated_point_uses_real_title_not_fallback(self):
        """The reactivated point's real title must be used — a lookup-key
        regression (e.g. self.bulk_data.get(None, {}) instead of
        .get(point_id, {})) would always fall through to the generic
        'Point {id}' fallback instead. The other fixture in this class
        happens to use a title that already looks like that fallback, so
        this test deliberately uses a distinct title to actually catch it."""
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 1001
        entry = self._bulk_entry(point_id)
        entry["title"] = "Distinctive Real Title"
        em.bulk_data[point_id] = entry
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with (
            patch.object(em, "_enable_entity_locked"),
            patch.object(em, "_index_point") as mock_index,
        ):
            em._reconcile_dynamic_points()
        indexed = mock_index.call_args.args[0]
        self.assertEqual(indexed["display_title"], "Distinctive Real Title")

    def test_activation_failure_does_not_mark_point_active(self):
        """If _enable_entity_locked fails (e.g. discovery publish failed),
        the point must not be added to active_dynamic_points — otherwise
        the bridge's own bookkeeping would falsely claim a point is active
        in HA when no discovery config was ever actually published."""
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 1001
        em.bulk_data[point_id] = self._bulk_entry(point_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with (
            patch.object(em, "_enable_entity_locked", return_value=False),
            patch.object(em, "_index_point"),
        ):
            em._reconcile_dynamic_points()
        self.assertNotIn(point_id, em.active_dynamic_points)

    def test_expected_but_absent_removes_from_active(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 1002
        # point not in bulk_data but in active_dynamic_points
        em.active_dynamic_points.add(point_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        # _reconcile_dynamic_points acquires _em_lock and calls _disable_entity_locked directly
        with patch.object(em, "_disable_entity_locked"), patch.object(em, "_deindex_point"):
            em._reconcile_dynamic_points()
        self.assertNotIn(point_id, em.active_dynamic_points)

    def test_activating_multiple_points_publishes_enabled_state_once(self):
        """Regression: _reconcile_dynamic_points must suppress
        publish_enabled_state() for the duration of its enable/disable
        loops, like apply_mode does — otherwise each individual (real,
        unmocked) _enable_entity_locked call fires its own
        publish_enabled_state(), flooding MQTT with N intermediate publishes
        instead of the single one at the end of this method. Uses the real
        _enable_entity_locked (only mocking publish_entity_discovery, its
        actual MQTT-facing dependency) so the internal publish_enabled_state
        call inside it isn't mocked away, unlike the other tests in this
        class which mock _enable_entity_locked itself and so can't see this
        flood."""
        em = _make_em()
        em.initial_discovery_complete = True
        point_ids = {1001, 1002, 1003}
        for pid in point_ids:
            em.bulk_data[pid] = self._bulk_entry(pid)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value=point_ids)
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        em._pub.publish_entity_discovery.side_effect = lambda point, bulk_data: {
            "point_id": point["variableId"],
            "entity_type": "switch",
            "availability_topic": f"homeassistant/switch/nibe_{point['variableId']}/available",
            "state_topic": f"homeassistant/switch/nibe_{point['variableId']}/state",
            "command_topic": None,
            "entity_id": f"nibe_{point['variableId']}",
        }
        with patch.object(em, "publish_enabled_state") as mock_publish:
            em._reconcile_dynamic_points()
        self.assertEqual(point_ids, em.active_dynamic_points)
        mock_publish.assert_called_once()

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
        # _reconcile_dynamic_points acquires _em_lock and calls _disable_entity_locked directly
        with patch.object(em, "_disable_entity_locked"), patch.object(em, "_deindex_point"):
            em._reconcile_dynamic_points()
        self.assertNotIn(stale_id, em.active_dynamic_points)

    def test_activated_dict_uses_correct_keys_and_isWritable_default(self):
        """The dicts built for _get_cached_entity_type/_index_point must use the
        real key names ('metadata', 'title', 'description', 'variableId',
        'entity_type', ...) and metadata.get('isWritable', False) must default
        to False (not None/True) when the API omits the key entirely — guards
        against key-name and default-value mutations in this block."""
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 1001
        entry = self._bulk_entry(point_id)
        entry["metadata"] = {}  # isWritable deliberately absent
        entry["title"] = "Real Title"
        entry["description"] = "Real Description"
        em.bulk_data[point_id] = entry
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with (
            patch.object(em, "_enable_entity_locked"),
            patch.object(
                em, "_get_cached_entity_type", return_value=("sensor", "diagnostic")
            ) as mock_detect,
            patch.object(em, "_index_point") as mock_index,
        ):
            em._reconcile_dynamic_points()
        detect_arg = mock_detect.call_args.args[0]
        self.assertEqual(detect_arg["variableId"], point_id)
        self.assertEqual(detect_arg["metadata"], {})
        self.assertEqual(detect_arg["title"], "Real Title")
        self.assertEqual(detect_arg["description"], "Real Description")
        indexed = mock_index.call_args.args[0]
        self.assertEqual(indexed["variableId"], point_id)
        self.assertEqual(indexed["display_title"], "Real Title")
        self.assertEqual(indexed["description"], "Real Description")
        self.assertEqual(indexed["metadata"], {})
        self.assertEqual(indexed["entity_type"], "sensor")
        self.assertEqual(indexed["entity_category"], "diagnostic")
        self.assertIs(indexed["is_writable"], False)
        self.assertIs(indexed["is_dynamic"], True)

    def test_activated_dict_is_writable_true_propagates(self):
        """metadata['isWritable']=True must reach indexed['is_writable']
        unchanged — a mistyped lookup key would silently fall back to the
        False default and mark a writable point read-only."""
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 1004
        entry = self._bulk_entry(point_id)
        entry["metadata"] = {"isWritable": True}
        em.bulk_data[point_id] = entry
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with (
            patch.object(em, "_enable_entity_locked"),
            patch.object(em, "_get_cached_entity_type", return_value=("sensor", "diagnostic")),
            patch.object(em, "_index_point") as mock_index,
        ):
            em._reconcile_dynamic_points()
        indexed = mock_index.call_args.args[0]
        self.assertIs(indexed["is_writable"], True)

    def test_activation_continues_to_next_point_after_one_fails(self):
        """If _enable_entity_locked fails for one expected point, the loop
        must `continue` to the next point rather than aborting the whole
        reconciliation — a second, independently-succeeding point in the
        same expected set must still be activated."""
        em = _make_em()
        em.initial_discovery_complete = True
        failing_id, succeeding_id = 1001, 1002
        em.bulk_data[failing_id] = self._bulk_entry(failing_id)
        em.bulk_data[succeeding_id] = self._bulk_entry(succeeding_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(
            return_value={failing_id, succeeding_id}
        )
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())

        def fake_enable(pid):
            return pid != failing_id

        with (
            patch.object(em, "_enable_entity_locked", side_effect=fake_enable),
            patch.object(em, "_index_point"),
        ):
            em._reconcile_dynamic_points()
        self.assertNotIn(failing_id, em.active_dynamic_points)
        self.assertIn(succeeding_id, em.active_dynamic_points)

    def test_removal_publishes_empty_retained_meta_to_real_topic(self):
        """The absent-point removal path must clear discovery via a retained,
        empty-payload publish to the real per-point META topic — not a
        different payload, non-retained publish, or a null topic."""
        from nibe_mqtt_publisher import BrowserTopic

        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 1002
        em.active_dynamic_points.add(point_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with (
            patch.object(em, "_disable_entity_locked"),
            patch.object(em, "_deindex_point") as mock_deindex,
        ):
            em._reconcile_dynamic_points()
        mock_deindex.assert_called_once_with(point_id)
        em._pub.invalidate_config_hash.assert_called_once_with(point_id)
        em.mqtt.publish.assert_any_call(
            BrowserTopic.META_TEMPLATE.format(id=point_id), "", retain=True
        )

    def test_stale_removal_publishes_empty_retained_meta_to_real_topic(self):
        """Same guarantee as the absent-point path, for the stale-persisted
        (case 3) removal branch."""
        from nibe_mqtt_publisher import BrowserTopic

        em = _make_em()
        em.initial_discovery_complete = True
        stale_id = 1003
        em.active_dynamic_points.add(stale_id)
        em.bulk_data[stale_id] = self._bulk_entry(stale_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value=set())
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with (
            patch.object(em, "_disable_entity_locked"),
            patch.object(em, "_deindex_point") as mock_deindex,
        ):
            em._reconcile_dynamic_points()
        mock_deindex.assert_called_once_with(stale_id)
        em._pub.invalidate_config_hash.assert_called_once_with(stale_id)
        em.mqtt.publish.assert_any_call(
            BrowserTopic.META_TEMPLATE.format(id=stale_id), "", retain=True
        )

    def test_persist_and_publish_called_when_only_removed_nonzero(self):
        """`if activated or removed:` must trigger persistence/publish when
        EITHER counter is nonzero — a mutation to `and` would require both,
        so a removal-only pass (activated stays 0) must still be caught here."""
        em = _make_em()
        em.initial_discovery_complete = True
        stale_id = 1003
        em.active_dynamic_points.add(stale_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value=set())
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with (
            patch.object(em, "_disable_entity_locked"),
            patch.object(em, "_deindex_point"),
            patch.object(em, "_persist_active_dynamic") as mock_persist_active,
            patch.object(em, "_persist_dynamic_map") as mock_persist_map,
            patch.object(em, "publish_enabled_state") as mock_publish_state,
        ):
            em._reconcile_dynamic_points()
        mock_persist_active.assert_called_once()
        mock_persist_map.assert_called_once()
        mock_publish_state.assert_called_once()

    def test_final_log_reports_real_activated_and_removed_counts(self):
        """The closing summary log must carry the real activated/removed
        counts computed this pass, not swapped/None values."""
        em = _make_em()
        em.initial_discovery_complete = True
        activate_id, stale_id = 1001, 1003
        em.bulk_data[activate_id] = self._bulk_entry(activate_id)
        em.active_dynamic_points.add(stale_id)
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={activate_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with (
            patch.object(em, "_enable_entity_locked", return_value=True),
            patch.object(em, "_index_point"),
            patch.object(em, "_disable_entity_locked"),
            patch.object(em, "_deindex_point"),
            patch("nibe_entity_manager.log_discovery") as mock_log,
        ):
            em._reconcile_dynamic_points()
        mock_log.info.assert_called_with(
            "Startup reconciliation: %d dynamic point(s) activated, %d removed",
            1,
            1,
        )


class TestReconcileDynamicPointsAlreadyEnabledCase(unittest.TestCase):
    """Reconcile case 1b: point expected, present, and already enabled —
    must republish online and refresh state without re-enabling."""

    def _bulk_entry(self, point_id):
        return {
            "raw_value": 1,
            "string_value": "",
            "is_ok": True,
            "metadata": {
                "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                "isWritable": False,
                "variableType": "integer",
                "variableSize": "u8",
                "divisor": 1,
            },
            "title": f"Point {point_id}",
            "description": "",
        }

    def test_already_enabled_publishes_online_and_updates_state(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 2001
        entity_info = {
            "point_id": point_id,
            "entity_type": "sensor",
            "availability_topic": f"nibe/avail/{point_id}",
            "state_topic": f"nibe/state/{point_id}",
            "command_topic": None,
            "point_data": {},
        }
        em.bulk_data[point_id] = self._bulk_entry(point_id)
        em.mqtt_enabled_points.add(point_id)
        em.active_dynamic_points.add(point_id)
        em.active_entities_by_id[point_id] = entity_info

        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())

        with (
            patch.object(em, "enable_entity") as mock_enable,
            patch.object(em, "_update_entity_state") as mock_update,
        ):
            em._reconcile_dynamic_points()

        mock_enable.assert_not_called()
        em.mqtt.publish.assert_any_call(f"nibe/avail/{point_id}", "online", retain=True)
        mock_update.assert_called_once_with(entity_info)
        # active_dynamic_points.add(point_id) must add the real point_id, not
        # None — the set already contains point_id before the call so a
        # mutation to add(None) would go undetected without checking the set
        # doesn't ALSO pick up a spurious None member.
        self.assertEqual(em.active_dynamic_points, {point_id})


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
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())

        # _reconcile_dynamic_points acquires _em_lock and calls _disable_entity_locked directly
        with (
            patch.object(em, "_disable_entity_locked") as mock_disable,
            patch.object(em, "_deindex_point"),
        ):
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
            "raw_value": 0,
            "string_value": "",
            "is_ok": True,
            "metadata": {},
            "title": f"Point {stale_id}",
            "description": "",
        }
        # expected_active is empty → stale_id becomes stale
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value=set())
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())

        # _reconcile_dynamic_points acquires _em_lock and calls _disable_entity_locked directly
        with (
            patch.object(em, "_disable_entity_locked") as mock_disable,
            patch.object(em, "_deindex_point"),
        ):
            em._reconcile_dynamic_points()

        mock_disable.assert_called_once_with(stale_id)
        self.assertNotIn(stale_id, em.active_dynamic_points)


class TestDynamicMapGzipBranch(unittest.TestCase):
    """on_dynamic_map_message: gzip-compressed payload is decompressed before deserialise."""

    def _make_em_with_dynamic_loading(self):
        with (
            patch("nibe_entity_manager.EntityManager.resubscribe_all"),
            patch("nibe_entity_manager.EntityManager._setup_history_loading"),
        ):
            from nibe_entity_manager import EntityManager

            em = EntityManager(
                api_client=MagicMock(),
                publisher=MagicMock(),
                notify_fn=MagicMock(),
                dismiss_fn=MagicMock(),
                mqtt_client=MagicMock(),
            )
        em.device_info = {}
        em.device_name = "Test"
        return em

    def test_gzip_payload_is_decompressed_and_deserialised(self):
        import base64
        import gzip as _gzip

        from nibe_entity_manager import _GZIP_SENTINEL

        em = self._make_em_with_dynamic_loading()
        em.initial_discovery_complete = False

        inner = json.dumps({"entries": []})
        compressed = base64.b64encode(_gzip.compress(inner.encode("utf-8"))).decode("ascii")
        payload = (_GZIP_SENTINEL + compressed).encode("utf-8")

        msg = MagicMock()
        msg.payload = payload

        with patch.object(em.dynamic_point_map, "deserialise") as mock_deser:
            em._on_dynamic_map_message(None, None, msg)

        mock_deser.assert_called_once()
        # The call arg must be valid JSON equivalent to inner
        called_json = mock_deser.call_args.args[0]
        self.assertEqual(json.loads(called_json), json.loads(inner))


class TestRecordOutcomeAllEmptyFalse(unittest.TestCase):
    """record_outcome: all_empty=False guard for select points (branch 372→374)."""

    def test_record_outcome_select_non_controlling_not_set_when_other_value_has_dynamic_points(
        self,
    ):
        """For a select (3+ values) where all_empty=False, is_controlling must
        remain None after the final value is processed."""
        from nibe_dynamic_map import DynamicPointEntry, DynamicPointMap

        dm = DynamicPointMap()
        dm._table[6000] = DynamicPointEntry(
            point_id=6000,
            title="Heat source",
            entity_type="select",
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


class TestReconcileAlreadyEnabledNoEntityInfo(unittest.TestCase):
    """_reconcile_dynamic_points: entity in mqtt_enabled_points but not in
    active_entities_by_id — guard must not crash."""

    def test_already_enabled_no_entity_info_still_adds_to_active_dynamic(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 2003
        em.bulk_data[point_id] = {
            "raw_value": 1,
            "is_ok": True,
            "string_value": "",
            "metadata": {},
            "title": "Test",
        }
        em.mqtt_enabled_points.add(point_id)
        em.active_dynamic_points.add(point_id)
        # Deliberately NOT adding to active_entities_by_id
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={point_id})
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value=set())
        with (
            patch.object(em, "enable_entity") as mock_enable,
            patch.object(em, "_update_entity_state") as mock_update,
        ):
            em._reconcile_dynamic_points()
        mock_enable.assert_not_called()
        mock_update.assert_not_called()
        self.assertIn(point_id, em.active_dynamic_points)


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
            "5110": {
                "title": "Heat mode",
                "description": "",
                "metadata": {
                    "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
                    "isWritable": True,
                    "minValue": 0,
                    "maxValue": 1,
                    "variableType": "integer",
                    "variableSize": "u8",
                    "divisor": 1,
                    "decimal": 0,
                    "unit": "",
                },
                "value": {"integerValue": 1, "stringValue": "", "isOk": True},
            }
        }
        with patch.object(em, "_get_cached_entity_type") as mock_type:
            em._fetch_bulk_data(detect_changes=False)
        mock_type.assert_not_called()


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
        em.active_dynamic_points = set()  # point 777 has never been active

        # Make dynamic_point_map.expected_active_dynamic_points return {777}
        em.dynamic_point_map.expected_active_dynamic_points = MagicMock(return_value={777})
        # bulk_data does NOT contain 777 (absent from bulk)
        em.bulk_data = {}

        with (
            patch.object(em, "_deindex_point") as mock_deindex,
            patch.object(em, "disable_entity") as mock_disable,
        ):
            em._reconcile_dynamic_points()

        mock_deindex.assert_not_called()
        mock_disable.assert_not_called()


class TestPersistActiveDynamicRetain(unittest.TestCase):
    """_persist_active_dynamic: the existing coverage in
    TestActiveDynamicCrashSafety uses a capture_publish mock whose
    signature accepts retain but never asserts on it — retain=True was
    completely unverified. Without it, the active-dynamic-points record
    doesn't survive a broker restart, the exact scenario this function's
    own docstring says it exists to handle."""

    def test_published_with_retain_true(self):
        em = _make_em()
        em.active_dynamic_points = {1, 2, 3}
        em._persist_active_dynamic()
        from nibe_mqtt_publisher import BrowserTopic

        calls = [
            c for c in em.mqtt.publish.call_args_list if c.args[0] == BrowserTopic.ACTIVE_DYNAMIC
        ]
        self.assertTrue(calls)
        for c in calls:
            retain = c.kwargs.get("retain", c.args[2] if len(c.args) > 2 else None)
            self.assertTrue(retain)


class TestPersistDynamicMap(unittest.TestCase):
    """_persist_dynamic_map: every existing caller in the test suite mocks
    this out entirely, so the real implementation's actual published
    payload was completely unverified — a broken/None payload here would
    silently corrupt the bridge's dynamic-point-map persistence, losing
    all learned dynamic points across a restart."""

    def test_publishes_real_non_none_payload_to_correct_topic(self):
        from nibe_mqtt_publisher import BrowserTopic

        em = _make_em()
        with patch.object(em.dynamic_point_map, "to_file"):
            em._persist_dynamic_map()
        calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == BrowserTopic.DYNAMIC_MAP]
        self.assertTrue(calls, "Expected a publish to BrowserTopic.DYNAMIC_MAP")
        payload = calls[0].args[1]
        self.assertIsNotNone(payload)
        retain = calls[0].kwargs.get("retain", calls[0].args[2] if len(calls[0].args) > 2 else None)
        self.assertTrue(retain)

    def test_payload_decompresses_to_real_serialised_map(self):
        """The payload must be the actual compressed serialisation of the
        dynamic_point_map, not a placeholder — decompressing it must
        round-trip to valid JSON matching what serialise() produced."""
        import json as json_module

        from nibe_dynamic_map import DynamicPointEntry
        from nibe_entity_manager import _decompress_payload
        from nibe_mqtt_publisher import BrowserTopic

        em = _make_em()
        em.dynamic_point_map._table[1001] = DynamicPointEntry(
            point_id=1001,
            title="Test Switch",
            entity_type="switch",
        )
        with patch.object(em.dynamic_point_map, "to_file"):
            em._persist_dynamic_map()
        calls = [c for c in em.mqtt.publish.call_args_list if c.args[0] == BrowserTopic.DYNAMIC_MAP]
        decompressed = json_module.loads(_decompress_payload(calls[0].args[1]))
        self.assertIn("1001", json_module.dumps(decompressed))


class TestDynamicMapFileFallbackSurvivesSimulatedRestart(unittest.TestCase):
    """_persist_dynamic_map() (writer, real file write via
    DynamicPointMap.to_file()) and discover_points()'s file-fallback
    branch (reader, real DynamicPointMap.from_file() call, used when
    nothing was restored from MQTT) had never been chained together with
    a real file on a fresh EntityManager instance — every existing test
    mocked DynamicPointMap.to_file() out entirely specifically because the
    production path (/data/dynamic_point_map.json) can't safely be
    exercised in a unit test. Now that to_file()/from_file() resolve their
    default path dynamically (see test_dynamic_map.py's
    test_default_path_resolves_dynamically_not_frozen_at_def_time), the
    module constant can be safely redirected to a temp file for the
    duration of a test, making this real round trip possible for the
    first time."""

    def test_entries_written_by_one_instance_are_loaded_by_a_fresh_ones_discover_points(self):
        import os
        import tempfile

        import nibe_dynamic_map as ndm
        from nibe_dynamic_map import DynamicPointEntry

        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            os.unlink(tmp)
            with patch.object(ndm, "_FILE_FALLBACK", tmp):
                writer = _make_em()
                writer.dynamic_point_map._table[2001] = DynamicPointEntry(
                    point_id=2001,
                    title="Silent Mode",
                    entity_type="switch",
                )
                writer._persist_dynamic_map()
                self.assertTrue(
                    os.path.exists(tmp),
                    "_persist_dynamic_map() did not actually write the real file",
                )

                reader = _make_em()
                self.assertEqual(len(reader.dynamic_point_map), 0)
                with (
                    patch.object(reader, "_fetch_bulk_data", return_value=True),
                    patch.object(reader._pub, "publish_all_metadata"),
                    patch.object(reader._pub, "publish_point_list"),
                    patch.object(reader, "_reconcile_dynamic_points"),
                ):
                    reader.discover_points()

                self.assertIn(2001, reader.dynamic_point_map)
                self.assertEqual(reader.dynamic_point_map[2001].title, "Silent Mode")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
            if os.path.exists(tmp + ".tmp"):
                os.unlink(tmp + ".tmp")
