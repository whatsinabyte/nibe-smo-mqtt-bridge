"""
test_dynamic_map.py
===================
Nibe_dynamic_map tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from hypothesis import assume, given
from hypothesis import strategies as st

from conftest import (
    _make_em,
    _nibe_point_id,
)

class TestDynamicPointEntrySerialiseProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointEntry serialisation roundtrip."""

    _entry_strategy = st.fixed_dictionaries({
        'point_id':   st.integers(min_value=1, max_value=99999),
        'title':      st.text(max_size=60),
        'entity_type': st.sampled_from(['switch', 'select']),
        'processed_values':   st.sets(st.integers(min_value=0, max_value=100)),
        'unprocessed_values': st.sets(st.integers(min_value=0, max_value=100)),
        'is_controlling': st.one_of(st.none(), st.booleans()),
        'firmware_removed': st.booleans(),
    })

    @given(_entry_strategy)
    def test_to_dict_never_raises(self, kwargs):
        from nibe_dynamic_map import DynamicPointEntry
        entry = DynamicPointEntry(**{k: v for k, v in kwargs.items()})
        entry.to_dict()

    @given(_entry_strategy)
    def test_from_dict_roundtrip(self, kwargs):
        """to_dict → from_dict recovers all fields exactly."""
        from nibe_dynamic_map import DynamicPointEntry
        entry = DynamicPointEntry(**{k: v for k, v in kwargs.items()})
        d = entry.to_dict()
        recovered = DynamicPointEntry.from_dict(d)
        self.assertEqual(recovered.point_id, entry.point_id)
        self.assertEqual(recovered.title, entry.title)
        self.assertEqual(recovered.entity_type, entry.entity_type)
        self.assertEqual(recovered.processed_values, entry.processed_values)
        self.assertEqual(recovered.unprocessed_values, entry.unprocessed_values)
        self.assertEqual(recovered.is_controlling, entry.is_controlling)
        self.assertEqual(recovered.firmware_removed, entry.firmware_removed)

    @given(_entry_strategy)
    def test_to_dict_produces_json_serialisable_output(self, kwargs):
        """to_dict output must be JSON-serialisable."""
        import json as _json
        from nibe_dynamic_map import DynamicPointEntry
        entry = DynamicPointEntry(**{k: v for k, v in kwargs.items()})
        _json.dumps(entry.to_dict())  # must not raise


# ---------------------------------------------------------------------------
# DynamicPointMap serialise/deserialise roundtrip
# ---------------------------------------------------------------------------


class TestDynamicPointMapSerialiseProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointMap serialise/deserialise."""

    @given(st.text())
    def test_deserialise_never_raises_on_arbitrary_input(self, json_str):
        """deserialise must never raise — it must handle any string gracefully."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        m.deserialise(json_str)  # must not raise

    @given(st.text())
    def test_deserialise_returns_int(self, json_str):
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        result = m.deserialise(json_str)
        self.assertIsInstance(result, int)

    @given(st.text())
    def test_deserialise_non_negative(self, json_str):
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        result = m.deserialise(json_str)
        self.assertGreaterEqual(result, 0)

    def test_serialise_then_deserialise_roundtrip(self):
        """serialise → deserialise into a fresh map recovers all entries."""
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m1 = DynamicPointMap()
        m1._table[100] = DynamicPointEntry(
            point_id=100, title='Test Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [22001, 22002]},
        )
        m1._table[200] = DynamicPointEntry(
            point_id=200, title='Test Select', entity_type='select',
            processed_values={0, 1, 2}, is_controlling=False,
        )
        json_str = m1.serialise()

        m2 = DynamicPointMap()
        count = m2.deserialise(json_str)
        self.assertEqual(count, 2)
        self.assertEqual(m2[100].title, 'Test Switch')
        self.assertEqual(m2[100].dynamic_points_by_value[1], [22001, 22002])
        self.assertEqual(m2[200].is_controlling, False)

    @given(st.lists(
        st.fixed_dictionaries({
            'point_id':    st.integers(min_value=1, max_value=9999),
            'title':       st.text(max_size=30),
            'entity_type': st.sampled_from(['switch', 'select']),
        }),
        min_size=0, max_size=10, unique_by=lambda e: e['point_id'],
    ))
    def test_serialise_roundtrip_for_arbitrary_maps(self, entries):
        """For any valid map, serialise → deserialise recovers same entry count."""
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m1 = DynamicPointMap()
        for e in entries:
            m1._table[e['point_id']] = DynamicPointEntry(**e)
        json_str = m1.serialise()
        m2 = DynamicPointMap()
        count = m2.deserialise(json_str)
        self.assertEqual(count, len(entries))


# ---------------------------------------------------------------------------
# fmt_ts extended properties
# ---------------------------------------------------------------------------


class TestMarkFirmwareRemovedProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointMap.mark_firmware_removed
    and restore_from_bulk."""

    def _map_with(self, point_ids):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        for pid in point_ids:
            m._table[pid] = DynamicPointEntry(
                point_id=pid, title=f'P{pid}', entity_type='switch',
            )
        return m

    @given(st.integers(min_value=1, max_value=9999))
    def test_mark_sets_firmware_removed_true(self, pid):
        """After marking, firmware_removed must be True."""
        m = self._map_with([pid])
        m.mark_firmware_removed(pid)
        self.assertTrue(m._table[pid].firmware_removed)

    @given(st.integers(min_value=1, max_value=9999))
    def test_mark_absent_point_never_raises(self, pid):
        """Marking a point not in the table must not raise."""
        m = self._map_with([])
        m.mark_firmware_removed(pid)  # must not raise

    @given(st.integers(min_value=1, max_value=9999))
    def test_mark_twice_is_idempotent(self, pid):
        """Marking twice must not raise and result remains True."""
        m = self._map_with([pid])
        m.mark_firmware_removed(pid)
        m.mark_firmware_removed(pid)
        self.assertTrue(m._table[pid].firmware_removed)

    @given(st.sets(st.integers(min_value=1, max_value=9999), min_size=1, max_size=10))
    def test_restore_clears_firmware_removed(self, pids):
        """restore_from_bulk must clear firmware_removed for points in the set."""
        m = self._map_with(pids)
        for pid in pids:
            m._table[pid].firmware_removed = True
        m.restore_from_bulk(pids)
        for pid in pids:
            self.assertFalse(m._table[pid].firmware_removed)

    @given(st.integers(min_value=1, max_value=4999),
           st.integers(min_value=5000, max_value=9999))
    def test_restore_only_affects_points_in_set(self, pid_in, pid_out):
        """restore_from_bulk must not clear firmware_removed for absent points."""
        m = self._map_with([pid_in, pid_out])
        m._table[pid_in].firmware_removed  = True
        m._table[pid_out].firmware_removed = True
        m.restore_from_bulk({pid_in})  # only pid_in in the bulk set
        self.assertFalse(m._table[pid_in].firmware_removed)
        self.assertTrue(m._table[pid_out].firmware_removed)

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=10))
    def test_restore_never_raises(self, pids):
        """restore_from_bulk must never raise for any set of point IDs."""
        m = self._map_with(pids)
        m.restore_from_bulk(pids)  # must not raise


# ---------------------------------------------------------------------------
# DynamicPointMap.flush properties (nibe_dynamic_map.py)
# ---------------------------------------------------------------------------


class TestDynamicPointMapActivePointsProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointMap.expected_active_dynamic_points."""

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=100),
        st.integers(min_value=0, max_value=5),
        max_size=10,
    ))
    def test_never_raises(self, current_values):
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        result = m.expected_active_dynamic_points(current_values)
        self.assertIsInstance(result, set)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=100),
        st.integers(min_value=0, max_value=5),
        max_size=10,
    ))
    def test_empty_map_always_returns_empty_set(self, current_values):
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        result = m.expected_active_dynamic_points(current_values)
        self.assertEqual(result, set())

    def test_controlling_entry_with_known_value_returns_dynamic_points(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[100] = DynamicPointEntry(
            point_id=100, title='Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [22001, 22002]},
        )
        result = m.expected_active_dynamic_points({100: 1})
        self.assertEqual(result, {22001, 22002})

    def test_non_controlling_entry_contributes_nothing(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[200] = DynamicPointEntry(
            point_id=200, title='Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=False,
            dynamic_points_by_value={},
        )
        result = m.expected_active_dynamic_points({200: 1})
        self.assertEqual(result, set())

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=100),
        st.integers(min_value=0, max_value=5),
        max_size=10,
    ))
    def test_result_is_always_a_set_of_ints(self, current_values):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[1] = DynamicPointEntry(
            point_id=1, title='T', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [22001]},
        )
        result = m.expected_active_dynamic_points(current_values)
        for item in result:
            self.assertIsInstance(item, int)


# ---------------------------------------------------------------------------
# DynamicPointMap lookup consistency properties
# ---------------------------------------------------------------------------



class TestDetectInputEntityProperties(unittest.TestCase):
    """Hypothesis properties for _detect_input_entity."""

    def _point(self, pid, var_type='integer', var_size='u8',
               unit='', min_val=0, max_val=1):
        return {
            'variableId': pid,
            'description': '',
            'metadata': {
                'variableType': var_type,
                'variableSize': var_size,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False,
                'minValue': min_val, 'maxValue': max_val,
                'unit': unit, 'divisor': 1,
            }
        }

    @given(_nibe_point_id)
    def test_always_returns_two_tuple(self, pid):
        from nibe_entity_detection import _detect_input_entity
        point = self._point(pid)
        result = _detect_input_entity(point, point['metadata'])
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    @given(_nibe_point_id)
    def test_category_is_always_diagnostic(self, pid):
        """_detect_input_entity always returns diagnostic category."""
        from nibe_entity_detection import _detect_input_entity
        point = self._point(pid)
        _, category = _detect_input_entity(point, point['metadata'])
        self.assertEqual(category, 'diagnostic')

    @given(_nibe_point_id)
    def test_time_var_type_always_returns_sensor(self, pid):
        from nibe_entity_detection import _detect_input_entity
        point = self._point(pid, var_type='time')
        entity_type, _ = _detect_input_entity(point, point['metadata'])
        self.assertEqual(entity_type, 'sensor')

    @given(_nibe_point_id)
    def test_date_var_type_always_returns_sensor(self, pid):
        from nibe_entity_detection import _detect_input_entity
        point = self._point(pid, var_type='date')
        entity_type, _ = _detect_input_entity(point, point['metadata'])
        self.assertEqual(entity_type, 'sensor')

    @given(_nibe_point_id)
    def test_never_raises(self, pid):
        from nibe_entity_detection import _detect_input_entity
        point = self._point(pid)
        _detect_input_entity(point, point['metadata'])  # must not raise

    @given(_nibe_point_id.filter(
        lambda p: (
            p not in __import__('nibe_entity_detection')._BINARY_SENSOR_EXCLUSIONS
            and p not in __import__('nibe_entity_detection').VALUE_MAPPINGS.get('input', {})
        )))
    def test_binary_shape_u8_0_1_no_unit_returns_binary_sensor(self, pid):
        """Classic binary sensor shape (no VALUE_MAPPINGS override) → binary_sensor."""
        from nibe_entity_detection import _detect_input_entity
        point = self._point(pid, var_size='u8', unit='', min_val=0, max_val=1)
        entity_type, _ = _detect_input_entity(point, point['metadata'])
        self.assertEqual(entity_type, 'binary_sensor')


# ---------------------------------------------------------------------------
# DynamicPointMap.populate_from_bulk properties (nibe_dynamic_map.py)
# ---------------------------------------------------------------------------



class TestPopulateFromBulkProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointMap.populate_from_bulk."""

    def _make_points(self, specs):
        """Build all_points_by_id dict from list of (pid, entity_type) pairs."""
        result = {}
        for pid, etype, meta in specs:
            result[pid] = {
                'variableId': pid,
                'display_title': f'Point {pid}',
                'metadata': {'minValue': meta[0], 'maxValue': meta[1]},
            }
        return result

    def _make_types(self, specs):
        return {pid: etype for pid, etype, _ in specs}

    _spec = st.tuples(
        st.integers(min_value=1, max_value=9999),
        st.sampled_from(['switch', 'select', 'sensor', 'number', 'binary_sensor']),
        st.tuples(st.integers(min_value=0, max_value=5),
                  st.integers(min_value=0, max_value=5)),
    )

    @given(st.lists(_spec, max_size=10, unique_by=lambda s: s[0]))
    def test_return_value_equals_new_entries_added(self, specs):
        """populate_from_bulk return value always equals len(new entries added)."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        points = self._make_points(specs)
        types  = self._make_types(specs)
        before = len(m._table)
        added  = m.populate_from_bulk(points, types)
        self.assertEqual(added, len(m._table) - before)

    @given(st.lists(_spec, max_size=10, unique_by=lambda s: s[0]))
    def test_only_switch_and_select_added(self, specs):
        """Only switch and select entities are added to the table."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        m.populate_from_bulk(self._make_points(specs), self._make_types(specs))
        for entry in m._table.values():
            self.assertIn(entry.entity_type, ('switch', 'select'))

    @given(st.lists(_spec, max_size=10, unique_by=lambda s: s[0]))
    def test_new_entries_have_none_is_controlling(self, specs):
        """All entries created by populate_from_bulk start with is_controlling=None."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        m.populate_from_bulk(self._make_points(specs), self._make_types(specs))
        for entry in m._table.values():
            self.assertIsNone(entry.is_controlling)

    @given(st.lists(_spec, max_size=10, unique_by=lambda s: s[0]))
    def test_new_entries_not_firmware_removed(self, specs):
        """All entries created by populate_from_bulk have firmware_removed=False."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        m.populate_from_bulk(self._make_points(specs), self._make_types(specs))
        for entry in m._table.values():
            self.assertFalse(entry.firmware_removed)

    @given(st.lists(_spec, max_size=10, unique_by=lambda s: s[0]))
    def test_idempotent_second_call_adds_nothing(self, specs):
        """Calling populate_from_bulk twice with the same data adds 0 the second time."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        points = self._make_points(specs)
        types  = self._make_types(specs)
        m.populate_from_bulk(points, types)
        added_second = m.populate_from_bulk(points, types)
        self.assertEqual(added_second, 0)

    @given(st.lists(_spec, max_size=10, unique_by=lambda s: s[0]))
    def test_existing_entries_not_overwritten(self, specs):
        """Entries already in the table must not be replaced."""
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        points = self._make_points(specs)
        types  = self._make_types(specs)
        # Pre-seed one entry as controlling=True
        switch_pids = [pid for pid, etype, _ in specs if etype == 'switch']
        if not switch_pids:
            return
        pid = switch_pids[0]
        sentinel = DynamicPointEntry(point_id=pid, title='SENTINEL',
                                     entity_type='switch', is_controlling=True)
        m._table[pid] = sentinel
        m.populate_from_bulk(points, types)
        # The sentinel must not have been replaced
        self.assertIs(m._table[pid], sentinel)
        self.assertTrue(m._table[pid].is_controlling)

    @given(st.lists(_spec, max_size=10, unique_by=lambda s: s[0]))
    def test_unprocessed_values_nonempty(self, specs):
        """Every new entry must have at least one unprocessed value."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        m.populate_from_bulk(self._make_points(specs), self._make_types(specs))
        for entry in m._table.values():
            self.assertGreater(len(entry.unprocessed_values), 0)


# ---------------------------------------------------------------------------
# _retry_delay properties (nibe_api.py)
# ---------------------------------------------------------------------------


class TestDynamicPointEntry(unittest.TestCase):
    """Unit tests for DynamicPointEntry dataclass."""

    def setUp(self):
        from nibe_dynamic_map import DynamicPointEntry
        self.cls = DynamicPointEntry

    def _switch_entry(self, **kwargs):
        defaults = dict(
            point_id=1001, title='Pool heating', entity_type='switch',
            processed_values={0, 1}, unprocessed_values=set(),
            is_controlling=True,
            dynamic_points_by_value={0: [], 1: [22001, 22002]},
            firmware_removed=False,
        )
        defaults.update(kwargs)
        return self.cls(**defaults)

    def test_is_fully_processed_true(self):
        e = self._switch_entry()
        self.assertTrue(e.is_fully_processed())

    def test_is_fully_processed_false_when_unprocessed_remain(self):
        e = self._switch_entry(processed_values={0}, unprocessed_values={1})
        self.assertFalse(e.is_fully_processed())

    def test_is_fully_processed_false_when_no_processed_values(self):
        e = self._switch_entry(processed_values=set(), unprocessed_values={0, 1})
        self.assertFalse(e.is_fully_processed())

    def test_dynamic_points_for_value_found(self):
        e = self._switch_entry()
        self.assertEqual(e.dynamic_points_for_value(1), [22001, 22002])

    def test_dynamic_points_for_value_empty(self):
        e = self._switch_entry()
        self.assertEqual(e.dynamic_points_for_value(0), [])

    def test_dynamic_points_for_value_unprocessed_returns_none(self):
        e = self._switch_entry(processed_values={0}, unprocessed_values={1},
                               dynamic_points_by_value={0: []})
        self.assertIsNone(e.dynamic_points_for_value(1))

    def test_all_known_dynamic_points(self):
        e = self._switch_entry()
        self.assertEqual(e.all_known_dynamic_points(), {22001, 22002})

    def test_all_known_dynamic_points_empty_when_non_controlling(self):
        e = self._switch_entry(is_controlling=False,
                               dynamic_points_by_value={0: [], 1: []})
        self.assertEqual(e.all_known_dynamic_points(), set())

    def test_roundtrip_serialisation(self):
        e = self._switch_entry()
        d = e.to_dict()
        e2 = self.cls.from_dict(d)
        self.assertEqual(e2.point_id, e.point_id)
        self.assertEqual(e2.title, e.title)
        self.assertEqual(e2.entity_type, e.entity_type)
        self.assertEqual(e2.processed_values, e.processed_values)
        self.assertEqual(e2.unprocessed_values, e.unprocessed_values)
        self.assertEqual(e2.is_controlling, e.is_controlling)
        self.assertEqual(e2.dynamic_points_by_value, e.dynamic_points_by_value)
        self.assertEqual(e2.firmware_removed, e.firmware_removed)

    def test_roundtrip_with_none_is_controlling(self):
        e = self.cls(point_id=9999, title='Unknown', entity_type='switch',
                     processed_values=set(), unprocessed_values={0, 1},
                     is_controlling=None)
        e2 = self.cls.from_dict(e.to_dict())
        self.assertIsNone(e2.is_controlling)

    def test_roundtrip_select_with_multiple_values(self):
        e = self.cls(
            point_id=47394, title='HW comfort mode', entity_type='select',
            processed_values={0, 1, 2}, unprocessed_values={3},
            is_controlling=True,
            dynamic_points_by_value={0: [], 1: [], 2: [33001, 33002]},
        )
        e2 = self.cls.from_dict(e.to_dict())
        self.assertEqual(e2.dynamic_points_by_value[2], [33001, 33002])
        self.assertEqual(e2.unprocessed_values, {3})

    def test_record_outcome_non_controlling_confirmed_after_all_values_empty(self):
        """record_outcome: when all processed values produce no dynamic points
        and unprocessed is empty, is_controlling is set to False (lines 367-372)."""
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        dm = DynamicPointMap()
        dm._table[5000] = DynamicPointEntry(
            point_id=5000, title='Economy mode', entity_type='switch',
            processed_values={0}, unprocessed_values={1},
            is_controlling=None,
            dynamic_points_by_value={0: []},
        )
        # Record value=1 with no dynamic points — now both values processed, both empty
        dm.record_outcome(5000, 1, [])
        self.assertFalse(dm._table[5000].is_controlling)
        self.assertEqual(dm._table[5000].unprocessed_values, set())



class TestDynamicPointMap(unittest.TestCase):
    """Unit tests for DynamicPointMap table operations."""

    def setUp(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        self.Map   = DynamicPointMap
        self.Entry = DynamicPointEntry

    def _map_with_entries(self):
        """Return a map pre-populated with two entries."""
        m = self.Map()
        m._table[1001] = self.Entry(
            point_id=1001, title='Pool heating', entity_type='switch',
            processed_values={0, 1}, unprocessed_values=set(),
            is_controlling=True,
            dynamic_points_by_value={0: [], 1: [22001, 22002]},
        )
        m._table[2001] = self.Entry(
            point_id=2001, title='Silent mode', entity_type='switch',
            processed_values={0, 1}, unprocessed_values=set(),
            is_controlling=False,
            dynamic_points_by_value={0: [], 1: []},
        )
        return m

    # ── is_known_dynamic ───────────────────────────────────────────────

    def test_is_known_dynamic_true(self):
        m = self._map_with_entries()
        self.assertTrue(m.is_known_dynamic(22001))
        self.assertTrue(m.is_known_dynamic(22002))

    def test_is_known_dynamic_false_for_controlling_point(self):
        m = self._map_with_entries()
        # The controlling point itself is not a dynamic point
        self.assertFalse(m.is_known_dynamic(1001))

    def test_is_known_dynamic_false_for_unknown(self):
        m = self._map_with_entries()
        self.assertFalse(m.is_known_dynamic(99999))

    def test_all_known_dynamic_point_ids(self):
        m = self._map_with_entries()
        self.assertEqual(m.all_known_dynamic_point_ids(), {22001, 22002})

    # ── controlling_entry_for_dynamic ─────────────────────────────────

    def test_controlling_entry_found(self):
        m = self._map_with_entries()
        entry = m.controlling_entry_for_dynamic(22001)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.point_id, 1001)

    def test_controlling_entry_none_for_unknown(self):
        m = self._map_with_entries()
        self.assertIsNone(m.controlling_entry_for_dynamic(99999))

    # ── expected_active_dynamic_points ────────────────────────────────

    def test_expected_active_when_controlling_switch_on(self):
        m = self._map_with_entries()
        active = m.expected_active_dynamic_points({1001: 1, 2001: 0})
        self.assertEqual(active, {22001, 22002})

    def test_expected_active_when_controlling_switch_off(self):
        m = self._map_with_entries()
        active = m.expected_active_dynamic_points({1001: 0, 2001: 0})
        self.assertEqual(active, set())

    def test_expected_active_ignores_non_controlling(self):
        m = self._map_with_entries()
        # Even if silent mode is on, it contributes no dynamic points
        active = m.expected_active_dynamic_points({1001: 0, 2001: 1})
        self.assertEqual(active, set())

    def test_expected_active_controlling_not_in_bulk(self):
        m = self._map_with_entries()
        # Pool heating point absent from bulk → not evaluated
        active = m.expected_active_dynamic_points({2001: 0})
        self.assertEqual(active, set())

    def test_expected_active_firmware_removed_ignored(self):
        m = self._map_with_entries()
        m._table[1001].firmware_removed = True
        active = m.expected_active_dynamic_points({1001: 1})
        self.assertEqual(active, set())

    # ── populate_from_bulk ────────────────────────────────────────────

    def _make_bulk(self, switches=None, selects=None):
        """Build a minimal all_points_by_id dict."""
        points = {}
        for pid in (switches or []):
            points[pid] = {
                'display_title': f'Switch {pid}',
                'metadata': {'minValue': 0, 'maxValue': 1, 'isWritable': True},
            }
        for pid, (mn, mx) in (selects or {}).items():
            points[pid] = {
                'display_title': f'Select {pid}',
                'metadata': {'minValue': mn, 'maxValue': mx, 'isWritable': True},
            }
        return points

    def test_populate_adds_switches(self):
        m = self.Map()
        bulk = self._make_bulk(switches=[100, 200])
        types = {100: 'switch', 200: 'switch'}
        added = m.populate_from_bulk(bulk, types)
        self.assertEqual(added, 2)
        self.assertIn(100, m)
        self.assertIn(200, m)
        self.assertEqual(m[100].unprocessed_values, {0, 1})

    def test_populate_adds_selects_with_correct_range(self):
        m = self.Map()
        bulk = self._make_bulk(selects={300: (0, 3)})
        types = {300: 'select'}
        m.populate_from_bulk(bulk, types)
        self.assertEqual(m[300].unprocessed_values, {0, 1, 2, 3})

    def test_populate_skips_non_switch_select(self):
        m = self.Map()
        bulk = {500: {'display_title': 'Sensor', 'metadata': {'minValue': 0, 'maxValue': 100}}}
        types = {500: 'sensor'}
        added = m.populate_from_bulk(bulk, types)
        self.assertEqual(added, 0)
        self.assertNotIn(500, m)

    def test_populate_skips_existing_entries(self):
        m = self._map_with_entries()
        bulk = self._make_bulk(switches=[1001])  # already in table
        types = {1001: 'switch'}
        added = m.populate_from_bulk(bulk, types)
        self.assertEqual(added, 0)
        # Existing entry untouched
        self.assertTrue(m[1001].is_controlling)

    def test_populate_all_new_marked_unprocessed(self):
        m = self.Map()
        bulk = self._make_bulk(switches=[1, 2, 3])
        types = {1: 'switch', 2: 'switch', 3: 'switch'}
        m.populate_from_bulk(bulk, types)
        for pid in [1, 2, 3]:
            self.assertIsNone(m[pid].is_controlling)
            self.assertEqual(m[pid].processed_values, set())

    # ── firmware_removed / restore ────────────────────────────────────

    def test_mark_firmware_removed(self):
        m = self._map_with_entries()
        m.mark_firmware_removed(1001)
        self.assertTrue(m[1001].firmware_removed)

    def test_mark_firmware_removed_nonexistent_point_is_noop(self):
        """309->exit: entry is None → early return without raising."""
        m = self._map_with_entries()
        m.mark_firmware_removed(9999)  # not in table — must not raise

    def test_restore_from_bulk_clears_removed_flag(self):
        m = self._map_with_entries()
        m._table[1001].firmware_removed = True
        m.restore_from_bulk({1001, 2001})
        self.assertFalse(m[1001].firmware_removed)

    # ── record_outcome ────────────────────────────────────────────────

    def test_record_outcome_controlling(self):
        m = self.Map()
        m._table[1001] = self.Entry(
            point_id=1001, title='Pool', entity_type='switch',
            unprocessed_values={0, 1},
        )
        m.record_outcome(1001, 1, [22001, 22002])
        entry = m[1001]
        self.assertIn(1, entry.processed_values)
        self.assertNotIn(1, entry.unprocessed_values)
        self.assertEqual(entry.dynamic_points_by_value[1], [22001, 22002])
        self.assertTrue(entry.is_controlling)

    def test_record_outcome_non_controlling_fully_processed(self):
        m = self.Map()
        m._table[2001] = self.Entry(
            point_id=2001, title='Silent', entity_type='switch',
            unprocessed_values={0, 1},
        )
        m.record_outcome(2001, 0, [])
        m.record_outcome(2001, 1, [])
        entry = m[2001]
        self.assertFalse(entry.is_controlling)
        self.assertEqual(entry.unprocessed_values, set())

    def test_record_outcome_all_processed_but_not_all_empty_leaves_is_controlling_none(self):
        """371->373: all values processed but one had dynamic points →
        all_empty is False → is_controlling stays None, not forced False."""
        m = self.Map()
        m._table[3001] = self.Entry(
            point_id=3001, title='Mixed', entity_type='select',
            unprocessed_values={0, 1, 2},
        )
        # value=0 → has dynamic points (controlling for this value)
        m.record_outcome(3001, 0, [22001])
        # value=1 and value=2 → no dynamic points
        m.record_outcome(3001, 1, [])
        m.record_outcome(3001, 2, [])
        entry = m[3001]
        # Not all empty → is_controlling stays True (set when value=0 was recorded)
        self.assertTrue(entry.is_controlling)

    def test_record_outcome_switch_infers_inverse_when_controlling(self):
        """For a 2-value switch, recording value=1 as controlling should
        automatically mark value=0 as processed with no dynamic points."""
        m = self.Map()
        m._table[1001] = self.Entry(
            point_id=1001, title='Pool', entity_type='switch',
            unprocessed_values={0, 1},
        )
        m.record_outcome(1001, 1, [22001])
        entry = m[1001]
        # Both values should be processed
        self.assertEqual(entry.unprocessed_values, set())
        self.assertIn(0, entry.processed_values)
        self.assertIn(1, entry.processed_values)
        # Inverse value maps to no dynamic points
        self.assertEqual(entry.dynamic_points_by_value[0], [])
        # is_controlling is True because value=1 produced dynamic points
        self.assertTrue(entry.is_controlling)

    def test_record_outcome_switch_infers_inverse_when_non_controlling(self):
        """For a 2-value switch, recording value=0 as non-controlling should
        automatically mark value=1 as processed with no dynamic points."""
        m = self.Map()
        m._table[2001] = self.Entry(
            point_id=2001, title='Silent', entity_type='switch',
            unprocessed_values={0, 1},
        )
        m.record_outcome(2001, 0, [])
        entry = m[2001]
        self.assertEqual(entry.unprocessed_values, set())
        self.assertFalse(entry.is_controlling)

    def test_record_outcome_select_does_not_infer_inverse(self):
        """For a select with >2 values, no inverse inference should occur."""
        m = self.Map()
        m._table[3001] = self.Entry(
            point_id=3001, title='Mode', entity_type='select',
            unprocessed_values={0, 1, 2},
        )
        m.record_outcome(3001, 1, [33001])
        entry = m[3001]
        # Only value=1 should be processed
        self.assertIn(2, entry.unprocessed_values)
        self.assertIn(0, entry.unprocessed_values)

    def test_record_outcome_partial_still_none_is_controlling(self):
        m = self.Map()
        m._table[3001] = self.Entry(
            point_id=3001, title='Mode', entity_type='select',
            unprocessed_values={0, 1, 2},
        )
        m.record_outcome(3001, 0, [])
        # Only one value processed, none controlling yet
        self.assertIsNone(m[3001].is_controlling)

    def test_record_outcome_unknown_point_logs_warning(self):
        m = self.Map()
        # Should not raise
        m.record_outcome(99999, 1, [12345])

    # ── serialisation ─────────────────────────────────────────────────

    def test_serialise_deserialise_roundtrip(self):
        m = self._map_with_entries()
        payload = m.serialise()
        m2 = self.Map()
        count = m2.deserialise(payload)
        self.assertEqual(count, 2)
        self.assertIn(1001, m2)
        self.assertIn(2001, m2)
        self.assertEqual(m2[1001].dynamic_points_by_value[1], [22001, 22002])
        self.assertFalse(m2[2001].is_controlling)

    def test_deserialise_empty_string_returns_zero(self):
        m = self.Map()
        count = m.deserialise('')
        self.assertEqual(count, 0)

    def test_deserialise_corrupt_json_returns_zero(self):
        m = self.Map()
        count = m.deserialise('not-json{{{')
        self.assertEqual(count, 0)

    def test_deserialise_wrong_type_returns_zero(self):
        m = self.Map()
        count = m.deserialise('[]')  # list instead of dict
        self.assertEqual(count, 0)

    def test_deserialise_skips_malformed_entries(self):
        m = self.Map()
        payload = json.dumps({
            '1001': {'point_id': 1001, 'title': 'Good', 'entity_type': 'switch',
                     'processed_values': [], 'unprocessed_values': [0, 1],
                     'is_controlling': None, 'dynamic_points_by_value': {},
                     'firmware_removed': False},
            'bad':  'not-a-dict',
        })
        count = m.deserialise(payload)
        self.assertEqual(count, 1)
        self.assertIn(1001, m)

    def test_to_file_and_from_file_roundtrip(self):
        import tempfile
        import os
        m = self._map_with_entries()
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            ok = m.to_file(path)
            self.assertTrue(ok)
            m2 = self.Map()
            count = m2.from_file(path)
            self.assertEqual(count, 2)
            self.assertEqual(m2[1001].title, 'Pool heating')
        finally:
            os.unlink(path)

    def test_from_file_missing_file_returns_zero(self):
        m = self.Map()
        count = m.from_file('/tmp/does_not_exist_nibe_test_xyz.json')
        self.assertEqual(count, 0)

    def test_to_file_oserror_returns_false(self):
        """OSError on write (e.g. read-only filesystem) returns False without raising."""
        m = self._map_with_entries()
        with patch('builtins.open', side_effect=OSError("read-only")):
            result = m.to_file('/tmp/nibe_test_readonly.json')
        self.assertFalse(result)

    # ── flush ─────────────────────────────────────────────────────────

    def test_flush_resets_all_entries(self):
        m = self._map_with_entries()
        bulk = {
            1001: {'display_title': 'Pool heating',
                   'metadata': {'minValue': 0, 'maxValue': 1, 'isWritable': True}},
            2001: {'display_title': 'Silent mode',
                   'metadata': {'minValue': 0, 'maxValue': 1, 'isWritable': True}},
        }
        types = {1001: 'switch', 2001: 'switch'}
        m.flush(bulk, types)
        for pid in [1001, 2001]:
            self.assertIsNone(m[pid].is_controlling)
            self.assertEqual(m[pid].processed_values, set())
            self.assertEqual(m[pid].dynamic_points_by_value, {})
            self.assertEqual(m[pid].unprocessed_values, {0, 1})

# ===========================================================================
# 27. Write handler — simplified two-case design
# ===========================================================================


class TestWriteCases(unittest.TestCase):
    """Tests for the simplified write cases in _handle_command_worker."""

    def setUp(self):
        self.em = _make_em()
        self.em._api = MagicMock()
        self.em._api.write_point.return_value = True

    def _make_switch_entity(self, point_id):
        return {
            'point_id':           point_id,
            'entity_type':        'switch',
            'entity_id':          f'nibe_{point_id}',
            'state_topic':        f'homeassistant/switch/nibe_{point_id}/state',
            'availability_topic': f'homeassistant/switch/nibe_{point_id}/availability',
            'command_topic':      f'homeassistant/switch/nibe_{point_id}/set',
            'is_writable':        True,
            'display_title':      f'Switch {point_id}',
            'metadata':           {'minValue': 0, 'maxValue': 1, 'divisor': 1,
                                   'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
        }

    def _seed_map_entry(self, pid, is_controlling, dynamic_pids=None, fully_processed=True):
        from nibe_dynamic_map import DynamicPointEntry
        dyn = dynamic_pids or []
        self.em.dynamic_point_map._table[pid] = DynamicPointEntry(
            point_id=pid,
            title=f'Switch {pid}',
            entity_type='switch',
            processed_values={0, 1} if fully_processed else {0},
            unprocessed_values=set() if fully_processed else {1},
            is_controlling=is_controlling,
            dynamic_points_by_value={0: [], 1: dyn} if fully_processed else {0: []},
        )

    def test_caseA1_non_controlling_no_scan_window(self):
        """Case A1: fully processed non-controlling → no post-write scan."""
        pid = 2001
        self._seed_map_entry(pid, is_controlling=False)
        self.em._handle_command_worker(self._make_switch_entity(pid), 1, '1', 'test01')
        self.assertFalse(self.em._post_write_active,
                         "Non-controlling switch must not activate scan window")

    def test_caseA2_controlling_opens_scan_window(self):
        """Case A2: fully processed controlling → scan window opened.
        Fast-path probing was removed (firmware takes >12.5s to activate
        dynamic points via REST write — probes always missed on S2125).
        Post-write scan is the correct detection mechanism."""
        pid = 1001
        self._seed_map_entry(pid, is_controlling=True, dynamic_pids=[22001])
        self.em._handle_command_worker(self._make_switch_entity(pid), 1, '1', 'test02')
        self.assertTrue(self.em._post_write_active,
                        "Case A2 must open scan window")
        self.assertEqual(self.em._post_write_controlling_point, pid)

    def test_caseA2_controlling_off_value_opens_scan_window(self):
        """Case A2: writing the off-value of a controlling switch also opens scan window."""
        pid = 1001
        self._seed_map_entry(pid, is_controlling=True, dynamic_pids=[22001])
        self.em._handle_command_worker(self._make_switch_entity(pid), 0, '0', 'test03')
        self.assertTrue(self.em._post_write_active)
        self.assertEqual(self.em._post_write_controlling_point, pid)

    def test_caseA3_unprocessed_always_runs_detection(self):
        """Case A3: unprocessed value → detection window always runs.
        Learning is permanently on — A3b (scan only, no recording) no
        longer exists."""
        pid = 3001
        self._seed_map_entry(pid, is_controlling=None, fully_processed=False)
        detection_called = []
        self.em._run_learning_detection = lambda pid, val, _cid: detection_called.append((pid, val))
        self.em._handle_command_worker(self._make_switch_entity(pid), 1, '1', 'test04')
        self.assertEqual(detection_called, [(pid, 1)])
        self.assertTrue(self.em._post_write_active,
                        "Learning detection must also activate scan window")

    def test_caseB_not_in_map_activates_scan(self):
        """Case B: point not in map → scan window activated as fallback."""
        self.em._handle_command_worker(self._make_switch_entity(9999), 1, '1', 'test06')
        self.assertTrue(self.em._post_write_active)

    def test_write_failure_does_not_activate_scan(self):
        """On write failure no scan window is activated."""
        pid = 2001
        self._seed_map_entry(pid, is_controlling=False)
        self.em._api.write_point.return_value = False
        self.em._handle_command_worker(self._make_switch_entity(pid), 1, '1', 'test07')
        self.assertFalse(self.em._post_write_active)


# ===========================================================================
# 29. _post_write_controlling_point set correctly in all write cases
# ===========================================================================


class TestPostWriteControllingPoint(unittest.TestCase):
    """_post_write_controlling_point must be set whenever the scan activates."""

    def setUp(self):
        self.em = _make_em()
        self.em._api = MagicMock()
        self.em._api.write_point.return_value = True

    def _entity(self, pid):
        return {
            'point_id': pid, 'entity_type': 'switch',
            'entity_id': f'nibe_{pid}',
            'state_topic': f'homeassistant/switch/nibe_{pid}/state',
            'availability_topic': f'homeassistant/switch/nibe_{pid}/avail',
            'command_topic': f'homeassistant/switch/nibe_{pid}/set',
            'is_writable': True, 'display_title': f'Sw {pid}',
            'metadata': {'minValue': 0, 'maxValue': 1, 'divisor': 1,
                         'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
        }

    def _seed(self, pid, is_controlling, fully_processed=True):
        from nibe_dynamic_map import DynamicPointEntry
        self.em.dynamic_point_map._table[pid] = DynamicPointEntry(
            point_id=pid, title=f'Sw {pid}', entity_type='switch',
            processed_values={0, 1} if fully_processed else {0},
            unprocessed_values=set() if fully_processed else {1},
            is_controlling=is_controlling,
            dynamic_points_by_value={0: [], 1: [22001]} if fully_processed else {0: []},
        )

    def test_caseA2_sets_controlling_point(self):
        """Case A2 must set _post_write_controlling_point so that any
        subsequent bulk-fetch detection of the dynamic change carries
        correct triggered_by attribution in the changelog entry."""
        pid = 1001
        self._seed(pid, is_controlling=True)
        self.em._post_write_controlling_point = None
        self.em._handle_command_worker(self._entity(pid), 1, '1', 'tid')
        self.assertEqual(self.em._post_write_controlling_point, pid)

    def test_caseA3_sets_controlling_point(self):
        """Unprocessed value always runs detection and sets controlling point."""
        pid = 3001
        self._seed(pid, is_controlling=None, fully_processed=False)
        with patch.object(self.em, '_run_learning_detection'):
            self.em._handle_command_worker(self._entity(pid), 1, '1', 'tid')
        self.assertEqual(self.em._post_write_controlling_point, pid)

    def test_caseB_sets_controlling_point(self):
        self.em._handle_command_worker(self._entity(9999), 1, '1', 'tid')
        self.assertEqual(self.em._post_write_controlling_point, 9999)

    def test_caseA1_does_not_set_controlling_point(self):
        pid = 2001
        self._seed(pid, is_controlling=False)
        self.em._post_write_controlling_point = None
        self.em._handle_command_worker(self._entity(pid), 1, '1', 'tid')
        self.assertIsNone(self.em._post_write_controlling_point)

    def test_write_failure_does_not_set_controlling_point(self):
        self.em._api.write_point.return_value = False
        self.em._post_write_controlling_point = None
        self.em._handle_command_worker(self._entity(9999), 1, '1', 'tid')
        self.assertIsNone(self.em._post_write_controlling_point)

    def test_new_dynamic_map_entry_created_on_first_controlling_write(self):
        """1687->1704 True branch: when _publish_dynamic_changes is called with a
        controlling point that has no DynamicPointMap entry, one is created."""
        em = _make_em()
        controlling = 5500
        dynamic_pid = 22999
        em.all_points_by_id[controlling] = {
            'variableId': controlling, 'display_title': 'New Switch',
            'entity_type': 'switch',
            'metadata': {'minValue': 0, 'maxValue': 1},
        }
        em._post_write_controlling_point = controlling
        em.bulk_data = {controlling: {'raw_value': 1, 'is_ok': True}}
        em.initial_discovery_complete = True
        with patch.object(em, 'publish_enabled_state'), \
             patch.object(em, '_persist_active_dynamic'), \
             patch.object(em, '_persist_dynamic_map'):
            em._publish_dynamic_changes([(dynamic_pid, {})], set())
        self.assertIn(controlling, em.dynamic_point_map._table)

    def test_existing_dynamic_map_entry_used_without_recreation(self):
        """1687->1704 False branch: when the entry already exists,
        record_outcome is called on it without creating a new entry."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        controlling = 5501
        dynamic_pid = 23000
        em._post_write_controlling_point = controlling
        em.bulk_data = {controlling: {'raw_value': 1, 'is_ok': True}}
        em.initial_discovery_complete = True
        em.dynamic_point_map._table[controlling] = DynamicPointEntry(
            point_id=controlling, title='Existing', entity_type='switch',
            unprocessed_values={0, 1},
        )
        with patch.object(em, 'publish_enabled_state'), \
             patch.object(em, '_persist_active_dynamic'), \
             patch.object(em, '_persist_dynamic_map'):
            em._publish_dynamic_changes([(dynamic_pid, {})], set())
        self.assertIn(controlling, em.dynamic_point_map._table)

    def test_write_with_no_state_topic_skips_optimistic_publish(self):
        """2109->2128: when entity_info has no state_topic, the optimistic
        state publish must be skipped without raising."""
        pid = 5501
        self._seed(pid, is_controlling=False)
        entity = self._entity(pid)
        del entity['state_topic']
        with patch.object(self.em, '_run_learning_detection'):
            self.em._handle_command_worker(entity, 1, '1', 'tid')
        # No state topic publish should have occurred
        state_calls = [c for c in self.em.mqtt.publish.call_args_list
                       if 'state' in str(c)]
        self.assertEqual(len(state_calls), 0)



    def _add_entity(self, em, pid, entity_type='sensor'):
        ei = {
            'point_id': pid, 'entity_type': entity_type,
            'entity_id': f'nibe_{pid}',
            'state_topic': f'homeassistant/sensor/nibe_{pid}/state',
            'availability_topic': f'homeassistant/sensor/nibe_{pid}/avail',
            'command_topic': None, 'is_writable': False,
            'display_title': f'Sensor {pid}',
            'metadata': {'minValue': 0, 'maxValue': 100, 'divisor': 1,
                         'isWritable': False,
                         'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'variableType': 'integer', 'intDefaultValue': 0,
                         'unit': '', 'shortUnit': ''},
        }
        em.active_entities_by_id[pid] = ei
        return ei

    def test_publishes_online_for_all_active_entities(self):
        em = _make_em()
        self._add_entity(em, 1)
        self._add_entity(em, 2)
        em.republish_availability()
        calls = [str(c) for c in em.mqtt.publish.call_args_list]
        online_calls = [c for c in calls if 'avail' in c and 'online' in c]
        self.assertGreaterEqual(len(online_calls), 2)

    def test_no_op_when_no_active_entities(self):
        em = _make_em()
        em.republish_availability()  # must not raise


# ===========================================================================
# 32. Value cache deduplication in _update_entity_state
# ===========================================================================


class TestDynamicMapRecordOutcomeNewEntry(unittest.TestCase):

    def test_record_outcome_sets_controlling(self):
        from nibe_dynamic_map import DynamicPointEntry, DynamicPointMap
        m = DynamicPointMap()
        m._table[6984] = DynamicPointEntry(
            point_id=6984, title='DOT manual', entity_type='switch',
            unprocessed_values={0, 1},
        )
        m.record_outcome(6984, 1, [6983])
        entry = m.get(6984)
        self.assertTrue(entry.is_controlling)
        self.assertIn(6983, entry.dynamic_points_by_value.get(1, []))

    def test_record_outcome_infers_inverse_value(self):
        from nibe_dynamic_map import DynamicPointEntry, DynamicPointMap
        m = DynamicPointMap()
        m._table[6984] = DynamicPointEntry(
            point_id=6984, title='DOT manual', entity_type='switch',
            unprocessed_values={0, 1},
        )
        m.record_outcome(6984, 1, [6983])
        entry = m.get(6984)
        self.assertIn(0, entry.dynamic_points_by_value)
        self.assertEqual(entry.dynamic_points_by_value[0], [])

    def test_record_outcome_non_controlling(self):
        from nibe_dynamic_map import DynamicPointEntry, DynamicPointMap
        m = DynamicPointMap()
        m._table[6984] = DynamicPointEntry(
            point_id=6984, title='DOT manual', entity_type='switch',
            unprocessed_values={0, 1},
        )
        m.record_outcome(6984, 1, [])
        entry = m.get(6984)
        self.assertFalse(entry.is_controlling)


# ===========================================================================
# 35. HAEntityRegistryWatcher — cache and event handling
# ===========================================================================


class TestHandleCommandWorkerDynamicCases(unittest.TestCase):
    """The four-case post-write dynamic point handling (A1/A2/A3a/A3b-B)
    documented in the method's own inline comments. A2 no longer uses
    fast-path single-point probing (removed after hardware testing showed
    the firmware takes >12.5s to activate dynamic points via REST write,
    causing all probes to miss — post-write scan is the correct path for
    all cases). A2 now simply opens the scan window, same as A3b/B."""

    def _entry(self, point_id, is_controlling=True, processed=None,
               unprocessed=None, firmware_removed=False):
        from nibe_dynamic_map import DynamicPointEntry
        return DynamicPointEntry(
            point_id=point_id,
            title=f'Point {point_id}',
            entity_type='switch',
            processed_values=processed if processed is not None else {1},
            unprocessed_values=unprocessed if unprocessed is not None else set(),
            is_controlling=is_controlling,
            firmware_removed=firmware_removed,
        )

    def _entity_info(self, point_id=5102, entity_type='switch'):
        return {
            'point_id': point_id, 'entity_type': entity_type,
            'display_title': f'Point {point_id}', 'state_topic': f'nibe/state/{point_id}',
        }

    def test_case_a1_non_controlling_no_scan(self):
        """Fully processed, NOT controlling -> no scan window opened."""
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[5102] = self._entry(5102, is_controlling=False)
        with patch.object(em, '_run_learning_detection') as mock_learn:
            em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        mock_learn.assert_not_called()
        self.assertFalse(em._post_write_active)

    def test_case_a2_fully_processed_controlling_opens_scan_window(self):
        """Fully processed AND controlling -> scan window opened.
        Fast-path probing was removed; the post-write scan detects
        dynamic point changes when the bulk fetch catches up."""
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[5102] = self._entry(5102, is_controlling=True)
        with patch.object(em, '_run_learning_detection') as mock_learn:
            em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        mock_learn.assert_not_called()
        self.assertTrue(em._post_write_active)
        self.assertEqual(em._post_write_controlling_point, 5102)

    def test_case_a2_excluded_when_firmware_removed(self):
        """A controlling, fully-processed entry that's ALSO firmware_removed
        must NOT open an A2 scan window — falls through to A3."""
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[5102] = self._entry(
            5102, is_controlling=True, firmware_removed=True,
        )
        with patch.object(em, '_run_learning_detection') as mock_learn:
            em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        mock_learn.assert_not_called()
        self.assertTrue(em._post_write_active)  # fell through to A3

    def test_case_a3_unprocessed_value_always_runs_detection(self):
        """Learning is always active — an unprocessed value always starts
        the detection window and records the outcome. A3b (learning OFF,
        no recording) no longer exists; the toggle was removed."""
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[5102] = self._entry(
            5102, is_controlling=True, processed={1}, unprocessed={2},
        )
        with patch.object(em, '_run_learning_detection') as mock_learn:
            em._handle_command_worker(self._entity_info(), 2, '2', 'cmd1')
        mock_learn.assert_called_once_with(5102, 2, 'cmd1')
        self.assertTrue(em._post_write_active)

    def test_case_b_point_not_in_dynamic_map_falls_back_to_scan(self):
        """Point genuinely absent from dynamic_point_map -> fallback scan
        window, no crash on a None entry."""
        em = _make_em()
        em._api.write_point.return_value = True
        with patch.object(em, '_run_learning_detection') as mock_learn:
            em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        mock_learn.assert_not_called()
        self.assertTrue(em._post_write_active)
        self.assertEqual(em._post_write_controlling_point, 5102)

    def test_non_switch_select_entity_type_skips_dynamic_logic_entirely(self):
        """A 'number' or 'text' write must never touch dynamic_point_map
        logic at all — only switch/select can be controlling points."""
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[100] = self._entry(100, is_controlling=True)
        with patch.object(em, '_run_learning_detection') as mock_learn:
            em._handle_command_worker(
                self._entity_info(point_id=100, entity_type='number'), 50, '50', 'cmd1',
            )
        mock_learn.assert_not_called()
        self.assertFalse(em._post_write_active)

    def test_write_failure_does_not_touch_dynamic_logic(self):
        """A failed write must skip all dynamic-point branching entirely —
        confirms the success/failure split happens before any of the
        A1-B case logic runs."""
        em = _make_em()
        em._api.write_point.return_value = False
        em.dynamic_point_map._table[5102] = self._entry(5102, is_controlling=True)
        with patch.object(em, '_run_learning_detection') as mock_learn:
            em._handle_command_worker(self._entity_info(), 1, '1', 'cmd1')
        mock_learn.assert_not_called()
        self.assertFalse(em._post_write_active)

    def test_successful_switch_write_publishes_optimistic_state(self):
        em = _make_em()
        em._api.write_point.return_value = True
        em.dynamic_point_map._table[100] = self._entry(100, is_controlling=False)
        em._handle_command_worker(
            self._entity_info(point_id=100), 1, '1', 'cmd1',
        )
        em.mqtt.publish.assert_called_once_with('nibe/state/100', '1', retain=True)
        self.assertEqual(em.last_states[100], '1')


# ===========================================================================
# 53. EntityManager._process_and_publish_state — raw value -> HA state
# ===========================================================================


class TestFetchBulkDataNewPointRouting(unittest.TestCase):
    """The three-way decision for a point appearing in the bulk response
    that wasn't in baseline_point_ids/published_configs: (1) during a
    post-write scan window, always treated as a dynamic appearance; (2) a
    known dynamic point appearing outside the scan window is STILL routed
    as dynamic, not mistakenly indexed as permanent; (3) a genuinely
    unknown point outside any scan window is indexed as a new permanent
    static point. Misrouting (2) as (3) would mean a dynamic point gets
    permanently and incorrectly indexed as static."""

    def _response(self, point_id, title='New point', writable=True):
        return {
            str(point_id): {
                'title': title, 'description': '',
                'metadata': {
                    'modbusRegisterType': 'MODBUS_HOLDING_REGISTER' if writable else 'MODBUS_INPUT_REGISTER',
                    'minValue': 0, 'maxValue': 100, 'isWritable': writable,
                },
                'value': {'integerValue': 0, 'stringValue': '', 'isOk': True},
            }
        }

    def _ready_em(self):
        em = _make_em()
        em.initial_discovery_complete = True
        return em

    def test_post_write_scan_routes_new_point_as_dynamic(self):
        em = self._ready_em()
        em._post_write_active = True
        em._api.fetch_bulk_points.return_value = self._response(300)
        with patch.object(em, '_publish_dynamic_changes') as mock_pub_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub_dyn.assert_called_once()
        new_points_arg = mock_pub_dyn.call_args.args[0]
        self.assertEqual([pid for pid, _ in new_points_arg], [300])

    def test_known_dynamic_point_outside_scan_window_still_routed_dynamic(self):
        """The case explicitly called out in the source comment: fast-path
        retries exhausted, firmware propagated late, no scan window active
        — must still route as dynamic, not get indexed as static. Point 301
        here is a DEPENDENT dynamic point (like the real THS-10's humidity
        sensor depending on its controlling point 5102) — is_known_dynamic
        checks dynamic_points_by_value's values, not the controlling entry's
        own point_id, so the fixture's controlling entry uses a different id."""
        em = self._ready_em()
        em._post_write_active = False
        em.dynamic_point_map._table[5102] = self._fake_dynamic_entry(
            controlling_id=5102, dynamic_dependent_id=301,
        )
        em._api.fetch_bulk_points.return_value = self._response(301)
        with patch.object(em, '_publish_dynamic_changes') as mock_pub_dyn, \
             patch.object(em, '_index_point') as mock_index:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub_dyn.assert_called_once()
        mock_index.assert_not_called()

    def test_genuinely_unknown_point_outside_scan_window_indexed_as_static(self):
        em = self._ready_em()
        em._post_write_active = False
        em._api.fetch_bulk_points.return_value = self._response(302, title='Firmware update point')
        with patch.object(em, '_publish_dynamic_changes') as mock_pub_dyn, \
             patch.object(em, '_index_point') as mock_index:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub_dyn.assert_not_called()
        mock_index.assert_called_once()
        indexed_arg = mock_index.call_args.args[0]
        self.assertEqual(indexed_arg['display_title'], 'Firmware update point')
        self.assertFalse(indexed_arg['is_dynamic'])

    def test_point_already_in_baseline_not_treated_as_new(self):
        """A point already known from initial discovery must never trigger
        the new-point routing at all, regardless of scan window state."""
        em = self._ready_em()
        em.baseline_point_ids.add(303)
        em._api.fetch_bulk_points.return_value = self._response(303)
        with patch.object(em, '_publish_dynamic_changes') as mock_pub_dyn, \
             patch.object(em, '_index_point') as mock_index:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub_dyn.assert_not_called()
        mock_index.assert_not_called()

    def test_detect_changes_false_never_routes_as_new(self):
        """detect_changes=False (e.g. the simplified no-detection poll
        path) must skip the new-point routing entirely, regardless of
        scan window or dynamic-map state."""
        em = self._ready_em()
        em._post_write_active = True
        em._api.fetch_bulk_points.return_value = self._response(304)
        with patch.object(em, '_publish_dynamic_changes') as mock_pub_dyn:
            em._fetch_bulk_data(detect_changes=False)
        mock_pub_dyn.assert_not_called()

    def _fake_dynamic_entry(self, controlling_id, dynamic_dependent_id):
        from nibe_dynamic_map import DynamicPointEntry
        return DynamicPointEntry(
            point_id=controlling_id, title=f'Point {controlling_id}', entity_type='switch',
            processed_values={1}, is_controlling=True,
            dynamic_points_by_value={1: [dynamic_dependent_id]},
        )


# ===========================================================================
# 57. EntityManager._handle_command_worker — write-failure path
# ===========================================================================


class TestFetchBulkDataDisappearedPoints(unittest.TestCase):
    """The counterpart to the new-point routing tested earlier: detecting
    when a previously-active dynamic point (e.g. a THS-10 humidity sensor
    after the accessory is unpaired) drops out of the bulk response. Two
    distinct mechanisms combine here — known dynamic points going absent,
    and (during a post-write scan specifically) baseline points vanishing
    entirely, which is how the bridge first discovers a point is actually
    dynamic. The second mechanism has a real side effect (permanently
    removing the point from baseline_point_ids) worth verifying precisely,
    since getting it wrong either loses track of a real static point or
    fails to ever recognize a genuinely dynamic one."""

    def _response(self, point_ids):
        """A bulk response containing exactly the given point_ids."""
        return {
            str(pid): {
                'title': f'Point {pid}', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'minValue': 0, 'maxValue': 100},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
            for pid in point_ids
        }

    def _ready_em(self):
        em = _make_em()
        em.initial_discovery_complete = True
        return em

    def _dynamic_entry(self, controlling_id, dependent_ids):
        from nibe_dynamic_map import DynamicPointEntry
        return DynamicPointEntry(
            point_id=controlling_id, title=f'Point {controlling_id}', entity_type='switch',
            processed_values={1}, is_controlling=True,
            dynamic_points_by_value={1: list(dependent_ids)},
        )

    # -- known dynamic point disappearing -------------------------------------

    def test_known_active_dynamic_point_absent_is_detected(self):
        """A known dynamic point that was active and is now missing from
        the bulk response must be reported as disappeared."""
        em = self._ready_em()
        em.dynamic_point_map._table[5102] = self._dynamic_entry(5102, [50827])
        em.active_dynamic_points = {50827}
        # 5102 (controller) still present, 50827 (dependent) now gone.
        em._api.fetch_bulk_points.return_value = self._response([5102])
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub.assert_called_once()
        _, disappeared_arg = mock_pub.call_args.args
        self.assertEqual(disappeared_arg, {50827})

    def test_known_dynamic_but_not_currently_active_not_reported(self):
        """A known dynamic point that's absent but was never marked
        active (e.g. it was already inactive) must not be spuriously
        reported as a NEW disappearance."""
        em = self._ready_em()
        em.dynamic_point_map._table[5102] = self._dynamic_entry(5102, [50827])
        em.active_dynamic_points = set()  # 50827 never became active
        em._api.fetch_bulk_points.return_value = self._response([5102])
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub.assert_not_called()

    def test_known_active_dynamic_point_still_present_not_reported(self):
        """Note: 50827 must be pre-seeded into baseline_point_ids and
        published_configs, otherwise the earlier new-point routing logic
        (tested in a previous round) would treat its appearance here as a
        brand-new point rather than 'still present' — confirmed by an
        initial failed run of this exact test before adding the seeding."""
        em = self._ready_em()
        em.dynamic_point_map._table[5102] = self._dynamic_entry(5102, [50827])
        em.active_dynamic_points = {50827}
        em.baseline_point_ids = {5102, 50827}
        em.published_configs = {5102, 50827}
        em._api.fetch_bulk_points.return_value = self._response([5102, 50827])
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub.assert_not_called()

    # -- post-write-scan baseline disappearance --------------------------------

    def test_post_write_scan_baseline_point_vanishing_is_newly_discovered(self):
        """During a post-write scan, a baseline (previously assumed static)
        point disappearing entirely is the FIRST discovery that it's
        actually dynamic — must be reported as disappeared even though it
        was never in dynamic_point_map at all."""
        em = self._ready_em()
        em._post_write_active = True
        em.baseline_point_ids = {600, 601}
        em._api.fetch_bulk_points.return_value = self._response([600])  # 601 vanished
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub.assert_called_once()
        _, disappeared_arg = mock_pub.call_args.args
        self.assertIn(601, disappeared_arg)

    def test_post_write_scan_removes_vanished_point_from_baseline(self):
        """The real side effect: a baseline point discovered to be dynamic
        this way must be permanently removed from baseline_point_ids, not
        just reported once and left in place."""
        em = self._ready_em()
        em._post_write_active = True
        em.baseline_point_ids = {600, 601}
        em._api.fetch_bulk_points.return_value = self._response([600])
        with patch.object(em, '_publish_dynamic_changes'):
            em._fetch_bulk_data(detect_changes=True)
        self.assertNotIn(601, em.baseline_point_ids)
        self.assertIn(600, em.baseline_point_ids)  # untouched, still present

    def test_post_write_scan_already_known_dynamic_point_not_double_counted(self):
        """A baseline point that ALSO happens to be a known dynamic point
        must not be processed by both mechanisms redundantly — the
        newly_absent calculation explicitly excludes known_dynamic_ids."""
        em = self._ready_em()
        em._post_write_active = True
        em.dynamic_point_map._table[700] = self._dynamic_entry(700, [701])
        em.baseline_point_ids = {701}  # also a known dynamic dependent
        em.active_dynamic_points = {701}
        em._api.fetch_bulk_points.return_value = self._response([700])  # 701 gone
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        # 701 should be detected exactly once via the known-dynamic path,
        # not duplicated via the baseline path.
        _, disappeared_arg = mock_pub.call_args.args
        self.assertEqual(disappeared_arg, {701})

    def test_no_post_write_scan_baseline_disappearance_not_detected(self):
        """Outside a post-write scan window, a baseline point vanishing is
        NOT treated as a dynamic disappearance via this mechanism — only
        the known-dynamic-points path applies when no scan is active."""
        em = self._ready_em()
        em._post_write_active = False
        em.baseline_point_ids = {600, 601}
        em._api.fetch_bulk_points.return_value = self._response([600])
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub.assert_not_called()
        self.assertIn(601, em.baseline_point_ids)  # left untouched

    # -- gating conditions -----------------------------------------------------

    def test_detect_changes_false_skips_disappearance_detection_entirely(self):
        em = self._ready_em()
        em.dynamic_point_map._table[5102] = self._dynamic_entry(5102, [50827])
        em.active_dynamic_points = {50827}
        em._api.fetch_bulk_points.return_value = self._response([5102])
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=False)
        mock_pub.assert_not_called()

    def test_initial_discovery_incomplete_skips_disappearance_detection(self):
        """Before initial discovery finishes, disappearance detection must
        not run — there's no reliable baseline yet to compare against."""
        em = _make_em()
        em.initial_discovery_complete = False  # explicit, matches the default
        em.dynamic_point_map._table[5102] = self._dynamic_entry(5102, [50827])
        em.active_dynamic_points = {50827}
        em._api.fetch_bulk_points.return_value = self._response([5102])
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        mock_pub.assert_not_called()

    def test_published_configs_updated_to_current_point_ids_regardless(self):
        """published_configs tracking must happen unconditionally, even
        with no dynamic changes at all — it's the bookkeeping for what
        was actually seen this poll, not tied to change detection."""
        em = self._ready_em()
        em._api.fetch_bulk_points.return_value = self._response([800, 801])
        em._fetch_bulk_data(detect_changes=True)
        self.assertEqual(em.published_configs, {800, 801})


# ===========================================================================
# 62. resolve_unit / _build_point_metadata_dict — modal unit-override bug fix
# ===========================================================================


class TestDynamicPointMapRemainingPaths(unittest.TestCase):

    def _make_map(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        dm = DynamicPointMap()
        entry = DynamicPointEntry(
            point_id=100, title='Test Switch', entity_type='switch',
            processed_values=set(), unprocessed_values={0, 1},
            is_controlling=None, firmware_removed=False,
        )
        dm._table[100] = entry
        return dm, entry

    def test_values_returns_table_values(self):
        dm, entry = self._make_map()
        self.assertIn(entry, list(dm.values()))

    def test_items_returns_table_items(self):
        dm, entry = self._make_map()
        self.assertIn((100, entry), list(dm.items()))

    def test_flush_resets_all_entries_to_unprocessed(self):
        dm, entry = self._make_map()
        entry.processed_values   = {0, 1}
        entry.unprocessed_values = set()
        entry.is_controlling     = True
        all_points = {100: {'metadata': {'minValue': 0, 'maxValue': 1}}}
        dm.flush(all_points, {100: 'switch'})
        self.assertEqual(entry.processed_values,   set())
        self.assertIn(0, entry.unprocessed_values)
        self.assertIn(1, entry.unprocessed_values)
        self.assertIsNone(entry.is_controlling)

    def test_flush_empty_range_defaults_to_0_1(self):
        """When minValue == maxValue the range is empty — flush must default to {0,1}."""
        dm, entry = self._make_map()
        # Degenerate range: min == max → set(range(5,5)) == set()
        all_points = {100: {'metadata': {'minValue': 5, 'maxValue': 4}}}
        dm.flush(all_points, {100: 'switch'})
        self.assertEqual(entry.unprocessed_values, {0, 1})

    def test_record_outcome_all_empty_sets_is_controlling_false(self):
        """After all values are processed with no dynamic points found,
        is_controlling must be set to False."""
        dm, entry = self._make_map()
        # Process value 0 with no new points
        dm.record_outcome(100, 0, [])
        # Now process value 1 with no new points — this should finalise is_controlling=False
        dm.record_outcome(100, 1, [])
        self.assertIs(entry.is_controlling, False)

    def test_from_file_oserror_returns_zero(self):
        """An OSError (e.g. permission denied) during from_file must return 0 gracefully."""
        from nibe_dynamic_map import DynamicPointMap
        dm = DynamicPointMap()
        with patch('builtins.open', side_effect=OSError('permission denied')):
            result = dm.from_file('/some/path.json')
        self.assertEqual(result, 0)

    def test_from_file_not_found_returns_zero(self):
        """Missing file on first run must return 0 (not raise)."""
        from nibe_dynamic_map import DynamicPointMap
        dm = DynamicPointMap()
        result = dm.from_file('/nonexistent/path/map.json')
        self.assertEqual(result, 0)



class TestDynamicPointMapExpectedActive(unittest.TestCase):
    """Extended tests for expected_active_dynamic_points."""

    def test_multiple_controlling_entries(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        dm = DynamicPointMap()
        dm._table[100] = DynamicPointEntry(
            point_id=100, title='Sw1', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [1001, 1002]}
        )
        dm._table[200] = DynamicPointEntry(
            point_id=200, title='Sel1', entity_type='select',
            processed_values={0, 1, 2}, is_controlling=True,
            dynamic_points_by_value={2: [2001]}
        )
        current = {100: 1, 200: 2}
        expected = dm.expected_active_dynamic_points(current)
        self.assertEqual(expected, {1001, 1002, 2001})

    def test_overlapping_dynamic_points_from_different_controllers(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        dm = DynamicPointMap()
        dm._table[100] = DynamicPointEntry(
            point_id=100, title='Sw1', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [999]}
        )
        dm._table[200] = DynamicPointEntry(
            point_id=200, title='Sw2', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [999]}
        )
        current = {100: 1, 200: 1}
        expected = dm.expected_active_dynamic_points(current)
        self.assertEqual(expected, {999})


# ===========================================================================
# 84. nibe_entity_detection — remaining uncovered paths
# ===========================================================================


class TestDynamicMapRemainingPaths(unittest.TestCase):
    """nibe_dynamic_map.py: empty range fallback; is_controlling=False
    when all processed values yielded no dynamic points."""

    def test_populate_from_bulk_empty_range_defaults_to_0_1(self):
        """If minValue == maxValue, range() is empty → fallback {0, 1}."""
        from nibe_dynamic_map import DynamicPointMap
        dm = DynamicPointMap()
        point = {
            'display_title': 'Degenerate',
            'metadata': {'minValue': 5, 'maxValue': 4},  # inverted → empty range
        }
        dm.populate_from_bulk({100: point}, {100: 'switch'})
        entry = dm.get(100)
        self.assertIsNotNone(entry)
        # unprocessed_values must contain both 0 and 1
        self.assertIn(0, entry.unprocessed_values)
        self.assertIn(1, entry.unprocessed_values)

    def test_record_outcome_sets_is_controlling_false_when_all_empty(self):
        """When all values for a point have been processed and none produced
        dynamic points, is_controlling is set to False."""
        from nibe_dynamic_map import DynamicPointMap
        dm = DynamicPointMap()
        # Populate with a 2-value switch (values 0 and 1)
        point = {
            'display_title': 'Mode',
            'metadata': {'minValue': 0, 'maxValue': 1},
        }
        dm.populate_from_bulk({200: point}, {200: 'switch'})
        # Record both values as producing no dynamic points
        dm.record_outcome(200, 0, [])
        dm.record_outcome(200, 1, [])
        entry = dm.get(200)
        self.assertIs(entry.is_controlling, False)



class TestPendingWriteAbsentFromBulk(unittest.TestCase):
    """Pending write for a point absent from bulk_data (branch 1145→1166)."""

    def _setup(self):
        em = _make_em()
        pid = 7777
        em.bulk_data[pid] = {
            'raw_value': 0, 'is_ok': True, 'string_value': '',
            'metadata': {'variableSize': 'u8', 'divisor': 1,
                         'unit': '', 'change': 0, 'decimal': 0},
            'title': 'Test',
        }
        entity_info = {
            'point_id': pid, 'entity_type': 'sensor',
            'availability_topic': f'nibe/avail/{pid}',
            'state_topic': f'nibe/state/{pid}',
            'command_topic': None, 'point_data': {},
        }
        em.active_entities_by_id[pid] = entity_info
        em.mqtt_enabled_points.add(pid)
        em.pending_writes[pid] = {
            'value': 1, 'timestamp': 1e18, 'time': 1e18, 'cmd_id': 'test',
        }
        return em, pid, entity_info

    def test_pending_write_held_when_point_absent_from_bulk(self):
        """Pending entry must be retained when bulk_data has no entry — a None
        bulk_raw must not be treated as a confirmation of the write."""
        em, pid, entity_info = self._setup()
        del em.bulk_data[pid]
        em._post_write_active = False
        em._update_entity_state(entity_info)
        self.assertIn(pid, em.pending_writes,
                      "Pending entry must be retained when point absent from bulk_data")
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == entity_info['state_topic']]
        self.assertFalse(state_calls,
                         "State must not be published when point absent from bulk_data")


# ---------------------------------------------------------------------------
# record_outcome all_empty=False branch (nibe_dynamic_map)
# ---------------------------------------------------------------------------


class TestDisappearedPointsSetAlgebra(unittest.TestCase):
    """Direct property tests for the disappeared-points computation in
    _fetch_bulk_data."""

    _PID_POOL = st.integers(min_value=1, max_value=9999)
    _PID_SET  = st.frozensets(_PID_POOL, max_size=10)

    @staticmethod
    def _make_em_with_dynamic_map(known_dynamic, active_dynamic):
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em.initial_discovery_complete = True
        em._post_write_active = False
        for pid in known_dynamic:
            entry = DynamicPointEntry(
                point_id=pid, title=f'Dyn {pid}', entity_type='switch',
                unprocessed_values=set(), processed_values={0, 1},
            )
            entry.dynamic_points_by_value = {0: set(), 1: {pid + 10000}}
            em.dynamic_point_map._table[pid] = entry
        em.active_dynamic_points = set(active_dynamic)
        return em

    @staticmethod
    def _raw_api(pids):
        return {
            str(pid): {'title': f'P{pid}', 'metadata': {},
                       'value': {'integerValue': 0, 'isOk': True}}
            for pid in pids
        }

    @given(_PID_SET, _PID_SET, _PID_SET)
    def test_normal_disappeared_equals_set_difference(
            self, known_dynamic, active_dynamic, current_ids):
        """disappeared = (known_dynamic ∩ active_dynamic) − current_ids."""
        em = self._make_em_with_dynamic_map(known_dynamic, active_dynamic)
        for pid in current_ids:
            em.bulk_data[pid] = {'raw_value': 0, 'is_ok': True,
                                 'metadata': {}, 'title': '', 'string_value': ''}
        expected = (em.dynamic_point_map.all_known_dynamic_point_ids()
                    & em.active_dynamic_points - current_ids)
        em._api.fetch_bulk_points.return_value = self._raw_api(current_ids)
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        if expected:
            self.assertTrue(mock_pub.called)
            _, actual = mock_pub.call_args.args
            self.assertEqual(actual, expected)
        else:
            if mock_pub.called:
                _, actual = mock_pub.call_args.args
                self.assertEqual(actual, set())

    @given(_PID_SET, _PID_SET, _PID_SET)
    def test_post_write_baseline_disappearance(
            self, baseline, known_dynamic, current_ids):
        """Post-write: baseline − current_ids − known_dynamic ⊆ disappeared."""
        assume(current_ids)
        em = self._make_em_with_dynamic_map(known_dynamic, frozenset())
        em._post_write_active = True
        em._post_write_until  = 1e18
        em.baseline_point_ids = set(baseline)
        for pid in current_ids:
            em.bulk_data[pid] = {'raw_value': 0, 'is_ok': True,
                                 'metadata': {}, 'title': '', 'string_value': ''}
        known_ids = em.dynamic_point_map.all_known_dynamic_point_ids()
        expected_newly_absent = baseline - current_ids - known_ids
        em._api.fetch_bulk_points.return_value = self._raw_api(current_ids)
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        if expected_newly_absent:
            self.assertTrue(mock_pub.called)
            _, actual = mock_pub.call_args.args
            self.assertTrue(expected_newly_absent <= actual)

    @given(_PID_SET, _PID_SET)
    def test_non_dynamic_points_never_in_disappeared(self, all_pids, current_ids):
        """With no known-dynamic points and no post-write, disappeared must be empty."""
        em = self._make_em_with_dynamic_map(frozenset(), frozenset())
        em._post_write_active = False
        for pid in all_pids:
            em.bulk_data[pid] = {'raw_value': 0, 'is_ok': True,
                                 'metadata': {}, 'title': '', 'string_value': ''}
        em._api.fetch_bulk_points.return_value = self._raw_api(current_ids)
        with patch.object(em, '_publish_dynamic_changes') as mock_pub:
            em._fetch_bulk_data(detect_changes=True)
        if mock_pub.called:
            _, actual = mock_pub.call_args.args
            self.assertEqual(actual, set())



# ===========================================================================
# Phase 2 mutmut survivor tests — nibe_dynamic_map.py genuine logic gaps
# ===========================================================================


class TestRecordOutcomeIsControllingGuard(unittest.TestCase):
    """record_outcome: 'is None' not 'is not None' guards.

    Two survivors:
    - line 367: is_controlling is None and not unprocessed_values — sets False
      only when controlling state is undetermined AND all values processed
    - line 397: is_controlling is None — sets False only when undetermined
      (must not overwrite True set by a previous controlling recording)
    """

    def _entry(self, **kwargs):
        from nibe_dynamic_map import DynamicPointEntry
        defaults = dict(
            point_id=100, title='Switch', entity_type='switch',
            unprocessed_values={0, 1},
        )
        defaults.update(kwargs)
        return DynamicPointEntry(**defaults)

    def test_is_controlling_not_overwritten_after_set_true(self):
        """Once is_controlling=True (set by a controlling value), a subsequent
        non-controlling value recording must NOT reset it to False."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        m._table[100] = self._entry(unprocessed_values={0, 1})
        # First: value=1 is controlling → is_controlling becomes True
        m.record_outcome(100, 1, [200])
        entry = m.get(100)
        self.assertTrue(entry.is_controlling)
        # Second: value=0 produces no dynamic points (non-controlling)
        # is_controlling must remain True, not be overwritten to False
        m.record_outcome(100, 0, [])
        entry = m.get(100)
        self.assertTrue(entry.is_controlling,
                        "is_controlling must not be reset from True to False")

    def test_is_controlling_set_false_only_when_none(self):
        """is_controlling is set to False only when it's still None
        (undetermined) after all values processed — not when already True."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        # Non-controlling switch: both values produce no dynamic points
        m._table[100] = self._entry(unprocessed_values={0, 1})
        m.record_outcome(100, 1, [])  # no dynamic points
        m.record_outcome(100, 0, [])  # no dynamic points → all processed
        entry = m.get(100)
        # All values processed, none controlling → is_controlling should be False
        self.assertFalse(entry.is_controlling)


class TestPopulateFromBulkContinueNotBreak(unittest.TestCase):
    """populate_from_bulk: 'continue' not 'break' for non-switch/select points.

    mutmut_10: continue → break. With break, processing stops at the first
    non-switch/select point, preventing subsequent switches from being added.
    """

    def test_non_switch_point_before_switch_does_not_stop_processing(self):
        """A sensor before a switch in the iteration must be skipped (continue),
        not stop processing (break)."""
        from nibe_dynamic_map import DynamicPointMap

        m = DynamicPointMap()
        points = {
            100: {'display_title': 'Sensor', 'metadata': {'minValue': 0, 'maxValue': 100}},
            200: {'display_title': 'Switch', 'metadata': {'minValue': 0, 'maxValue': 1}},
        }
        types = {100: 'sensor', 200: 'switch'}
        added = m.populate_from_bulk(points, types)
        # Switch at 200 must be added despite sensor at 100 coming first
        self.assertEqual(added, 1)
        self.assertIn(200, m._table)
        self.assertNotIn(100, m._table)


class TestFlushRangeCalculation(unittest.TestCase):
    """flush: range(min_val, max_val + 1) not max_val - 1.

    Also tests minValue default 0 not 1.
    """

    def _make_map(self, point_id, min_val=None, max_val=None):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[point_id] = DynamicPointEntry(
            point_id=point_id, title='T', entity_type='select',
            processed_values={0, 1, 2},
            unprocessed_values=set(),
        )
        meta = {}
        if min_val is not None:
            meta['minValue'] = min_val
        if max_val is not None:
            meta['maxValue'] = max_val
        points = {point_id: {'display_title': 'T', 'metadata': meta}}
        types = {point_id: 'select'}
        m.flush(points, types)
        return m

    def test_flush_includes_max_val_in_range(self):
        """range(min, max+1) includes max — max-1 would exclude it."""
        m = self._make_map(100, min_val=0, max_val=3)
        entry = m.get(100)
        # With +1: {0,1,2,3}. With -1: {0,1} — 3 would be missing
        self.assertIn(3, entry.unprocessed_values,
                      "max_val must be included (range uses +1, not -1)")
        self.assertEqual(entry.unprocessed_values, {0, 1, 2, 3})

    def test_flush_min_val_default_is_zero_not_one(self):
        """minValue absent → defaults to 0, not 1."""
        m = self._make_map(100, min_val=None, max_val=1)
        entry = m.get(100)
        # default 0: range(0,2) = {0,1}. default 1: range(1,2) = {1}
        self.assertIn(0, entry.unprocessed_values,
                      "minValue default must be 0, not 1")


class TestExpectedActiveContinueNotBreak(unittest.TestCase):
    """expected_active_dynamic_points: 'continue' not 'break'.

    Two continue statements mutated to break:
    - line 237: skips non-controlling entries (break would stop all processing)
    - line 240: skips entries with no current value (break would stop)
    """

    def _make_map_with_entries(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        # Entry 1: non-controlling — must be skipped, not stop iteration
        m._table[100] = DynamicPointEntry(
            point_id=100, title='T1', entity_type='sensor',
            is_controlling=False,
            processed_values={0, 1}, unprocessed_values=set(),
            dynamic_points_by_value={},
        )
        # Entry 2: controlling with known dynamic points
        m._table[200] = DynamicPointEntry(
            point_id=200, title='T2', entity_type='switch',
            is_controlling=True,
            processed_values={0, 1}, unprocessed_values=set(),
            dynamic_points_by_value={1: [300]},
        )
        return m

    def test_non_controlling_entry_does_not_stop_iteration(self):
        """Non-controlling entry at 100 must be skipped; controlling at 200 must still contribute."""
        m = self._make_map_with_entries()
        result = m.expected_active_dynamic_points({100: 0, 200: 1})
        # With continue: 300 is in result. With break: result is empty.
        self.assertIn(300, result,
                      "Non-controlling entry must not stop iteration (continue, not break)")

    def test_entry_with_no_current_value_does_not_stop_iteration(self):
        """Entry with no current value (None) must be skipped, not stop iteration."""
        m = self._make_map_with_entries()
        # 100 has is_controlling=False, 200 has value but 100 doesn't
        result = m.expected_active_dynamic_points({200: 1})  # 100 has no value
        self.assertIn(300, result)


class TestRecordOutcomeLenZeroCheck(unittest.TestCase):
    """record_outcome: len(pts) == 0 not == 1 or != 0."""

    def _dmap(self, min_val=0, max_val=1):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[200] = DynamicPointEntry(
            point_id=200, title='T', entity_type='switch',
            unprocessed_values={0, 1},
        )
        return m

    def test_zero_dynamic_points_does_not_set_controlling(self):
        """len(pts)==0 → not controlling for this value. With ==1: single
        dynamic point would wrongly be treated as non-controlling."""
        m = self._dmap()
        m.record_outcome(200, 0, [])        # 0 pts → not controlling
        m.record_outcome(200, 1, [300])     # 1 pt  → IS controlling
        self.assertTrue(m.get(200).is_controlling)

    def test_one_dynamic_point_is_controlling(self):
        """len(pts)==1 (not 0) → IS controlling. With ==1 mutation: 1 pt
        would be treated as non-controlling (same as 0)."""
        m = self._dmap()
        m.record_outcome(200, 1, [300])
        m.record_outcome(200, 0, [])
        entry = m.get(200)
        self.assertTrue(entry.is_controlling)
        self.assertIn(300, entry.dynamic_points_by_value.get(1, []))

    def test_two_dynamic_points_is_also_controlling(self):
        m = self._dmap()
        m.record_outcome(200, 1, [300, 301])
        self.assertTrue(m.get(200).is_controlling)


class TestPopulateFromBulkFieldAssignments(unittest.TestCase):
    """populate_from_bulk: correct field assignments in DynamicPointEntry.

    mutmut_60: processed_values=set() → unprocessed_values=all_vals (wrong field)
    mutmut_62: is_controlling=None → firmware_removed=False (wrong field)
    mutmut_42: range(min_val, max_val+1) → range(max_val+1) (drops min_val)
    """

    def _populate(self, point_id=200, min_val=0, max_val=1, entity_type='switch'):
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        points = {point_id: {
            'display_title': 'Switch',
            'metadata': {'minValue': min_val, 'maxValue': max_val},
        }}
        types = {point_id: entity_type}
        m.populate_from_bulk(points, types)
        return m.get(point_id)

    def test_processed_values_starts_empty(self):
        """processed_values must start as set(), not be assigned all_vals."""
        entry = self._populate()
        self.assertEqual(entry.processed_values, set(),
                         "processed_values must start empty")

    def test_unprocessed_values_starts_as_full_range(self):
        """unprocessed_values must start as the full range {min..max}."""
        entry = self._populate(min_val=0, max_val=3)
        self.assertEqual(entry.unprocessed_values, {0, 1, 2, 3})

    def test_is_controlling_starts_as_none(self):
        """is_controlling must start as None (undetermined), not False."""
        entry = self._populate()
        self.assertIsNone(entry.is_controlling,
                          "is_controlling must be None initially")

    def test_firmware_removed_starts_as_false(self):
        """firmware_removed must start as False, independently of is_controlling."""
        entry = self._populate()
        self.assertFalse(entry.firmware_removed)

    def test_range_includes_min_val(self):
        """range(min_val, max_val+1) — min_val=2 means {2,3,4}, not {0,1,2,3,4}."""
        entry = self._populate(min_val=2, max_val=4)
        self.assertNotIn(0, entry.unprocessed_values)
        self.assertNotIn(1, entry.unprocessed_values)
        self.assertIn(2, entry.unprocessed_values)
        self.assertIn(4, entry.unprocessed_values)


# ===========================================================================
# Phase 2 round 3 — nibe_dynamic_map.py remaining genuine survivors
# ===========================================================================


class TestExpectedActiveDynamicPointsValueLookup(unittest.TestCase):
    """expected_active_dynamic_points: pts truthiness check kills pts=[] survivors.

    The check 'if pts:' (not 'if pts is not None:') — an empty list for a value
    must not contribute active points. A mutation 'if pts is not None:' would
    incorrectly add zero dynamic points from an empty list (no actual effect, but
    'if pts:' is the canonical check and mutmut can produce survivors here).

    Also pins: current_values lookup uses point_id not a constant.
    """

    def _make_map(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        # Controlling switch: value=1 → [300]; value=0 → []
        m._table[100] = DynamicPointEntry(
            point_id=100, title='T', entity_type='switch',
            is_controlling=True,
            processed_values={0, 1}, unprocessed_values=set(),
            dynamic_points_by_value={1: [300], 0: []},
        )
        return m

    def test_value_with_empty_list_contributes_no_points(self):
        """value=0 → dynamic_points_by_value[0]=[] → empty list → no active points."""
        m = self._make_map()
        result = m.expected_active_dynamic_points({100: 0})
        self.assertNotIn(300, result)
        self.assertEqual(len(result), 0)

    def test_value_with_points_contributes_them(self):
        """value=1 → dynamic_points_by_value[1]=[300] → 300 is active."""
        m = self._make_map()
        result = m.expected_active_dynamic_points({100: 1})
        self.assertIn(300, result)

    def test_missing_value_in_current_values_skipped(self):
        """current_values has no entry for point 100 → skipped, result empty."""
        m = self._make_map()
        result = m.expected_active_dynamic_points({})
        self.assertEqual(len(result), 0)

    def test_firmware_removed_entry_skipped(self):
        """firmware_removed=True → entry skipped even if is_controlling=True."""
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[100] = DynamicPointEntry(
            point_id=100, title='T', entity_type='switch',
            is_controlling=True, firmware_removed=True,
            processed_values={1}, unprocessed_values=set(),
            dynamic_points_by_value={1: [300]},
        )
        result = m.expected_active_dynamic_points({100: 1})
        self.assertNotIn(300, result)


class TestDeserialiseReturnCount(unittest.TestCase):
    """deserialise: returns count of loaded entries; invalid JSON returns 0.

    mutmut survivors: loaded += 1 → loaded += 0 (count never increments);
    return loaded → return 0 (always returns 0).
    """

    def _map(self):
        from nibe_dynamic_map import DynamicPointMap
        return DynamicPointMap()

    def test_valid_json_returns_entry_count(self):
        """Two valid entries → returns 2."""
        import json
        payload = {
            "100": {"point_id": 100, "title": "T", "entity_type": "switch",
                    "processed_values": [], "unprocessed_values": [0, 1],
                    "is_controlling": None, "dynamic_points_by_value": {},
                    "firmware_removed": False},
            "200": {"point_id": 200, "title": "U", "entity_type": "select",
                    "processed_values": [], "unprocessed_values": [0, 1, 2],
                    "is_controlling": None, "dynamic_points_by_value": {},
                    "firmware_removed": False},
        }
        m = self._map()
        count = m.deserialise(json.dumps(payload))
        self.assertEqual(count, 2)

    def test_empty_dict_returns_zero(self):
        """Empty JSON object → 0 entries loaded."""
        import json
        m = self._map()
        count = m.deserialise(json.dumps({}))
        self.assertEqual(count, 0)

    def test_invalid_json_returns_zero(self):
        """JSON parse error → returns 0 (not raises)."""
        m = self._map()
        count = m.deserialise('not valid json {{{')
        self.assertEqual(count, 0)

    def test_non_dict_json_returns_zero(self):
        """JSON list (not dict) → returns 0."""
        import json
        m = self._map()
        count = m.deserialise(json.dumps([1, 2, 3]))
        self.assertEqual(count, 0)

    def test_loaded_entries_are_accessible(self):
        """Entries loaded by deserialise are actually in the table."""
        import json
        payload = {
            "100": {"point_id": 100, "title": "T", "entity_type": "switch",
                    "processed_values": [1], "unprocessed_values": [],
                    "is_controlling": True, "dynamic_points_by_value": {"1": [200]},
                    "firmware_removed": False},
        }
        m = self._map()
        m.deserialise(json.dumps(payload))
        self.assertIn(100, m._table)
        self.assertTrue(m._table[100].is_controlling)


class TestToFileTmpPattern(unittest.TestCase):
    """to_file: writes to .tmp then renames; returns True on success, False on OSError.

    mutmut survivors: path + '.tmp' → path + '.tmp2' (wrong tmp name);
    os.replace(tmp, path) args swapped; return True → return False.
    """

    def test_to_file_returns_true_on_success(self):
        """Successful write → returns True."""
        import tempfile
        import os
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            result = m.to_file(path)
            self.assertTrue(result)
            self.assertTrue(os.path.exists(path))
        finally:
            os.unlink(path)

    def test_to_file_content_is_valid_json(self):
        """File written by to_file must contain valid JSON."""
        import tempfile
        import json
        import os
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[100] = DynamicPointEntry(
            point_id=100, title='T', entity_type='switch',
            processed_values={1}, unprocessed_values=set(),
            is_controlling=True,
            dynamic_points_by_value={1: [200]},
        )
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            m.to_file(path)
            with open(path) as f:
                data = json.load(f)
            self.assertIn('100', data)
        finally:
            os.unlink(path)

    def test_to_file_returns_false_on_oserror(self):
        """OSError (e.g. unwritable path) → returns False (not raises)."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        result = m.to_file('/nonexistent_dir/nope/map.json')
        self.assertFalse(result)

    def test_to_file_tmp_file_cleaned_up_on_success(self):
        """The .tmp file must not remain after a successful write."""
        import tempfile
        import os
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            m.to_file(path)
            self.assertFalse(os.path.exists(path + '.tmp'),
                             ".tmp file must be removed after successful write")
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestDynamicPointMapGet(unittest.TestCase):
    """DynamicPointMap.get: returns entry for known key, default for unknown.

    mutmut survivors: self._table.get(point_id, default) → .get(point_id)
    (drops default) — absent key raises KeyError instead of returning None.
    """

    def _map_with_entry(self):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[100] = DynamicPointEntry(
            point_id=100, title='T', entity_type='switch')
        return m

    def test_get_known_key_returns_entry(self):
        m = self._map_with_entry()
        entry = m.get(100)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.point_id, 100)

    def test_get_unknown_key_returns_none(self):
        """Unknown key → default None, not KeyError."""
        m = self._map_with_entry()
        result = m.get(999)
        self.assertIsNone(result)

    def test_get_unknown_key_returns_custom_default(self):
        """Custom default returned for missing key."""
        m = self._map_with_entry()
        sentinel = object()
        result = m.get(999, sentinel)
        self.assertIs(result, sentinel)


class TestRestoreFromBulkClearsFlag(unittest.TestCase):
    """restore_from_bulk: clears firmware_removed when point reappears.

    mutmut survivors: entry.firmware_removed = False → True (inverted assignment);
    'and point_id in bulk_point_ids' condition mutation.
    """

    def _map_with_removed(self, point_id=100):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[point_id] = DynamicPointEntry(
            point_id=point_id, title='T', entity_type='switch',
            firmware_removed=True,
        )
        return m

    def test_point_in_bulk_has_flag_cleared(self):
        """Point marked firmware_removed that reappears in bulk → firmware_removed=False."""
        m = self._map_with_removed(100)
        m.restore_from_bulk({100, 200})
        self.assertFalse(m._table[100].firmware_removed)

    def test_point_not_in_bulk_stays_removed(self):
        """Point still absent from bulk → firmware_removed stays True."""
        m = self._map_with_removed(100)
        m.restore_from_bulk({200, 300})
        self.assertTrue(m._table[100].firmware_removed)

    def test_non_removed_point_unaffected(self):
        """Point with firmware_removed=False not in bulk → stays False."""
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[100] = DynamicPointEntry(
            point_id=100, title='T', entity_type='switch',
            firmware_removed=False,
        )
        m.restore_from_bulk(set())   # not in bulk
        self.assertFalse(m._table[100].firmware_removed)
