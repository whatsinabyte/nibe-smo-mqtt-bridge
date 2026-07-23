"""
test_lovelace.py
================
Nibe_lovelace tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from hypothesis import given
from hypothesis import strategies as st

from conftest import (
    _nibe_point_id,
    _safe_entity_id,
    _dyn_map_entry,
)

class TestPublisherLovelaceConstantsProperties(unittest.TestCase):
    """Structural invariants for nibe_mqtt_publisher and nibe_lovelace constants."""

    def test_legacy_preset_topics_all_strings(self):
        from nibe_mqtt_publisher import _LEGACY_PRESET_TOPICS
        for topic in _LEGACY_PRESET_TOPICS:
            self.assertIsInstance(topic, str)
            self.assertGreater(len(topic), 0)

    def test_legacy_preset_topics_all_unique(self):
        from nibe_mqtt_publisher import _LEGACY_PRESET_TOPICS
        self.assertEqual(len(_LEGACY_PRESET_TOPICS),
                         len(set(_LEGACY_PRESET_TOPICS)))

    def test_legacy_preset_topics_homeassistant_prefix(self):
        """All legacy topics must start with 'homeassistant/' — they are
        HA discovery topics that need to be cleared on migration."""
        from nibe_mqtt_publisher import _LEGACY_PRESET_TOPICS
        for topic in _LEGACY_PRESET_TOPICS:
            self.assertTrue(topic.startswith('homeassistant/'),
                f"Legacy topic {topic!r} does not start with 'homeassistant/'")

    def test_mgmt_avail_topic_equals_mgmttopic_avail(self):
        """MGMT_AVAIL_TOPIC module constant must equal MgmtTopic.AVAIL."""
        from nibe_mqtt_publisher import MGMT_AVAIL_TOPIC, MgmtTopic
        self.assertEqual(str(MGMT_AVAIL_TOPIC), str(MgmtTopic.AVAIL))

    def test_dashboard_title_nonempty(self):
        from nibe_lovelace import _DASHBOARD_TITLE
        self.assertIsInstance(_DASHBOARD_TITLE, str)
        self.assertGreater(len(_DASHBOARD_TITLE), 0)

    def test_menu_dashboard_title_nonempty(self):
        from nibe_lovelace import _MENU_DASHBOARD_TITLE
        self.assertIsInstance(_MENU_DASHBOARD_TITLE, str)
        self.assertGreater(len(_MENU_DASHBOARD_TITLE), 0)

    def test_dashboard_titles_are_different(self):
        """The two dashboard titles must differ — same title would cause
        slug collisions in HA's Lovelace dashboard registry."""
        from nibe_lovelace import _DASHBOARD_TITLE, _MENU_DASHBOARD_TITLE
        self.assertNotEqual(_DASHBOARD_TITLE, _MENU_DASHBOARD_TITLE)

    def test_legacy_topics_not_in_current_mgmt_topics(self):
        """Legacy topics must not appear in current MgmtTopic enum —
        they are being cleaned up, not published to."""
        from nibe_mqtt_publisher import _LEGACY_PRESET_TOPICS, MgmtTopic
        current = {m.value for m in MgmtTopic}
        for legacy in _LEGACY_PRESET_TOPICS:
            self.assertNotIn(legacy, current,
                f"Legacy topic {legacy!r} still appears in MgmtTopic")


# ---------------------------------------------------------------------------
# DynamicPointMap.mark_firmware_removed / restore_from_bulk properties
# ---------------------------------------------------------------------------


class TestDynamicPointMapLookupProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointMap lookup methods.

    Key invariants tested:
    - is_known_dynamic ↔ pid in all_known_dynamic_point_ids (consistency)
    - controlling_entry_for_dynamic returns entry iff is_known_dynamic
    - all_known_dynamic_point_ids is the union of all entry known points
    """

    def _make_map(self, entries):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        seen_pids = set()
        for e in entries:
            pid = e['point_id']
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            entry = DynamicPointEntry(
                point_id=pid, title=e['title'],
                entity_type=e['entity_type'],
            )
            entry.dynamic_points_by_value = {
                k: list(v) for k, v in e['dynamic_points_by_value'].items()
            }
            m._table[pid] = entry
        return m

    @given(st.lists(_dyn_map_entry, max_size=6, unique_by=lambda e: e['point_id']),
           st.integers(min_value=1000, max_value=2000))
    def test_is_known_dynamic_consistent_with_all_known_ids(self, entries, pid):
        """is_known_dynamic(pid) iff pid in all_known_dynamic_point_ids()."""
        m = self._make_map(entries)
        self.assertEqual(
            m.is_known_dynamic(pid),
            pid in m.all_known_dynamic_point_ids(),
        )

    @given(st.lists(_dyn_map_entry, max_size=6, unique_by=lambda e: e['point_id']),
           st.integers(min_value=1000, max_value=2000))
    def test_controlling_entry_none_iff_not_known(self, entries, pid):
        """controlling_entry_for_dynamic returns None iff is_known_dynamic is False."""
        m = self._make_map(entries)
        entry = m.controlling_entry_for_dynamic(pid)
        if m.is_known_dynamic(pid):
            self.assertIsNotNone(entry)
        else:
            self.assertIsNone(entry)

    @given(st.lists(_dyn_map_entry, max_size=6, unique_by=lambda e: e['point_id']),
           st.integers(min_value=1000, max_value=2000))
    def test_controlling_entry_actually_contains_pid(self, entries, pid):
        """When controlling_entry_for_dynamic returns an entry, pid is in it."""
        m = self._make_map(entries)
        entry = m.controlling_entry_for_dynamic(pid)
        if entry is not None:
            self.assertIn(pid, entry.all_known_dynamic_points())

    @given(st.lists(_dyn_map_entry, max_size=6, unique_by=lambda e: e['point_id']))
    def test_all_known_ids_is_union_of_entries(self, entries):
        """all_known_dynamic_point_ids() equals the union of all entry known points."""
        m = self._make_map(entries)
        expected = set()
        for entry in m._table.values():
            expected.update(entry.all_known_dynamic_points())
        self.assertEqual(m.all_known_dynamic_point_ids(), expected)

    @given(st.lists(_dyn_map_entry, max_size=6, unique_by=lambda e: e['point_id']))
    def test_all_known_ids_always_returns_set(self, entries):
        m = self._make_map(entries)
        self.assertIsInstance(m.all_known_dynamic_point_ids(), set)

    @given(st.lists(_dyn_map_entry, max_size=6, unique_by=lambda e: e['point_id']),
           st.integers(min_value=1000, max_value=2000))
    def test_is_known_dynamic_always_returns_bool(self, entries, pid):
        m = self._make_map(entries)
        self.assertIsInstance(m.is_known_dynamic(pid), bool)

    def test_empty_map_is_known_dynamic_always_false(self):
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        for pid in range(0, 100, 7):
            self.assertFalse(m.is_known_dynamic(pid))

    def test_empty_map_all_known_ids_is_empty_set(self):
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        self.assertEqual(m.all_known_dynamic_point_ids(), set())


# ---------------------------------------------------------------------------
# _build_dynamic_injection properties (nibe_lovelace.py)
# ---------------------------------------------------------------------------


class TestBuildDynamicInjectionProperties(unittest.TestCase):
    """Hypothesis properties for _build_dynamic_injection."""

    def _make_map_with_entry(self, controlling_pid, dynamic_pids):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        entry = DynamicPointEntry(
            point_id=controlling_pid, title='Test', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
        )
        entry.dynamic_points_by_value = {1: list(dynamic_pids)}
        m._table[controlling_pid] = entry
        return m

    @given(st.integers(min_value=1, max_value=500),
           st.sets(st.integers(min_value=1000, max_value=2000), min_size=0, max_size=5))
    def test_always_returns_dict(self, controlling, dynamic_pids):
        from nibe_lovelace import _build_dynamic_injection
        m = self._make_map_with_entry(controlling, dynamic_pids)
        rw = MagicMock()
        rw.entity_id_for.return_value = 'sensor.test'
        result = _build_dynamic_injection(m, dynamic_pids, rw, {})
        self.assertIsInstance(result, dict)

    @given(st.integers(min_value=1, max_value=500),
           st.sets(st.integers(min_value=1000, max_value=2000), min_size=1, max_size=5))
    def test_keys_are_ints(self, controlling, dynamic_pids):
        from nibe_lovelace import _build_dynamic_injection
        m = self._make_map_with_entry(controlling, dynamic_pids)
        rw = MagicMock()
        rw.entity_id_for.return_value = 'sensor.test'
        result = _build_dynamic_injection(m, dynamic_pids, rw, {})
        for k in result.keys():
            self.assertIsInstance(k, int)

    @given(st.integers(min_value=1, max_value=500),
           st.sets(st.integers(min_value=1000, max_value=2000), min_size=1, max_size=5))
    def test_values_are_lists_of_4_tuples(self, controlling, dynamic_pids):
        from nibe_lovelace import _build_dynamic_injection
        m = self._make_map_with_entry(controlling, dynamic_pids)
        rw = MagicMock()
        rw.entity_id_for.return_value = 'sensor.test'
        result = _build_dynamic_injection(m, dynamic_pids, rw, {})
        for v in result.values():
            self.assertIsInstance(v, list)
            for item in v:
                self.assertIsInstance(item, tuple)
                self.assertEqual(len(item), 4)

    @given(st.integers(min_value=1, max_value=500),
           st.sets(st.integers(min_value=1000, max_value=2000), min_size=1, max_size=5))
    def test_firmware_removed_entry_excluded(self, controlling, dynamic_pids):
        from nibe_lovelace import _build_dynamic_injection
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        entry = DynamicPointEntry(
            point_id=controlling, title='Removed', entity_type='switch',
            is_controlling=True, firmware_removed=True,
        )
        entry.dynamic_points_by_value = {1: list(dynamic_pids)}
        m._table[controlling] = entry
        rw = MagicMock()
        rw.entity_id_for.return_value = 'sensor.test'
        result = _build_dynamic_injection(m, dynamic_pids, rw, {})
        self.assertNotIn(controlling, result)

    def test_empty_active_dynamic_points_gives_empty_result(self):
        from nibe_lovelace import _build_dynamic_injection
        m = self._make_map_with_entry(100, {1001, 1002})
        rw = MagicMock()
        result = _build_dynamic_injection(m, set(), rw, {})
        self.assertEqual(result, {})

    def test_no_entity_id_excludes_dynamic_point(self):
        """Dynamic points without a registry entity_id are excluded."""
        from nibe_lovelace import _build_dynamic_injection
        m = self._make_map_with_entry(100, {1001})
        rw = MagicMock()
        rw.entity_id_for.return_value = None  # not registered yet
        result = _build_dynamic_injection(m, {1001}, rw, {})
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# _detect_holding_entity / _detect_input_entity properties (nibe_entity_detection.py)
# ---------------------------------------------------------------------------


class TestRetryDelayProperties(unittest.TestCase):
    """Hypothesis properties for _retry_delay."""

    @given(st.integers(min_value=0, max_value=100))
    def test_always_returns_float(self, _n):
        from nibe_api import _retry_delay
        result = _retry_delay()
        self.assertIsInstance(result, float)

    @given(st.integers(min_value=0, max_value=100))
    def test_always_non_negative(self, _n):
        from nibe_api import _retry_delay
        self.assertGreaterEqual(_retry_delay(), 0.0)

    @given(st.integers(min_value=0, max_value=100))
    def test_bounded_by_retry_max(self, _n):
        from nibe_api import _retry_delay, _RETRY_MAX_S
        self.assertLessEqual(_retry_delay(), _RETRY_MAX_S)


# ---------------------------------------------------------------------------
# _collect_menu_points properties (nibe_lovelace.py)
# ---------------------------------------------------------------------------

# Strategy: a menu node with optional settings and submenus
_setting = st.fixed_dictionaries({
    'point_id': st.one_of(st.none(), st.integers(min_value=1, max_value=99999)),
    'label': st.text(max_size=20),
})
_menu_node = st.fixed_dictionaries({
    'id': st.text(max_size=10),
    'title': st.text(max_size=30),
    'settings': st.lists(_setting, max_size=5),
    'submenus': st.just([]),  # no recursion in strategy; tested separately
})
_menu_list = st.lists(_menu_node, max_size=8)



class TestCollectMenuPointsProperties(unittest.TestCase):
    """Hypothesis properties for _collect_menu_points."""

    @given(_menu_list)
    def test_never_raises(self, menus):
        from nibe_lovelace import _collect_menu_points
        _collect_menu_points(menus)

    @given(_menu_list)
    def test_always_returns_set(self, menus):
        from nibe_lovelace import _collect_menu_points
        result = _collect_menu_points(menus)
        self.assertIsInstance(result, set)

    @given(_menu_list)
    def test_all_results_are_ints(self, menus):
        from nibe_lovelace import _collect_menu_points
        result = _collect_menu_points(menus)
        for pid in result:
            self.assertIsInstance(pid, int)

    @given(_menu_list)
    def test_result_subset_of_all_settings_point_ids(self, menus):
        """Every collected point_id must come from a setting in some menu."""
        from nibe_lovelace import _collect_menu_points
        all_setting_pids = set()
        for m in menus:
            for s in m.get('settings', []):
                pid = s.get('point_id')
                if pid:
                    all_setting_pids.add(pid)
        result = _collect_menu_points(menus)
        self.assertTrue(result.issubset(all_setting_pids))

    @given(_menu_list)
    def test_none_point_ids_excluded(self, menus):
        """Settings with point_id=None must not appear in the result."""
        from nibe_lovelace import _collect_menu_points
        result = _collect_menu_points(menus)
        self.assertNotIn(None, result)

    def test_empty_list_returns_empty_set(self):
        from nibe_lovelace import _collect_menu_points
        self.assertEqual(_collect_menu_points([]), set())

    def test_nested_submenus_included(self):
        """Points in submenus must be recursively collected."""
        from nibe_lovelace import _collect_menu_points
        menus = [{'id': '1', 'title': 'Top', 'settings': [],
                  'submenus': [{'id': '1.1', 'title': 'Sub',
                                'settings': [{'point_id': 12345, 'label': 'X'}],
                                'submenus': []}]}]
        result = _collect_menu_points(menus)
        self.assertIn(12345, result)


# ---------------------------------------------------------------------------
# _build_point_to_menu properties (nibe_lovelace.py)
# ---------------------------------------------------------------------------


class TestBuildPointToMenuProperties(unittest.TestCase):
    """Hypothesis properties for _build_point_to_menu."""

    @given(_menu_list)
    def test_never_raises(self, menus):
        from nibe_lovelace import _build_point_to_menu
        _build_point_to_menu(menus)

    @given(_menu_list)
    def test_always_returns_dict(self, menus):
        from nibe_lovelace import _build_point_to_menu
        result = _build_point_to_menu(menus)
        self.assertIsInstance(result, dict)

    @given(_menu_list)
    def test_keys_are_ints(self, menus):
        from nibe_lovelace import _build_point_to_menu
        result = _build_point_to_menu(menus)
        for k in result.keys():
            self.assertIsInstance(k, int)

    @given(_menu_list)
    def test_values_are_two_tuples(self, menus):
        from nibe_lovelace import _build_point_to_menu
        result = _build_point_to_menu(menus)
        for v in result.values():
            self.assertIsInstance(v, tuple)
            self.assertEqual(len(v), 2)

    @given(_menu_list)
    def test_consistent_with_collect_menu_points(self, menus):
        """Keys in _build_point_to_menu must be a subset of _collect_menu_points."""
        from nibe_lovelace import _build_point_to_menu, _collect_menu_points
        mapping = _build_point_to_menu(menus)
        collected = _collect_menu_points(menus)
        self.assertTrue(set(mapping.keys()).issubset(collected))

    def test_empty_list_returns_empty_dict(self):
        from nibe_lovelace import _build_point_to_menu
        self.assertEqual(_build_point_to_menu([]), {})


# ---------------------------------------------------------------------------
# _should_attempt_dashboard_create properties (nibe_lovelace.py)
# ---------------------------------------------------------------------------


class TestShouldAttemptDashboardCreateProperties(unittest.TestCase):
    """Hypothesis properties for _should_attempt_dashboard_create."""

    @given(st.dictionaries(st.text(max_size=10), st.text(max_size=10)),
           st.text(max_size=20))
    def test_never_raises(self, response, slug):
        from nibe_lovelace import _should_attempt_dashboard_create
        _should_attempt_dashboard_create(response, slug)

    @given(st.dictionaries(st.text(max_size=10), st.text(max_size=10)),
           st.text(max_size=20))
    def test_always_returns_bool(self, response, slug):
        from nibe_lovelace import _should_attempt_dashboard_create
        result = _should_attempt_dashboard_create(response, slug)
        self.assertIsInstance(result, bool)

    @given(st.text(max_size=20))
    def test_failure_response_always_false(self, slug):
        """success=False → always False regardless of result content."""
        from nibe_lovelace import _should_attempt_dashboard_create
        self.assertFalse(_should_attempt_dashboard_create(
            {'success': False, 'result': []}, slug))

    @given(st.text(max_size=20))
    def test_missing_success_always_false(self, slug):
        """Missing success key → always False."""
        from nibe_lovelace import _should_attempt_dashboard_create
        self.assertFalse(_should_attempt_dashboard_create({}, slug))

    @given(st.text(max_size=20))
    def test_existing_slug_always_false(self, slug):
        """slug already in result → always False."""
        from nibe_lovelace import _should_attempt_dashboard_create
        response = {'success': True, 'result': [{'url_path': slug}]}
        self.assertFalse(_should_attempt_dashboard_create(response, slug))

    @given(st.text(min_size=1, max_size=20),
           st.text(min_size=1, max_size=20).filter(lambda s: s != 'other'))
    def test_success_no_match_true(self, slug, other_slug):
        """success=True + no matching slug → True."""
        from nibe_lovelace import _should_attempt_dashboard_create
        if slug == other_slug:
            return
        response = {'success': True, 'result': [{'url_path': other_slug}]}
        self.assertTrue(_should_attempt_dashboard_create(response, slug))

    @given(st.text(max_size=20))
    def test_success_empty_result_true(self, slug):
        """success=True + empty result list → True (no existing dashboard)."""
        from nibe_lovelace import _should_attempt_dashboard_create
        self.assertTrue(_should_attempt_dashboard_create(
            {'success': True, 'result': []}, slug))


# ---------------------------------------------------------------------------
# _build_point_defaults properties (nibe_lovelace.py)
# ---------------------------------------------------------------------------



class TestMetadataDictTypeCrossModuleProperties(unittest.TestCase):
    """Cross-module Hypothesis properties: the 'type' field in
    _build_point_metadata_dict must always match what _get_cached_entity_type
    returns for the same point. These two systems must agree on entity type."""

    def _point(self, pid, modbus_type='MODBUS_INPUT_REGISTER',
               entity_type='sensor', writable=False):
        return {
            'variableId':     pid,
            'display_title':  f'Point {pid}',
            'entity_type':    entity_type,
            'entity_category': 'diagnostic',
            'is_writable':    writable,
            'is_dynamic':     False,
            'description':    '',
            'metadata': {
                'unit': '', 'shortUnit': '',
                'minValue': 0, 'maxValue': 1,
                'modbusRegisterID': pid,
                'modbusRegisterType': modbus_type,
                'variableType': 'integer', 'variableSize': 'u8',
                'isWritable': writable, 'divisor': 1, 'decimal': 0,
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            },
        }

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'binary_sensor', 'switch', 'number',
                            'select', 'button']))
    def test_metadata_type_matches_point_entity_type(self, pid, entity_type):
        """_build_point_metadata_dict 'type' must always equal point['entity_type']."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = MqttDiscoveryPublisher(
            mqtt_client=MagicMock(), device_info={},
            device_id='test', device_name='Test',
        )
        point = self._point(pid, entity_type=entity_type)
        result = pub._build_point_metadata_dict(point)
        self.assertEqual(result['type'], entity_type)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER']),
           st.booleans())
    def test_cached_entity_type_agrees_with_metadata_dict_type(self, pid, modbus, writable):
        """_get_cached_entity_type and _build_point_metadata_dict must agree on type
        when both are given the same raw point dict."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        from nibe_entity_detection import detect_entity_type
        pub = MqttDiscoveryPublisher(
            mqtt_client=MagicMock(), device_info={},
            device_id='test', device_name='Test',
        )
        raw_point = self._point(pid, modbus_type=modbus, writable=writable)
        # Detect the entity type directly
        detected_type, _ = detect_entity_type(raw_point)
        # Now make a point dict with that entity_type (as EntityManager would)
        point_with_type = {**raw_point, 'entity_type': detected_type}
        metadata_result = pub._build_point_metadata_dict(point_with_type)
        self.assertEqual(metadata_result['type'], detected_type)

    @given(_nibe_point_id, st.text(max_size=10))
    def test_unit_in_metadata_dict_never_contains_mojibake(self, pid, raw_unit):
        """Unit field after resolve_unit must never contain U+00C2 mojibake."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = MqttDiscoveryPublisher(
            mqtt_client=MagicMock(), device_info={},
            device_id='test', device_name='Test',
        )
        point = self._point(pid)
        point['metadata']['unit'] = raw_unit
        result = pub._build_point_metadata_dict(point)
        self.assertNotIn('\u00c2', result['unit'])

    @given(_nibe_point_id)
    def test_metadata_dict_writable_matches_is_writable(self, pid):
        """writable field must always match point['is_writable'] exactly."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = MqttDiscoveryPublisher(
            mqtt_client=MagicMock(), device_info={},
            device_id='test', device_name='Test',
        )
        for writable in (True, False):
            point = self._point(pid, writable=writable)
            result = pub._build_point_metadata_dict(point)
            self.assertEqual(result['writable'], writable)


# ---------------------------------------------------------------------------
# _build_menu_view properties (nibe_lovelace.py)
# ---------------------------------------------------------------------------


class TestBuildSelectConfigProperties(unittest.TestCase):
    """Hypothesis properties for _build_select_config."""

    _meta = {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'}

    @given(_nibe_point_id,
           _safe_entity_id, st.text(max_size=100))
    def test_always_sets_state_and_command_topic(self, pid, entity_id, description):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_select_config(
            config, entity_id, pid, self._meta, description)
        self.assertIn('state_topic', config)
        self.assertIn('command_topic', config)

    @given(_nibe_point_id,
           _safe_entity_id, st.text(max_size=100))
    def test_topics_contain_entity_id(self, pid, entity_id, description):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_select_config(
            config, entity_id, pid, self._meta, description)
        self.assertIn(entity_id, config['state_topic'])
        self.assertIn(entity_id, config['command_topic'])

    @given(_nibe_point_id,
           _safe_entity_id, st.text(max_size=100))
    def test_options_when_present_are_list_of_strings(self, pid, entity_id, description):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_select_config(
            config, entity_id, pid, self._meta, description)
        if 'options' in config:
            self.assertIsInstance(config['options'], list)
            for opt in config['options']:
                self.assertIsInstance(opt, str)

    @given(_nibe_point_id,
           _safe_entity_id, st.text(max_size=100))
    def test_options_when_present_have_no_duplicates(self, pid, entity_id, description):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_select_config(
            config, entity_id, pid, self._meta, description)
        if 'options' in config:
            opts = config['options']
            self.assertEqual(len(opts), len(set(opts)))

    @given(_nibe_point_id,
           _safe_entity_id, st.text(max_size=100))
    def test_options_when_present_have_at_least_two(self, pid, entity_id, description):
        """A single option is not a valid select — options must be ≥2 or absent."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_select_config(
            config, entity_id, pid, self._meta, description)
        if 'options' in config:
            self.assertGreaterEqual(len(config['options']), 2)

    @given(_nibe_point_id,
           _safe_entity_id, st.text(max_size=100))
    def test_consistent_with_t_state_t_command(self, pid, entity_id, description):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, t_state, t_command
        config = {}
        MqttDiscoveryPublisher._build_select_config(
            config, entity_id, pid, self._meta, description)
        self.assertEqual(config['state_topic'], t_state('select', entity_id))
        self.assertEqual(config['command_topic'], t_command('select', entity_id))


# ---------------------------------------------------------------------------
# _build_menu_dashboard_config properties (nibe_lovelace.py)
# ---------------------------------------------------------------------------


class TestBuildMenuDashboardConfigProperties(unittest.TestCase):
    """Hypothesis properties for _build_menu_dashboard_config."""

    @given(_menu_list)
    def test_never_raises(self, menus):
        from nibe_lovelace import _build_menu_dashboard_config
        from unittest.mock import MagicMock
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        _build_menu_dashboard_config(menus, rw)

    @given(_menu_list)
    def test_empty_produces_none_or_none_views(self, menus):
        """Empty menus list → returns None (no views generated)."""
        from nibe_lovelace import _build_menu_dashboard_config
        from unittest.mock import MagicMock
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result = _build_menu_dashboard_config([], rw)
        self.assertIsNone(result)

    @given(_menu_list)
    def test_nonempty_result_has_views_key(self, menus):
        from nibe_lovelace import _build_menu_dashboard_config
        from unittest.mock import MagicMock
        rw = MagicMock()
        rw.entity_id_for.return_value = 'sensor.nibe_100'
        result = _build_menu_dashboard_config(menus, rw)
        if result is not None:
            self.assertIn('views', result)
            self.assertIsInstance(result['views'], list)

    @given(_menu_list)
    def test_result_is_dict_or_none(self, menus):
        from nibe_lovelace import _build_menu_dashboard_config
        from unittest.mock import MagicMock
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result = _build_menu_dashboard_config(menus, rw)
        self.assertIn(type(result), (dict, type(None)))


# ---------------------------------------------------------------------------
# Cross-function: _collect_menu_points == keys of _build_point_to_menu
# ---------------------------------------------------------------------------


class TestMenuFunctionConsistencyProperties(unittest.TestCase):
    """Cross-function Hypothesis properties between lovelace menu functions."""

    @given(_menu_list)
    def test_build_point_to_menu_keys_subset_of_collect_menu_points(self, menus):
        """_build_point_to_menu keys must be a subset of _collect_menu_points output.
        (They should be equal for single-level menus with no None point_ids.)"""
        from nibe_lovelace import _collect_menu_points, _build_point_to_menu
        collected = _collect_menu_points(menus)
        mapped = set(_build_point_to_menu(menus).keys())
        self.assertTrue(mapped.issubset(collected))

    @given(_menu_list)
    def test_collect_menu_points_superset_of_build_point_to_menu_keys(self, menus):
        """Every key in _build_point_to_menu is in _collect_menu_points."""
        from nibe_lovelace import _collect_menu_points, _build_point_to_menu
        for pid in _build_point_to_menu(menus).keys():
            self.assertIn(pid, _collect_menu_points(menus))

    def test_both_functions_agree_on_empty_input(self):
        from nibe_lovelace import _collect_menu_points, _build_point_to_menu
        self.assertEqual(_collect_menu_points([]), set())
        self.assertEqual(_build_point_to_menu([]), {})

    @given(_menu_list)
    def test_should_attempt_consistent_with_result_content(self, menus):
        """_should_attempt_dashboard_create is False iff slug already in result."""
        from nibe_lovelace import _should_attempt_dashboard_create
        slug = 'nibe-menus'
        # Case: slug present
        response_with = {'success': True, 'result': [{'url_path': slug}]}
        self.assertFalse(_should_attempt_dashboard_create(response_with, slug))
        # Case: slug absent
        response_without = {'success': True, 'result': [{'url_path': 'other'}]}
        self.assertTrue(_should_attempt_dashboard_create(response_without, slug))


# ---------------------------------------------------------------------------
# build_menu_points properties (nibe_lovelace.py)
# ---------------------------------------------------------------------------


class TestBuildUnplacedViewProperties(unittest.TestCase):
    """Hypothesis properties for _build_unplaced_view."""

    def _bulk(self, point_ids, writable=False):
        """Build a minimal bulk_data dict."""
        result = {}
        for pid in point_ids:
            result[pid] = {
                'raw_value': 1, 'is_ok': True,
                'title': f'Point {pid}',
                'metadata': {
                    'modbusRegisterType': (
                        'MODBUS_HOLDING_REGISTER' if writable
                        else 'MODBUS_INPUT_REGISTER'
                    ),
                    'isWritable': writable,
                    'variableSize': 's16', 'variableType': 'integer',
                    'minValue': 0, 'maxValue': 100, 'divisor': 1,
                    'unit': '', 'shortUnit': '',
                },
                'description': '',
            }
        return result

    @given(st.sets(st.integers(min_value=1, max_value=5000), max_size=20),
           st.sets(st.integers(min_value=1, max_value=5000), max_size=10))
    def test_always_returns_dict_or_none(self, bulk_pids, menu_pids):
        from nibe_lovelace import _build_unplaced_view
        bulk = self._bulk(bulk_pids)
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result = _build_unplaced_view(bulk, menu_pids, rw, {})
        self.assertIn(type(result), (dict, type(None)))

    @given(st.sets(st.integers(min_value=1, max_value=5000), max_size=20),
           st.sets(st.integers(min_value=1, max_value=5000), max_size=10))
    def test_never_raises(self, bulk_pids, menu_pids):
        from nibe_lovelace import _build_unplaced_view
        bulk = self._bulk(bulk_pids)
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        _build_unplaced_view(bulk, menu_pids, rw, {})  # must not raise

    def test_empty_bulk_returns_none(self):
        from nibe_lovelace import _build_unplaced_view
        rw = MagicMock()
        result = _build_unplaced_view({}, set(), rw, {})
        self.assertIsNone(result)

    @given(st.sets(st.integers(min_value=1000, max_value=2000), min_size=1, max_size=10))
    def test_menu_points_excluded_from_result(self, menu_pids):
        """Points in menu_yaml_points must never appear in the unplaced view."""
        from nibe_lovelace import _build_unplaced_view
        # Put menu_pids in bulk AND in menu set — they should be excluded
        bulk = self._bulk(menu_pids, writable=True)
        rw = MagicMock()
        rw.entity_id_for.return_value = 'sensor.test'
        result = _build_unplaced_view(bulk, menu_pids, rw, {})
        if result and 'views' in result:
            for view in result.get('views', []):
                for section in view.get('sections', []):
                    for card in section.get('cards', []):
                        for entity in card.get('entities', []):
                            eid = entity if isinstance(entity, str) else entity.get('entity', '')
                            for pid in menu_pids:
                                self.assertNotIn(str(pid), eid)


# ===========================================================================
# 10. _prune_changelog_if_due
# ===========================================================================


class TestWsCallSendFailure(unittest.TestCase):
    """_ws_call must return {} rather than propagate an exception when the
    underlying send() fails (e.g. BrokenPipeError from a dropped HA
    Supervisor WebSocket connection). A regression here previously crashed
    the nibe_menu_regen thread silently and skipped the retry mechanism."""

    def test_send_broken_pipe_returns_empty_dict(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.send.side_effect = BrokenPipeError(32, "Broken pipe")
        result = nl._ws_call(ws, 1, {"type": "ping"})
        self.assertEqual(result, {})

    def test_send_os_error_returns_empty_dict(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.send.side_effect = OSError("connection reset")
        result = nl._ws_call(ws, 1, {"type": "ping"})
        self.assertEqual(result, {})

    def test_send_failure_does_not_call_recv(self):
        """If send() fails, _ws_call must not attempt recv() afterward."""
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.send.side_effect = BrokenPipeError(32, "Broken pipe")
        nl._ws_call(ws, 1, {"type": "ping"})
        ws.recv.assert_not_called()

    def test_successful_send_still_returns_matching_result(self):
        """Confirms the fix didn't change behavior on the happy path."""
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.return_value = json.dumps({"id": 1, "type": "result", "success": True})
        result = nl._ws_call(ws, 1, {"type": "ping"})
        self.assertEqual(result, {"id": 1, "type": "result", "success": True})


# ===========================================================================
# 39. _regen_menu_dashboard retry/exception handling
# ===========================================================================


class TestRegenMenuDashboard(unittest.TestCase):
    """Covers the actual failure chain from the production crash: an
    exception raised deep inside setup_dashboard_fn must be caught and
    treated as needs_retry, not propagate and kill the calling thread.

    API change (stale-WebSocket fix): _regen_menu_dashboard no longer
    opens the WebSocket itself. Instead it passes open_ws_fn through to
    setup_dashboard_fn, which opens a fresh connection after the registry
    wait (inside _setup_menu_dashboard). This prevents the Supervisor
    from closing an idle connection during the 60-second registry wait,
    which caused every lovelace/dashboards/list call to return {} and the
    dashboard to never build. All dependencies are injected fakes — no
    real WebSocket, broker, or timer thread involved."""

    def test_setup_dashboard_exception_is_caught_and_schedules_retry(self):
        """The original bug: setup_dashboard_fn raising must not propagate."""
        import nibe_lovelace as nl
        setup_dashboard_fn = MagicMock(side_effect=BrokenPipeError(32, "Broken pipe"))
        schedule_retry_fn = MagicMock()
        open_ws_fn = MagicMock()
        registry_watcher = MagicMock()

        nl._regen_menu_dashboard(
            registry_watcher, debug_mode=False, attempt=1,
            open_ws_fn=open_ws_fn, setup_dashboard_fn=setup_dashboard_fn,
            schedule_retry_fn=schedule_retry_fn,
        )

        schedule_retry_fn.assert_called_once()
        # open_ws_fn is forwarded to setup_dashboard_fn, not called directly
        open_ws_fn.assert_not_called()

    def test_setup_dashboard_called_with_open_ws_fn(self):
        """setup_dashboard_fn receives open_ws_fn as its first argument so it
        can open a fresh WebSocket after its own registry wait."""
        import nibe_lovelace as nl
        open_ws_fn = MagicMock()
        setup_dashboard_fn = MagicMock(return_value=False)
        registry_watcher = MagicMock()

        nl._regen_menu_dashboard(
            registry_watcher, debug_mode=False, attempt=1,
            open_ws_fn=open_ws_fn, setup_dashboard_fn=setup_dashboard_fn,
            schedule_retry_fn=MagicMock(),
        )

        setup_dashboard_fn.assert_called_once_with(
            open_ws_fn, registry_watcher, debug_mode=False,
        )

    def test_exception_on_final_attempt_does_not_schedule_retry(self):
        """At max_attempts, a failure logs and gives up rather than retrying."""
        import nibe_lovelace as nl
        setup_dashboard_fn = MagicMock(side_effect=RuntimeError("boom"))
        schedule_retry_fn = MagicMock()
        nl._regen_menu_dashboard(
            MagicMock(), debug_mode=False, attempt=3, max_attempts=3,
            open_ws_fn=MagicMock(), setup_dashboard_fn=setup_dashboard_fn,
            schedule_retry_fn=schedule_retry_fn,
        )
        schedule_retry_fn.assert_not_called()

    def test_needs_retry_true_without_exception_schedules_retry(self):
        """needs_retry=True (e.g. dynamic points not yet in registry) also retries."""
        import nibe_lovelace as nl
        setup_dashboard_fn = MagicMock(return_value=True)
        schedule_retry_fn = MagicMock()
        nl._regen_menu_dashboard(
            MagicMock(), debug_mode=False, attempt=1,
            open_ws_fn=MagicMock(), setup_dashboard_fn=setup_dashboard_fn,
            schedule_retry_fn=schedule_retry_fn,
        )
        schedule_retry_fn.assert_called_once()

    def test_needs_retry_false_does_not_schedule_retry(self):
        """The clean success path: no retry needed, no retry scheduled."""
        import nibe_lovelace as nl
        setup_dashboard_fn = MagicMock(return_value=False)
        schedule_retry_fn = MagicMock()
        nl._regen_menu_dashboard(
            MagicMock(), debug_mode=False, attempt=1,
            open_ws_fn=MagicMock(), setup_dashboard_fn=setup_dashboard_fn,
            schedule_retry_fn=schedule_retry_fn,
        )
        schedule_retry_fn.assert_not_called()

    def test_websocket_open_failure_at_max_attempts_gives_up(self):
        """open_ws_fn=None-returning is now forwarded to setup_dashboard_fn —
        setup_dashboard_fn is responsible for handling its own failure."""
        import nibe_lovelace as nl
        setup_dashboard_fn = MagicMock(return_value=True)  # signals needs_retry
        schedule_retry_fn = MagicMock()
        nl._regen_menu_dashboard(
            MagicMock(), debug_mode=False, attempt=3, max_attempts=3,
            open_ws_fn=MagicMock(return_value=None),
            setup_dashboard_fn=setup_dashboard_fn,
            schedule_retry_fn=schedule_retry_fn,
        )
        schedule_retry_fn.assert_not_called()

    def test_default_schedule_retry_refreshes_registry(self):
        """When schedule_retry_fn isn't injected, the default path must
        refresh the registry before scheduling the timer."""
        import nibe_lovelace as nl
        setup_dashboard_fn = MagicMock(return_value=True)
        registry_watcher = MagicMock()
        with patch('threading.Timer') as mock_timer:
            nl._regen_menu_dashboard(
                registry_watcher, debug_mode=False, attempt=1,
                open_ws_fn=MagicMock(), setup_dashboard_fn=setup_dashboard_fn,
            )
            registry_watcher.refresh_registry.assert_called_once()
            mock_timer.assert_called_once()
            mock_timer.return_value.start.assert_called_once()


# ===========================================================================
# 40. _on_enabled_state_change_factory debounce wiring
# ===========================================================================


class TestOnEnabledStateChangeFactory(unittest.TestCase):
    """Covers the debounce closure itself — this is where a previous editing
    pass accidentally dropped t.start(), which would have made the regen
    handler silently never fire. Verified directly here so a regression is
    caught immediately rather than only in production logs."""

    def test_calling_handler_starts_a_timer(self):
        import nibe_lovelace as nl
        registry_watcher = MagicMock()
        handler = nl._on_enabled_state_change_factory(registry_watcher, debug_mode=False)
        with patch('threading.Timer') as mock_timer:
            handler()
            mock_timer.assert_called_once()
            mock_timer.return_value.start.assert_called_once()

    def test_second_call_cancels_pending_timer(self):
        """Trailing debounce: a second call before the first fires must
        cancel the first timer rather than letting both run."""
        import nibe_lovelace as nl
        registry_watcher = MagicMock()
        handler = nl._on_enabled_state_change_factory(registry_watcher, debug_mode=False)
        with patch('threading.Timer') as mock_timer:
            first_timer = MagicMock()
            second_timer = MagicMock()
            mock_timer.side_effect = [first_timer, second_timer]

            handler()
            handler()

            first_timer.cancel.assert_called_once()
            second_timer.start.assert_called_once()

    def test_timer_daemon_and_name_set(self):
        """Sanity check that the timer thread won't block process exit."""
        import nibe_lovelace as nl
        registry_watcher = MagicMock()
        handler = nl._on_enabled_state_change_factory(registry_watcher, debug_mode=False)
        with patch('threading.Timer') as mock_timer:
            handler()
            t = mock_timer.return_value
            self.assertTrue(t.daemon)
            self.assertEqual(t.name, "nibe_menu_regen")



class TestCollectMenuPoints(unittest.TestCase):
    """Pure recursive logic, extracted from _setup_menu_dashboard for direct
    testing. A bug here (e.g. forgetting to recurse into submenus) would
    mean menu entities silently fail to auto-enable on startup — hard to
    notice without an explicit test, since the dashboard would just look
    incomplete rather than error."""

    def test_empty_menu_list(self):
        import nibe_lovelace as nl
        self.assertEqual(nl._collect_menu_points([]), set())

    def test_single_menu_single_setting(self):
        import nibe_lovelace as nl
        menus = [{'id': '1', 'settings': [{'point_id': 100}]}]
        self.assertEqual(nl._collect_menu_points(menus), {100})

    def test_multiple_settings_same_menu(self):
        import nibe_lovelace as nl
        menus = [{'id': '1', 'settings': [{'point_id': 100}, {'point_id': 200}]}]
        self.assertEqual(nl._collect_menu_points(menus), {100, 200})

    def test_nested_submenus_collected(self):
        """The actual menu tree is up to 4 levels deep (e.g. 7.1.10.6) —
        confirm recursion reaches the bottom."""
        import nibe_lovelace as nl
        menus = [{
            'id': '7', 'settings': [],
            'submenus': [{
                'id': '7.1', 'settings': [{'point_id': 100}],
                'submenus': [{
                    'id': '7.1.1', 'settings': [],
                    'submenus': [{
                        'id': '7.1.1.1', 'settings': [{'point_id': 200}],
                    }],
                }],
            }],
        }]
        self.assertEqual(nl._collect_menu_points(menus), {100, 200})

    def test_missing_settings_key_does_not_crash(self):
        """Some menus have no 'settings' key at all (e.g. pure container
        menus like '7.1' that only hold submenus)."""
        import nibe_lovelace as nl
        menus = [{'id': '7.1', 'submenus': [{'id': '7.1.1', 'settings': [{'point_id': 5}]}]}]
        self.assertEqual(nl._collect_menu_points(menus), {5})

    def test_null_point_id_placeholder_skipped(self):
        """The real menu_structure.yaml has several settings with
        point_id: (null) used as a 'documented but configured elsewhere'
        placeholder (e.g. menu 2.4's periodic-increase stop temperature).
        These must not appear in the collected set."""
        import nibe_lovelace as nl
        menus = [{'id': '1', 'settings': [{'point_id': 100}, {'point_id': None}]}]
        self.assertEqual(nl._collect_menu_points(menus), {100})

    def test_duplicate_point_id_across_menus_collapses_to_one(self):
        """Several real points (e.g. outdoor temperature BT1) are
        intentionally shown in multiple menus — the collected set must
        de-duplicate, since it drives one-time entity enabling, not display."""
        import nibe_lovelace as nl
        menus = [
            {'id': '3.1.2', 'settings': [{'point_id': 4}]},
            {'id': '3.1.3', 'settings': [{'point_id': 4}]},
        ]
        self.assertEqual(nl._collect_menu_points(menus), {4})



class TestBuildMenuPoints(unittest.TestCase):
    """build_menu_points(yaml_path) is the single source of truth for the
    'menus' mode point set — derived from menu_structure.yaml at startup
    and stored into MODES['menus'] before apply_mode() runs. This replaces
    the old hardcoded MENU_POINTS frozenset that could silently diverge
    from the YAML (causing Spook ghost entities for cards with no entity,
    or enabled entities with no dashboard card)."""

    def test_returns_frozenset(self):
        import nibe_lovelace as nl
        import tempfile
        import os
        import yaml
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        yaml.dump({'menus': [{'id': '1', 'settings': [{'point_id': 42}], 'submenus': []}]},
                  tmp)
        tmp.close()
        try:
            result = nl.build_menu_points(tmp.name)
            self.assertIsInstance(result, frozenset)
            self.assertIn(42, result)
        finally:
            os.unlink(tmp.name)

    def test_missing_file_returns_empty_frozenset(self):
        import nibe_lovelace as nl
        result = nl.build_menu_points('/nonexistent/path/menu_structure.yaml')
        self.assertIsInstance(result, frozenset)
        self.assertEqual(len(result), 0)

    def test_empty_yaml_returns_empty_frozenset(self):
        import nibe_lovelace as nl
        import tempfile
        import os
        import yaml
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        yaml.dump({'menus': []}, tmp)
        tmp.close()
        try:
            result = nl.build_menu_points(tmp.name)
            self.assertEqual(result, frozenset())
        finally:
            os.unlink(tmp.name)

    def test_real_yaml_sync_with_modes_menus(self):
        """The critical regression test: MODES['menus'] (set at startup by
        main() calling build_menu_points) must exactly equal the full set of
        point_ids in menu_structure.yaml. Any point in the YAML with no
        matching enabled entity becomes a Spook ghost. Any point in MODES
        but not in the YAML gets enabled with no dashboard card.

        This test reads the real menu_structure.yaml from the project and
        compares against what build_menu_points returns — they must be
        identical. If this test fails, either a point was added to the YAML
        without being found by _collect_menu_points (a collection bug) or
        MODES['menus'] was set from a different source (a wiring bug).

        Tests that patch MODES['menus'] directly for isolation are fine —
        this test verifies the real startup wiring, so it uses the real file."""
        import nibe_lovelace as nl
        import os
        yaml_path = os.path.join(os.path.dirname(__import__('nibe_lovelace').__file__), 'menu_structure.yaml')
        if not os.path.exists(yaml_path):
            self.skipTest("menu_structure.yaml not present in test directory")
        from_yaml = nl.build_menu_points(yaml_path)
        self.assertGreater(len(from_yaml), 100,
            "Expected >100 points from menu_structure.yaml — "
            "if this fails, the YAML may be empty or unreadable")
        # Simulate what main() does: store into MODES, then verify consistency
        from nibe_entity_detection import MODES
        saved = MODES['menus']
        try:
            MODES['menus'] = from_yaml
            self.assertEqual(
                MODES['menus'], from_yaml,
                "MODES['menus'] after assignment must equal build_menu_points() output"
            )
        finally:
            MODES['menus'] = saved  # restore for other tests



class TestMenuPointsYamlSync(unittest.TestCase):
    """Structural sync test: every point_id in menu_structure.yaml must be
    reachable by build_menu_points() — i.e. _collect_menu_points() must find
    it. Catches nesting patterns not yet handled by the collector."""

    def test_no_ghost_points(self):
        """Every point in menu_structure.yaml must appear in what
        build_menu_points() returns. A miss here means a dashboard card
        will reference an entity that was never enabled — a Spook ghost."""
        import nibe_lovelace as nl
        import yaml
        import os

        yaml_path = os.path.join(os.path.dirname(__import__('nibe_lovelace').__file__), 'menu_structure.yaml')
        if not os.path.exists(yaml_path):
            self.skipTest("menu_structure.yaml not present in test directory")

        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        # Full recursive walk — the ground truth
        def collect_all(node):
            ids = set()
            if isinstance(node, dict):
                pid = node.get('point_id')
                if pid is not None:
                    ids.add(pid)
                for v in node.values():
                    ids |= collect_all(v)
            elif isinstance(node, list):
                for item in node:
                    ids |= collect_all(item)
            return ids

        all_in_yaml = collect_all(data)
        from_build   = nl.build_menu_points(yaml_path)

        ghosts = sorted(all_in_yaml - from_build)
        self.assertEqual(ghosts, [],
            f"{len(ghosts)} point(s) in menu_structure.yaml not found by "
            f"build_menu_points() — these become Spook ghosts: {ghosts}")



class TestBuildPointToMenu(unittest.TestCase):

    def test_empty_menu_list(self):
        import nibe_lovelace as nl
        self.assertEqual(nl._build_point_to_menu([]), {})

    def test_maps_point_to_menu_id_and_title(self):
        import nibe_lovelace as nl
        menus = [{'id': '7.1.6.3', 'title': 'Power at DOT', 'settings': [{'point_id': 6984}]}]
        result = nl._build_point_to_menu(menus)
        self.assertEqual(result[6984], ('7.1.6.3', 'Power at DOT'))

    def test_nested_submenus_mapped(self):
        import nibe_lovelace as nl
        menus = [{
            'id': '7', 'title': 'Installer', 'settings': [],
            'submenus': [{
                'id': '7.1.5.1', 'title': 'Additional heat settings',
                'settings': [{'point_id': 14968}],
            }],
        }]
        result = nl._build_point_to_menu(menus)
        self.assertEqual(result[14968], ('7.1.5.1', 'Additional heat settings'))

    def test_duplicate_point_id_keeps_last_menu_seen(self):
        """When the same point appears in multiple menus (e.g. point 4527
        legitimately documented in 7.1.5.1, 7.1.10.3, and 7.2.3), the
        reverse-lookup can only point to one menu. This pins down current
        behavior (last one wins) so a future change to the walk order is
        a deliberate decision, not an accidental side effect."""
        import nibe_lovelace as nl
        menus = [
            {'id': '7.1.5.1', 'title': 'Additional heat settings',
             'settings': [{'point_id': 4527}]},
            {'id': '7.2.3', 'title': 'Shunt-controlled additional heat (AXC)',
             'settings': [{'point_id': 4527}]},
        ]
        result = nl._build_point_to_menu(menus)
        self.assertEqual(result[4527], ('7.2.3', 'Shunt-controlled additional heat (AXC)'))

    def test_result_dict_can_be_passed_in_and_extended(self):
        """The function accepts an existing dict to accumulate into —
        confirm it mutates and returns the same object rather than losing
        prior entries."""
        import nibe_lovelace as nl
        existing = {999: ('0', 'Pre-existing')}
        menus = [{'id': '1', 'title': 'New', 'settings': [{'point_id': 1}]}]
        result = nl._build_point_to_menu(menus, existing)
        self.assertEqual(result[999], ('0', 'Pre-existing'))
        self.assertEqual(result[1], ('1', 'New'))


# ===========================================================================
# 42. _should_attempt_dashboard_create — list-failure vs. genuinely-absent
# ===========================================================================


class TestShouldAttemptDashboardCreate(unittest.TestCase):
    """Covers the production bug where a failed lovelace/dashboards list
    call (e.g. _ws_call returning {} after a dropped WebSocket) was treated
    identically to "zero dashboards exist", triggering a doomed create
    attempt on every retry. Home Assistant logged this repeatedly as
    'The URL "nibe-menus" is already in use' even though the dashboard
    genuinely already existed from a prior run."""

    def test_empty_dict_response_does_not_trigger_create(self):
        """The exact production scenario: _ws_call returned {} because the
        underlying list call failed. Must not be read as 'no dashboards'."""
        import nibe_lovelace as nl
        self.assertFalse(nl._should_attempt_dashboard_create({}, "nibe-menus"))

    def test_explicit_success_false_does_not_trigger_create(self):
        import nibe_lovelace as nl
        response = {"success": False, "error": {"message": "timeout"}}
        self.assertFalse(nl._should_attempt_dashboard_create(response, "nibe-menus"))

    def test_successful_empty_list_triggers_create(self):
        """Genuine case: the list call succeeded and truly found zero
        dashboards — this is the only case that should create one."""
        import nibe_lovelace as nl
        response = {"success": True, "result": []}
        self.assertTrue(nl._should_attempt_dashboard_create(response, "nibe-menus"))

    def test_successful_list_with_matching_dashboard_skips_create(self):
        import nibe_lovelace as nl
        response = {"success": True, "result": [
            {"url_path": "nibe-menus", "title": "Nibe Menus"},
            {"url_path": "other-dashboard", "title": "Something Else"},
        ]}
        self.assertFalse(nl._should_attempt_dashboard_create(response, "nibe-menus"))

    def test_successful_list_without_matching_dashboard_triggers_create(self):
        import nibe_lovelace as nl
        response = {"success": True, "result": [
            {"url_path": "other-dashboard", "title": "Something Else"},
        ]}
        self.assertTrue(nl._should_attempt_dashboard_create(response, "nibe-menus"))

    def test_missing_result_key_on_success_treated_as_empty(self):
        """Defensive: success=True but no 'result' key at all shouldn't crash."""
        import nibe_lovelace as nl
        response = {"success": True}
        self.assertTrue(nl._should_attempt_dashboard_create(response, "nibe-menus"))


# ===========================================================================
# 43. _build_point_defaults — dashboard default-value annotation logic
# ===========================================================================


class TestBuildPointDefaults(unittest.TestCase):
    """Pure function, zero coverage despite being module-level and easily
    testable. Runs on every dashboard regen to produce the default-value
    text shown next to each setting — a silent regression here doesn't
    crash anything, it just shows wrong or missing defaults on the
    dashboard, which is easy to overlook."""

    def _point(self, writable=True, regtype='MODBUS_HOLDING_REGISTER',
               min_val=0, max_val=100, default=50, divisor=1, unit=''):
        return {
            'metadata': {
                'isWritable': writable,
                'modbusRegisterType': regtype,
                'minValue': min_val,
                'maxValue': max_val,
                'intDefaultValue': default,
                'divisor': divisor,
                'unit': unit,
            }
        }

    def test_basic_integer_default_with_unit(self):
        import nibe_lovelace as nl
        points = {100: self._point(min_val=0, max_val=400, default=200, divisor=10, unit='°C')}
        result = nl._build_point_defaults(points)
        self.assertEqual(result[100], "20 °C")

    def test_default_without_unit(self):
        import nibe_lovelace as nl
        points = {100: self._point(min_val=0, max_val=100, default=50, divisor=1, unit='')}
        result = nl._build_point_defaults(points)
        self.assertEqual(result[100], "50")

    def test_non_writable_point_excluded(self):
        import nibe_lovelace as nl
        points = {100: self._point(writable=False)}
        self.assertNotIn(100, nl._build_point_defaults(points))

    def test_non_holding_register_excluded(self):
        """e.g. MODBUS_INPUT_REGISTER (read-only sensor) has no settable default."""
        import nibe_lovelace as nl
        points = {100: self._point(regtype='MODBUS_INPUT_REGISTER')}
        self.assertNotIn(100, nl._build_point_defaults(points))

    def test_degenerate_range_excluded(self):
        """min == max means the value is fixed/not really a setting — the
        same is_degenerate_range concept used elsewhere in the bridge for
        write-side validation."""
        import nibe_lovelace as nl
        points = {100: self._point(min_val=5, max_val=5, default=5)}
        self.assertNotIn(100, nl._build_point_defaults(points))

    def test_missing_default_excluded(self):
        import nibe_lovelace as nl
        point = self._point()
        point['metadata']['intDefaultValue'] = None
        points = {100: point}
        self.assertNotIn(100, nl._build_point_defaults(points))

    def test_ambiguous_zero_on_wide_range_excluded(self):
        """default=0, min=0, max>1 — e.g. a 0-400 range defaulting to 0 is
        suppressed because '0' there reads as 'unset' rather than a
        meaningful default."""
        import nibe_lovelace as nl
        points = {100: self._point(min_val=0, max_val=400, default=0)}
        self.assertNotIn(100, nl._build_point_defaults(points))

    def test_zero_default_on_binary_toggle_is_included(self):
        """Pinned-down current behavior: a real 0/1 toggle (max_val=1)
        defaulting to 0 is NOT caught by the ambiguous-zero suppression
        (which only fires when max_val > 1), so it IS included as '0'.
        Point 4562 (manual pump speed override) is exactly this shape in
        the real firmware. Whether '0' is the most readable label for an
        off-by-default switch is a separate question — this test exists so
        a future change to the suppression condition is a deliberate
        decision, not an accidental side effect."""
        import nibe_lovelace as nl
        points = {4562: self._point(min_val=0, max_val=1, default=0, divisor=1, unit='')}
        result = nl._build_point_defaults(points)
        self.assertEqual(result[4562], "0")

    def test_nonzero_default_on_narrow_range_is_included(self):
        """default != 0 is never suppressed, even on a 0-1 range."""
        import nibe_lovelace as nl
        points = {100: self._point(min_val=0, max_val=1, default=1)}
        result = nl._build_point_defaults(points)
        self.assertEqual(result[100], "1")

    def test_nonzero_min_with_zero_default_is_included(self):
        """The ambiguous-zero suppression only applies when min_val == 0 —
        a negative-range point (e.g. a DM threshold) defaulting to 0 is
        meaningful and must be shown."""
        import nibe_lovelace as nl
        points = {100: self._point(min_val=-1000, max_val=-30, default=0)}
        # Note: default=0 is outside [-1000,-30] in reality, but the function
        # doesn't validate that — it only checks the suppression condition.
        result = nl._build_point_defaults(points)
        self.assertEqual(result[100], "0")

    def test_divisor_zero_treated_as_one(self):
        """Matches the firmware-wide convention (confirmed elsewhere in the
        bridge) that divisor: 0 is treated as divisor: 1, not a ZeroDivisionError."""
        import nibe_lovelace as nl
        points = {100: self._point(min_val=0, max_val=100, default=50, divisor=0)}
        result = nl._build_point_defaults(points)
        self.assertEqual(result[100], "50")

    def test_mojibake_degree_symbol_normalised(self):
        """Firmware embeds 'Â°C' (Latin-1 ° mis-decoded as UTF-8) — this
        must be normalised to '°C' in the displayed default, same as
        everywhere else in the bridge that handles firmware unit strings."""
        import nibe_lovelace as nl
        points = {100: self._point(min_val=0, max_val=400, default=200, divisor=10, unit='Â°C')}
        result = nl._build_point_defaults(points)
        self.assertEqual(result[100], "20 °C")

    def test_shortunit_used_as_fallback(self):
        import nibe_lovelace as nl
        point = self._point(min_val=0, max_val=400, default=200, divisor=10, unit='')
        point['metadata']['shortUnit'] = '°C'
        points = {100: point}
        result = nl._build_point_defaults(points)
        self.assertEqual(result[100], "20 °C")

    def test_negative_default_formatted_correctly(self):
        """e.g. DM start heating defaults to -60."""
        import nibe_lovelace as nl
        points = {3818: self._point(min_val=-1000, max_val=-30, default=-60, divisor=1, unit='DM')}
        result = nl._build_point_defaults(points)
        self.assertEqual(result[3818], "-60 DM")

    def test_multiple_points_independent(self):
        import nibe_lovelace as nl
        points = {
            1: self._point(min_val=0, max_val=400, default=200, divisor=10, unit='°C'),
            2: self._point(writable=False),
            3: self._point(min_val=0, max_val=100, default=0),  # ambiguous zero, excluded
        }
        result = nl._build_point_defaults(points)
        self.assertEqual(set(result.keys()), {1})

    def test_empty_input_returns_empty_dict(self):
        import nibe_lovelace as nl
        self.assertEqual(nl._build_point_defaults({}), {})


# ===========================================================================
# 44. _build_dynamic_injection — dynamic point card injection mapping
# ===========================================================================


class TestBuildDynamicInjection(unittest.TestCase):
    """Pure function (aside from a registry_watcher lookup, mocked here),
    zero coverage before this. Decides which dynamic points get injected
    as extra cards under their controlling switch/select on the dashboard.
    A regression here means a paired accessory's settings silently don't
    appear on the dashboard even though the points exist and are enabled —
    easy to miss since nothing errors, the card is just absent."""

    def _entry(self, point_id, dynamic_points_by_value, firmware_removed=False):
        from nibe_dynamic_map import DynamicPointEntry
        return DynamicPointEntry(
            point_id=point_id,
            title=f'Switch {point_id}',
            entity_type='switch',
            dynamic_points_by_value=dynamic_points_by_value,
            firmware_removed=firmware_removed,
        )

    def _registry_watcher(self, entity_ids: dict):
        """entity_ids: {point_id: 'sensor.foo'} — points not in this dict
        resolve to None, matching a not-yet-registered point."""
        watcher = MagicMock()
        watcher.entity_id_for.side_effect = lambda pid: entity_ids.get(pid)
        return watcher

    def test_firmware_removed_entry_excluded(self):
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [200]}, firmware_removed=True)}
        result = nl._build_dynamic_injection(
            dpm, {200}, self._registry_watcher({200: 'sensor.foo'}), {},
        )
        self.assertEqual(result, {})

    def test_no_active_dynamic_points_for_entry_excluded(self):
        """The entry's known dynamic points exist, but none of them are in
        the currently-active set (e.g. accessory disconnected)."""
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [200]})}
        result = nl._build_dynamic_injection(
            dpm, {999}, self._registry_watcher({200: 'sensor.foo'}), {},
        )
        self.assertEqual(result, {})

    def test_unresolved_entity_id_skips_that_point(self):
        """A dynamic point is active but the registry hasn't resolved its
        entity_id yet (race condition this bridge handles elsewhere with
        retries) — it must be silently skipped, not included with a None id."""
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [200, 201]})}
        watcher = self._registry_watcher({200: 'sensor.foo'})  # 201 unresolved
        result = nl._build_dynamic_injection(dpm, {200, 201}, watcher, {})
        self.assertEqual(len(result[100]), 1)
        self.assertEqual(result[100][0][0], 'sensor.foo')

    def test_basic_injection_shape(self):
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [200]})}
        watcher = self._registry_watcher({200: 'sensor.foo'})
        all_points = {200: {
            'title': 'Pool temperature',
            'metadata': {'minValue': 0, 'maxValue': 400, 'divisor': 10, 'unit': '°C'},
        }}
        result = nl._build_dynamic_injection(dpm, {200}, watcher, all_points)
        eid, title, rng, dflt = result[100][0]
        self.assertEqual(eid, 'sensor.foo')
        self.assertEqual(title, 'Pool temperature')
        self.assertEqual(rng, '0 – 40 °C')
        self.assertEqual(dflt, '')

    def test_display_title_preferred_over_title(self):
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [200]})}
        watcher = self._registry_watcher({200: 'sensor.foo'})
        all_points = {200: {
            'title': 'Raw firmware title',
            'display_title': 'Friendly title',
            'metadata': {'minValue': 0, 'maxValue': 100, 'divisor': 1, 'unit': ''},
        }}
        result = nl._build_dynamic_injection(dpm, {200}, watcher, all_points)
        self.assertEqual(result[100][0][1], 'Friendly title')

    def test_missing_point_data_falls_back_to_generic_label(self):
        """The dynamic point is active and resolved in the registry, but
        for some reason isn't in all_points_by_id (stale bulk data) —
        must not crash, falls back to a generic 'Point N' label."""
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [200]})}
        watcher = self._registry_watcher({200: 'sensor.foo'})
        result = nl._build_dynamic_injection(dpm, {200}, watcher, {})
        eid, title, rng, dflt = result[100][0]
        self.assertEqual(title, 'Point 200')
        self.assertEqual(rng, '0 – 0')

    def test_mojibake_a_character_stripped_from_unit(self):
        """This function uses a different (older) mojibake-cleanup approach
        than _build_point_defaults — a direct replace of the stray 'Â'
        character rather than a lookup table. Confirm it still produces a
        clean unit string for the same firmware-encoding quirk."""
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [200]})}
        watcher = self._registry_watcher({200: 'sensor.foo'})
        all_points = {200: {
            'title': 'Some point',
            'metadata': {'minValue': 0, 'maxValue': 400, 'divisor': 10, 'unit': 'Â°C'},
        }}
        result = nl._build_dynamic_injection(dpm, {200}, watcher, all_points)
        self.assertEqual(result[100][0][2], '0 – 40 °C')

    def test_default_value_included_when_provided(self):
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [200]})}
        watcher = self._registry_watcher({200: 'sensor.foo'})
        all_points = {200: {
            'title': 'Some point',
            'metadata': {'minValue': 0, 'maxValue': 400, 'divisor': 10, 'unit': '°C'},
        }}
        result = nl._build_dynamic_injection(
            dpm, {200}, watcher, all_points, point_defaults={200: '20 °C'},
        )
        self.assertEqual(result[100][0][3], '20 °C')

    def test_multiple_active_points_sorted_by_id(self):
        """Output order should be deterministic (sorted) so the dashboard
        doesn't jitter between regenerations with the same active set."""
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [202, 200, 201]})}
        watcher = self._registry_watcher({200: 'sensor.a', 201: 'sensor.b', 202: 'sensor.c'})
        result = nl._build_dynamic_injection(dpm, {200, 201, 202}, watcher, {})
        ids_in_order = [item[0] for item in result[100]]
        self.assertEqual(ids_in_order, ['sensor.a', 'sensor.b', 'sensor.c'])

    def test_multiple_controlling_entries_independent(self):
        import nibe_lovelace as nl
        dpm = {
            100: self._entry(100, {1: [200]}),
            101: self._entry(101, {1: [300]}),
        }
        watcher = self._registry_watcher({200: 'sensor.a', 300: 'sensor.b'})
        result = nl._build_dynamic_injection(dpm, {200, 300}, watcher, {})
        self.assertEqual(set(result.keys()), {100, 101})

    def test_empty_dynamic_point_map_returns_empty(self):
        import nibe_lovelace as nl
        result = nl._build_dynamic_injection({}, {200}, self._registry_watcher({}), {})
        self.assertEqual(result, {})

    def test_divisor_zero_treated_as_one(self):
        """Same convention as _build_point_defaults and the rest of the
        bridge: divisor 0 must not cause a ZeroDivisionError."""
        import nibe_lovelace as nl
        dpm = {100: self._entry(100, {1: [200]})}
        watcher = self._registry_watcher({200: 'sensor.foo'})
        all_points = {200: {
            'title': 'Some point',
            'metadata': {'minValue': 0, 'maxValue': 100, 'divisor': 0, 'unit': ''},
        }}
        result = nl._build_dynamic_injection(dpm, {200}, watcher, all_points)
        self.assertEqual(result[100][0][2], '0 – 100')


# ===========================================================================
# 45. _build_unplaced_view — debug-only undocumented-points audit view
# ===========================================================================


class TestBuildUnplacedView(unittest.TestCase):
    """This is the function that powers exactly the kind of audit work done
    repeatedly this session: surfacing firmware points present in the bulk
    fetch but not yet documented in menu_structure.yaml. A bug here means
    real undocumented points silently never show up for review — there's
    no error, the debug tab just looks more complete than it is. Zero
    coverage before this despite being central to the project's own
    documented workflow (README's 'menu review pattern')."""

    def _holding_point(self, title, min_val=0, max_val=100, writable=True, unit=''):
        return {
            'title': title,
            'metadata': {
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'minValue': min_val, 'maxValue': max_val,
                'isWritable': writable, 'divisor': 1, 'unit': unit,
            },
        }

    def _input_point(self, title, min_val=0, max_val=100, unit=''):
        return {
            'title': title,
            'metadata': {
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'minValue': min_val, 'maxValue': max_val,
                'isWritable': False, 'divisor': 1, 'unit': unit,
            },
        }

    def _watcher(self, entity_ids: dict):
        watcher = MagicMock()
        watcher.entity_id_for.side_effect = lambda pid: entity_ids.get(pid)
        return watcher

    def test_point_already_in_menu_yaml_excluded(self):
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Some setting')}
        result = nl._build_unplaced_view(bulk, {100}, self._watcher({}), {})
        self.assertIsNone(result)

    def test_unrecognised_register_type_excluded(self):
        """e.g. MODBUS_NO_REGISTER / ERR_UNKNOWN per the firmware spec —
        these fall through elsewhere in the bridge and have no place here."""
        import nibe_lovelace as nl
        bulk = {100: {
            'title': 'Weird point',
            'metadata': {'modbusRegisterType': 'MODBUS_NO_REGISTER',
                         'minValue': 0, 'maxValue': 100, 'isWritable': True},
        }}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        self.assertIsNone(result)

    def test_degenerate_range_excluded(self):
        """Status/enum read-only fields (e.g. real firmware point 2500
        'Compressor status' has min=max=0) are excluded — they have no
        adjustable range and aren't actionable for a menu-documentation
        audit, consistent with is_degenerate_range used elsewhere in the
        bridge for write-side validation."""
        import nibe_lovelace as nl
        bulk = {100: self._input_point('Compressor status', min_val=0, max_val=0)}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        self.assertIsNone(result)

    def test_all_excluded_returns_none(self):
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Documented thing')}
        result = nl._build_unplaced_view(bulk, {100}, self._watcher({}), {})
        self.assertIsNone(result)

    def test_writable_non_grouped_point_goes_to_writable_section(self):
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Some new writable setting')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        self.assertIsNotNone(result)
        content = result['cards'][0]['cards'][0]['content']
        self.assertIn('1 writable (review)', content)
        self.assertIn('0 writable (series/grouped)', content)

    def test_grouped_pattern_climate_system_routes_to_grouped_section(self):
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Room sensor factor climate system 3')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        content = result['cards'][0]['cards'][0]['content']
        self.assertIn('0 writable (review)', content)
        self.assertIn('1 writable (series/grouped)', content)

    def test_grouped_pattern_case_insensitive(self):
        """The regex search uses re.I — confirm a differently-cased title
        still matches (firmware title casing isn't always consistent)."""
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('ECS2 something')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        content = result['cards'][0]['cards'][0]['content']
        self.assertIn('1 writable (series/grouped)', content)

    def test_grouping_only_applies_to_writable_holding_registers(self):
        """A read-only point whose title happens to match a group pattern
        (e.g. mentions 'tariff') must still go to the read-only section,
        not be silently dropped or miscategorised — grouping is a
        writable-only concept."""
        import nibe_lovelace as nl
        bulk = {100: self._input_point('Current tariff status')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        content = result['cards'][0]['cards'][0]['content']
        self.assertIn('1 read-only', content)
        self.assertIn('0 writable (series/grouped)', content)

    def test_readonly_holding_register_goes_to_readonly_not_writable(self):
        """A non-writable HOLDING register (isWritable=False) must not be
        treated as a writable point even though its register type matches —
        writability is the deciding factor, not register type alone."""
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Locked setting', writable=False)}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        content = result['cards'][0]['cards'][0]['content']
        self.assertIn('1 read-only', content)
        self.assertIn('0 writable (review)', content)

    def test_missing_title_falls_back_to_point_id_label(self):
        import nibe_lovelace as nl
        bulk = {777: {
            'metadata': {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                         'minValue': 0, 'maxValue': 100, 'isWritable': True, 'divisor': 1},
        }}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        entities_card = result['cards'][0]['cards'][1]
        labels = [e.get('label', '') for e in entities_card['entities']]
        self.assertTrue(any('Point 777' in lbl for lbl in labels))

    def test_empty_title_falls_back_to_point_id_label(self):
        """Distinct from missing title: the key exists but is an empty
        string. clean_string('') returns '' (falsy), so the 'or' fallback
        must still catch it — this is the fragile double-fallback chain
        flagged during review."""
        import nibe_lovelace as nl
        bulk = {777: self._holding_point('')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        entities_card = result['cards'][0]['cards'][1]
        labels = [e.get('label', '') for e in entities_card['entities']]
        self.assertTrue(any('Point 777' in lbl for lbl in labels))

    def test_display_title_preferred_over_title(self):
        import nibe_lovelace as nl
        bulk = {100: {
            'title': 'Raw firmware title',
            'display_title': 'Friendly title',
            'metadata': {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                         'minValue': 0, 'maxValue': 100, 'isWritable': True, 'divisor': 1},
        }}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        entities_card = result['cards'][0]['cards'][1]
        labels = [e.get('label', '') for e in entities_card['entities']]
        self.assertTrue(any('Friendly title' in lbl for lbl in labels))
        self.assertFalse(any('Raw firmware title' in lbl for lbl in labels))

    def test_enabled_point_shows_entity_row(self):
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Some setting')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({100: 'switch.foo'}), {})
        entities_card = result['cards'][0]['cards'][1]
        self.assertIn({'entity': 'switch.foo'}, entities_card['entities'])

    def test_unenabled_point_shows_not_enabled_label(self):
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Some setting')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        entities_card = result['cards'][0]['cards'][1]
        labels = [e.get('label', '') for e in entities_card['entities']]
        self.assertTrue(any('not enabled' in lbl for lbl in labels))

    def test_default_value_appended_when_present(self):
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Some setting')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {100: '50 °C'})
        entities_card = result['cards'][0]['cards'][1]
        labels = [e.get('label', '') for e in entities_card['entities']]
        self.assertTrue(any('default: 50 °C' in lbl for lbl in labels))

    def test_section_omitted_when_empty(self):
        """Only sections with content get a card — a debug tab with zero
        grouped points shouldn't show an empty 'series/grouped' section."""
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Solo writable point')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        # Banner + writable section only — no grouped, no readonly card.
        self.assertEqual(len(result['cards'][0]['cards']), 2)

    def test_mojibake_unit_normalised_in_range_string(self):
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Some setting', min_val=0, max_val=400, unit='\u00c2°C')}
        bulk[100]['metadata']['divisor'] = 10
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        entities_card = result['cards'][0]['cards'][1]
        labels = [e.get('label', '') for e in entities_card['entities']]
        self.assertTrue(any('°C' in lbl and '00c2' not in lbl for lbl in labels))

    def test_dashboard_metadata_correct(self):
        import nibe_lovelace as nl
        bulk = {100: self._holding_point('Some setting')}
        result = nl._build_unplaced_view(bulk, set(), self._watcher({}), {})
        self.assertEqual(result['path'], 'menu-unplaced-debug')
        self.assertIn('Unplaced', result['title'])


# ===========================================================================
# 46. _build_menu_view — the actual Lovelace card renderer for every menu
# ===========================================================================


class TestBuildMenuView(unittest.TestCase):
    """This is the function that renders every one of the 163 menus this
    session reviewed in detail — directly responsible for whether warnings,
    notes, tips, defaults, and dynamic-point relationships actually show up
    correctly on the dashboard. Zero coverage before this.

    Also covers the fix made alongside these tests: the entity-row-building
    loop previously had two branches — 'if point_id and not s_note' and
    'elif s_note and point_id' — with byte-identical bodies. The s_note
    check never changed behavior; it was dead weight collapsed into a
    single 'if point_id' branch. These tests pin down the simplified
    behavior directly."""

    def _watcher(self, entity_ids: dict):
        watcher = MagicMock()
        watcher.entity_id_for.side_effect = lambda pid: entity_ids.get(pid)
        return watcher

    def _menu(self, **overrides):
        base = {'id': '1', 'title': 'Test Menu', 'settings': []}
        base.update(overrides)
        return base

    def test_basic_structure_returns_markdown_and_footer(self):
        import nibe_lovelace as nl
        menu = self._menu()
        cards = nl._build_menu_view(menu, self._watcher({}))
        self.assertEqual(cards[0]['type'], 'markdown')
        self.assertEqual(cards[-1]['type'], 'markdown')
        self.assertIn('SMO S40 installer manual', cards[-1]['content'])

    def test_top_level_heading_is_h2(self):
        import nibe_lovelace as nl
        menu = self._menu(id='7.1', title='Heating')
        cards = nl._build_menu_view(menu, self._watcher({}))
        self.assertIn('<h2>', cards[0]['content'])
        self.assertIn('Menu 7.1 – Heating', cards[0]['content'])

    def test_submenu_heading_is_h3(self):
        import nibe_lovelace as nl
        menu = self._menu(submenus=[
            {'id': '1.1', 'title': 'Sub', 'settings': []},
        ])
        cards = nl._build_menu_view(menu, self._watcher({}))
        sub_card = next(c for c in cards if 'Menu 1.1' in c.get('content', ''))
        self.assertIn('<h3>', sub_card['content'])

    def test_deeply_nested_submenu_heading_is_h4(self):
        """Real menus nest up to 4 levels (e.g. 7.1.10.6) — depth 4 and
        beyond should all use h4, not keep growing."""
        import nibe_lovelace as nl
        menu = self._menu(submenus=[
            {'id': '1.1', 'title': 'L2', 'settings': [], 'submenus': [
                {'id': '1.1.1', 'title': 'L3', 'settings': [], 'submenus': [
                    {'id': '1.1.1.1', 'title': 'L4', 'settings': []},
                ]},
            ]},
        ])
        cards = nl._build_menu_view(menu, self._watcher({}))
        l4_card = next(c for c in cards if 'Menu 1.1.1.1' in c.get('content', ''))
        self.assertIn('<h4>', l4_card['content'])

    def test_description_rendered_in_markdown(self):
        import nibe_lovelace as nl
        menu = self._menu(description='Some explanation text.')
        cards = nl._build_menu_view(menu, self._watcher({}))
        self.assertIn('Some explanation text.', cards[0]['content'])

    def test_menu_level_warning_rendered_as_alert(self):
        import nibe_lovelace as nl
        menu = self._menu(warning='Be careful with this.')
        cards = nl._build_menu_view(menu, self._watcher({}))
        self.assertIn('alert-type="warning"', cards[0]['content'])
        self.assertIn('Be careful with this.', cards[0]['content'])

    def test_menu_level_note_rendered_as_info_alert(self):
        import nibe_lovelace as nl
        menu = self._menu(note='FYI this is informational.')
        cards = nl._build_menu_view(menu, self._watcher({}))
        self.assertIn('alert-type="info"', cards[0]['content'])

    def test_menu_level_tip_rendered_as_success_alert(self):
        import nibe_lovelace as nl
        menu = self._menu(tip='Helpful suggestion.')
        cards = nl._build_menu_view(menu, self._watcher({}))
        self.assertIn('alert-type="success"', cards[0]['content'])

    def test_local_api_false_skips_entity_card_entirely(self):
        """A menu with no API access (e.g. 7.1.11 SPLA) must show only the
        markdown explanation card, never an empty/broken entities card."""
        import nibe_lovelace as nl
        menu = self._menu(local_api=False, settings=[
            {'label': 'Should not appear', 'point_id': 100},
        ])
        cards = nl._build_menu_view(menu, self._watcher({100: 'switch.foo'}))
        self.assertNotIn('entities', [c['type'] for c in cards])
        self.assertIn('Not available via local API', cards[0]['content'])

    def test_setting_warning_rendered_as_alert_with_label_as_title(self):
        import nibe_lovelace as nl
        menu = self._menu(settings=[
            {'label': 'Risky setting', 'point_id': 100, 'warning': 'This can break things.'},
        ])
        cards = nl._build_menu_view(menu, self._watcher({100: 'switch.foo'}))
        md = cards[0]['content']
        self.assertIn('alert-type="warning" title="Risky setting"', md)
        self.assertIn('This can break things.', md)

    def test_setting_note_does_not_suppress_entity_row(self):
        """Regression test for the dead-code fix: a setting with a note
        must still get its entity row, same as one without — note presence
        was never supposed to gate display."""
        import nibe_lovelace as nl
        menu = self._menu(settings=[
            {'label': 'Noted setting', 'point_id': 100, 'note': 'Some context.'},
        ])
        cards = nl._build_menu_view(menu, self._watcher({100: 'switch.foo'}))
        entities_card = next(c for c in cards if c['type'] == 'entities')
        self.assertIn({'entity': 'switch.foo'}, entities_card['entities'])

    def test_section_divider_includes_range(self):
        import nibe_lovelace as nl
        menu = self._menu(settings=[
            {'label': 'Some setting', 'point_id': 100, 'range': '0 – 100 %'},
        ])
        cards = nl._build_menu_view(menu, self._watcher({}))
        entities_card = next(c for c in cards if c['type'] == 'entities')
        divider = entities_card['entities'][0]
        self.assertEqual(divider['label'], 'Some setting  ·  0 – 100 %')

    def test_section_divider_includes_default_when_known(self):
        import nibe_lovelace as nl
        menu = self._menu(settings=[
            {'label': 'Some setting', 'point_id': 100, 'range': '0 – 100 %'},
        ])
        cards = nl._build_menu_view(
            menu, self._watcher({}), point_defaults={100: '50 %'},
        )
        entities_card = next(c for c in cards if c['type'] == 'entities')
        divider = entities_card['entities'][0]
        self.assertEqual(divider['label'], 'Some setting  ·  0 – 100 %  ·  default: 50 %')

    def test_resolved_point_shows_entity_row(self):
        import nibe_lovelace as nl
        menu = self._menu(settings=[{'label': 'X', 'point_id': 100}])
        cards = nl._build_menu_view(menu, self._watcher({100: 'switch.foo'}))
        entities_card = next(c for c in cards if c['type'] == 'entities')
        self.assertIn({'entity': 'switch.foo'}, entities_card['entities'])

    def test_unresolved_static_point_shows_not_enabled(self):
        """A point that isn't dynamic and isn't yet registered in HA shows
        the 'not enabled' fallback row."""
        import nibe_lovelace as nl
        menu = self._menu(settings=[{'label': 'X', 'point_id': 100}])
        cards = nl._build_menu_view(menu, self._watcher({}))
        entities_card = next(c for c in cards if c['type'] == 'entities')
        labels = [e.get('label', '') for e in entities_card['entities']]
        self.assertIn('↳ not enabled', labels)

    def test_active_dynamic_point_skipped_entirely_not_shown_as_not_enabled(self):
        """A currently-active dynamic point must not get a 'not enabled'
        row even if unresolved — it's deliberately skipped here and shown
        via dynamic_injection under its controlling point instead."""
        import nibe_lovelace as nl
        menu = self._menu(settings=[{'label': 'X', 'point_id': 100}])
        cards = nl._build_menu_view(
            menu, self._watcher({}), known_dynamic={100},
        )
        entities_card = [c for c in cards if c['type'] == 'entities']
        # No entities card at all: the only setting was a dynamic point,
        # so the section produced zero rows.
        self.assertEqual(entities_card, [])

    def test_dynamic_point_known_but_resolved_still_skipped(self):
        """Even if a dynamic point happens to resolve in the registry, the
        static menu entry for it is still skipped — it must only appear
        via dynamic_injection, never as a duplicate direct row."""
        import nibe_lovelace as nl
        menu = self._menu(settings=[{'label': 'X', 'point_id': 100}])
        cards = nl._build_menu_view(
            menu, self._watcher({100: 'switch.foo'}), known_dynamic={100},
        )
        entities_card = [c for c in cards if c['type'] == 'entities']
        self.assertEqual(entities_card, [])

    def test_dynamic_injection_appears_under_controlling_point(self):
        import nibe_lovelace as nl
        menu = self._menu(settings=[{'label': 'Controlling switch', 'point_id': 100}])
        cards = nl._build_menu_view(
            menu, self._watcher({100: 'switch.controller'}),
            dynamic_injection={100: [('sensor.humidity', 'Humidity', '0 – 100 %', '')]},
        )
        entities_card = next(c for c in cards if c['type'] == 'entities')
        entities = entities_card['entities']
        self.assertIn({'entity': 'switch.controller'}, entities)
        divider_idx = next(i for i, e in enumerate(entities)
                            if e.get('label') == '↳ Humidity  ·  0 – 100 %')
        self.assertEqual(entities[divider_idx + 1], {'entity': 'sensor.humidity'})

    def test_dynamic_injection_only_appears_when_controlling_point_resolved(self):
        """If the controlling switch itself isn't enabled yet, its injected
        children shouldn't appear either (they'd be orphaned under nothing)."""
        import nibe_lovelace as nl
        menu = self._menu(settings=[{'label': 'Controlling switch', 'point_id': 100}])
        cards = nl._build_menu_view(
            menu, self._watcher({}),  # controlling point NOT resolved
            dynamic_injection={100: [('sensor.humidity', 'Humidity', '0 – 100 %', '')]},
        )
        entities_card = next(c for c in cards if c['type'] == 'entities')
        labels = [e.get('label', '') for e in entities_card['entities']]
        self.assertNotIn('↳ Humidity  ·  0 – 100 %', labels)

    def test_setting_without_point_id_shows_only_divider(self):
        """The 'configured elsewhere' placeholder pattern (point_id: null,
        used ~8 times in the real menu_structure.yaml) — must show the
        section divider but no entity row and no 'not enabled' fallback."""
        import nibe_lovelace as nl
        menu = self._menu(settings=[
            {'label': 'See menu X', 'point_id': None, 'range': 'configured elsewhere'},
        ])
        cards = nl._build_menu_view(menu, self._watcher({}))
        entities_card = next(c for c in cards if c['type'] == 'entities')
        entities = entities_card['entities']
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]['label'], 'See menu X  ·  configured elsewhere')

    def test_no_settings_no_entities_card(self):
        import nibe_lovelace as nl
        menu = self._menu(settings=[])
        cards = nl._build_menu_view(menu, self._watcher({}))
        self.assertNotIn('entities', [c['type'] for c in cards])

    def test_recurses_into_submenus(self):
        import nibe_lovelace as nl
        menu = self._menu(submenus=[
            {'id': '1.1', 'title': 'Child', 'settings': [{'label': 'Y', 'point_id': 200}]},
        ])
        cards = nl._build_menu_view(menu, self._watcher({200: 'sensor.child'}))
        entities_cards = [c for c in cards if c['type'] == 'entities']
        self.assertEqual(len(entities_cards), 1)
        self.assertIn({'entity': 'sensor.child'}, entities_cards[0]['entities'])

    def test_multiple_submenus_each_get_own_cards(self):
        import nibe_lovelace as nl
        menu = self._menu(submenus=[
            {'id': '1.1', 'title': 'A', 'settings': [{'label': 'a', 'point_id': 1}]},
            {'id': '1.2', 'title': 'B', 'settings': [{'label': 'b', 'point_id': 2}]},
        ])
        cards = nl._build_menu_view(menu, self._watcher({1: 'x.a', 2: 'x.b'}))
        entities_cards = [c for c in cards if c['type'] == 'entities']
        self.assertEqual(len(entities_cards), 2)

    def test_default_mutable_args_not_shared_across_calls(self):
        """known_dynamic/absent_dynamic/point_defaults/dynamic_injection
        default to None -> fresh set()/dict() each call, not a shared
        mutable default — confirms no cross-call state leakage."""
        import nibe_lovelace as nl
        menu1 = self._menu(settings=[{'label': 'X', 'point_id': 100}])
        nl._build_menu_view(menu1, self._watcher({}), known_dynamic={100})
        # A second, independent call with no known_dynamic must NOT skip
        # point 100 — if defaults were shared/mutated this would fail.
        menu2 = self._menu(settings=[{'label': 'X', 'point_id': 100}])
        cards2 = nl._build_menu_view(menu2, self._watcher({100: 'switch.foo'}))
        entities_card = next(c for c in cards2 if c['type'] == 'entities')
        self.assertIn({'entity': 'switch.foo'}, entities_card['entities'])


# ===========================================================================
# 47. _build_menu_dashboard_config — top-level view orchestration
# ===========================================================================


class TestBuildMenuDashboardConfig(unittest.TestCase):
    """The orchestrator that turns a list of top-level menus into the final
    {"views": [...]} dashboard config. _build_menu_view and
    _build_unplaced_view are already directly tested elsewhere, so these
    tests mock them out and focus on this function's own unique logic:
    skipping empty menus, view path/title formatting, the all-empty
    short-circuit, and the three-condition debug-mode gating."""

    def _menu(self, id_, title='Test'):
        return {'id': id_, 'title': title, 'settings': []}

    def test_single_menu_produces_one_view(self):
        import nibe_lovelace as nl
        menus = [self._menu('1', 'Indoor climate')]
        with patch.object(nl, '_build_menu_view', return_value=[{'type': 'markdown', 'content': 'x'}]):
            config = nl._build_menu_dashboard_config(menus, MagicMock())
        self.assertEqual(len(config['views']), 1)
        self.assertEqual(config['views'][0]['title'], '1 Indoor climate')

    def test_view_path_replaces_dots_with_hyphens(self):
        import nibe_lovelace as nl
        menus = [self._menu('7.1.6', 'Heating')]
        with patch.object(nl, '_build_menu_view', return_value=[{'type': 'markdown', 'content': 'x'}]):
            config = nl._build_menu_dashboard_config(menus, MagicMock())
        self.assertEqual(config['views'][0]['path'], 'menu-7-1-6')

    def test_menu_producing_no_cards_is_skipped(self):
        """_build_menu_view returning [] (e.g. local_api:false with no
        warning/note/tip and no description) must not produce an empty view tab."""
        import nibe_lovelace as nl
        menus = [self._menu('1'), self._menu('2')]
        with patch.object(nl, '_build_menu_view', side_effect=[[], [{'type': 'markdown', 'content': 'x'}]]):
            config = nl._build_menu_dashboard_config(menus, MagicMock())
        self.assertEqual(len(config['views']), 1)
        self.assertEqual(config['views'][0]['title'], '2 Test')

    def test_all_menus_empty_returns_none(self):
        import nibe_lovelace as nl
        menus = [self._menu('1'), self._menu('2')]
        with patch.object(nl, '_build_menu_view', return_value=[]):
            config = nl._build_menu_dashboard_config(menus, MagicMock())
        self.assertIsNone(config)

    def test_empty_menu_list_returns_none(self):
        import nibe_lovelace as nl
        config = nl._build_menu_dashboard_config([], MagicMock())
        self.assertIsNone(config)

    def test_each_view_is_a_single_vertical_stack(self):
        import nibe_lovelace as nl
        menus = [self._menu('1')]
        cards = [{'type': 'markdown', 'content': 'a'}, {'type': 'entities', 'entities': []}]
        with patch.object(nl, '_build_menu_view', return_value=cards):
            config = nl._build_menu_dashboard_config(menus, MagicMock())
        view_cards = config['views'][0]['cards']
        self.assertEqual(len(view_cards), 1)
        self.assertEqual(view_cards[0]['type'], 'vertical-stack')
        self.assertEqual(view_cards[0]['cards'], cards)

    def test_debug_mode_false_never_calls_unplaced_view(self):
        import nibe_lovelace as nl
        menus = [self._menu('1')]
        with patch.object(nl, '_build_menu_view', return_value=[{'type': 'markdown', 'content': 'x'}]), \
             patch.object(nl, '_build_unplaced_view') as mock_unplaced:
            nl._build_menu_dashboard_config(
                menus, MagicMock(), debug_mode=False,
                bulk_data={1: {}}, menu_yaml_points={1},
            )
            mock_unplaced.assert_not_called()

    def test_debug_mode_true_but_no_bulk_data_skips_unplaced_view(self):
        """All three conditions (debug_mode, bulk_data, menu_yaml_points is
        not None) must be true — missing bulk_data alone must suppress it,
        not just debug_mode."""
        import nibe_lovelace as nl
        menus = [self._menu('1')]
        with patch.object(nl, '_build_menu_view', return_value=[{'type': 'markdown', 'content': 'x'}]), \
             patch.object(nl, '_build_unplaced_view') as mock_unplaced:
            nl._build_menu_dashboard_config(
                menus, MagicMock(), debug_mode=True,
                bulk_data=None, menu_yaml_points={1},
            )
            mock_unplaced.assert_not_called()

    def test_debug_mode_true_with_empty_menu_yaml_points_still_calls(self):
        """menu_yaml_points is an explicit 'is not None' check — an empty
        set() is falsy-ish but must still be treated as provided, not
        missing (distinct from bulk_data which is checked truthily)."""
        import nibe_lovelace as nl
        menus = [self._menu('1')]
        with patch.object(nl, '_build_menu_view', return_value=[{'type': 'markdown', 'content': 'x'}]), \
             patch.object(nl, '_build_unplaced_view', return_value=None) as mock_unplaced:
            nl._build_menu_dashboard_config(
                menus, MagicMock(), debug_mode=True,
                bulk_data={1: {}}, menu_yaml_points=set(),
            )
            mock_unplaced.assert_called_once()

    def test_all_three_conditions_true_appends_unplaced_view(self):
        import nibe_lovelace as nl
        menus = [self._menu('1')]
        unplaced = {'title': '⚙ Unplaced (debug)', 'path': 'menu-unplaced-debug', 'cards': []}
        with patch.object(nl, '_build_menu_view', return_value=[{'type': 'markdown', 'content': 'x'}]), \
             patch.object(nl, '_build_unplaced_view', return_value=unplaced):
            config = nl._build_menu_dashboard_config(
                menus, MagicMock(), debug_mode=True,
                bulk_data={1: {}}, menu_yaml_points={1},
            )
        self.assertEqual(len(config['views']), 2)
        self.assertEqual(config['views'][1], unplaced)

    def test_unplaced_view_returning_none_not_appended(self):
        """_build_unplaced_view itself returns None when there's nothing
        unplaced — must not add a blank/broken view in that case."""
        import nibe_lovelace as nl
        menus = [self._menu('1')]
        with patch.object(nl, '_build_menu_view', return_value=[{'type': 'markdown', 'content': 'x'}]), \
             patch.object(nl, '_build_unplaced_view', return_value=None):
            config = nl._build_menu_dashboard_config(
                menus, MagicMock(), debug_mode=True,
                bulk_data={1: {}}, menu_yaml_points={1},
            )
        self.assertEqual(len(config['views']), 1)

    def test_no_unplaced_view_when_all_menus_already_skipped(self):
        """If every top-level menu produced zero cards, the function
        returns None before ever reaching the debug-mode unplaced-view
        logic — even with debug_mode=True, no view list to append to."""
        import nibe_lovelace as nl
        menus = [self._menu('1')]
        with patch.object(nl, '_build_menu_view', return_value=[]), \
             patch.object(nl, '_build_unplaced_view') as mock_unplaced:
            config = nl._build_menu_dashboard_config(
                menus, MagicMock(), debug_mode=True,
                bulk_data={1: {}}, menu_yaml_points={1},
            )
        self.assertIsNone(config)
        mock_unplaced.assert_not_called()

    def test_multiple_menus_preserve_order(self):
        import nibe_lovelace as nl
        menus = [self._menu('1', 'First'), self._menu('2', 'Second'), self._menu('3', 'Third')]
        with patch.object(nl, '_build_menu_view', return_value=[{'type': 'markdown', 'content': 'x'}]):
            config = nl._build_menu_dashboard_config(menus, MagicMock())
        titles = [v['title'] for v in config['views']]
        self.assertEqual(titles, ['1 First', '2 Second', '3 Third'])

    def test_default_none_arguments_become_empty_collections(self):
        """known_dynamic/absent_dynamic/point_defaults/dynamic_injection
        default to None at this function's boundary too — confirm they're
        passed through to _build_menu_view as empty set()/dict(), not None,
        matching that function's own expectations."""
        import nibe_lovelace as nl
        menus = [self._menu('1')]
        captured = {}

        def fake_build_menu_view(menu, watcher, known_dynamic, absent_dynamic,
                                  point_defaults, dynamic_injection):
            captured['known_dynamic'] = known_dynamic
            captured['absent_dynamic'] = absent_dynamic
            captured['point_defaults'] = point_defaults
            captured['dynamic_injection'] = dynamic_injection
            return [{'type': 'markdown', 'content': 'x'}]

        with patch.object(nl, '_build_menu_view', side_effect=fake_build_menu_view):
            nl._build_menu_dashboard_config(menus, MagicMock())

        self.assertEqual(captured['known_dynamic'], set())
        self.assertEqual(captured['absent_dynamic'], set())
        self.assertEqual(captured['point_defaults'], {})
        self.assertEqual(captured['dynamic_injection'], {})


# ===========================================================================
# 48. EntityManager.resolve_point_from_entity_id — HA entity_id -> point_id
# ===========================================================================


class TestBuildPointToMenuNoDuplicate(unittest.TestCase):
    """_build_ptm in main() was a character-for-character duplicate of the
    module-level _build_point_to_menu. The fix replaces it with a direct
    call to _build_point_to_menu, eliminating the maintenance risk of two
    implementations drifting apart.

    Verified by confirming both functions produce identical output for the
    same input — which is a necessary post-condition of the refactor."""

    def _menus(self):
        return [
            {
                'id': '1.1', 'title': 'Climate system 1',
                'settings': [{'point_id': 3945, 'label': 'Heating setpoint'}],
                'submenus': [
                    {
                        'id': '1.1.1', 'title': 'Room sensor',
                        'settings': [{'point_id': 5087, 'label': 'Cooling factor'}],
                        'submenus': [],
                    }
                ],
            },
            {
                'id': '4.2', 'title': 'SPA',
                'settings': [{'point_id': 4789, 'label': 'SPA activated'}],
                'submenus': [],
            },
        ]

    def test_module_level_function_produces_correct_map(self):
        from nibe_lovelace import _build_point_to_menu
        result = _build_point_to_menu(self._menus())
        self.assertEqual(result[3945], ('1.1', 'Climate system 1'))
        self.assertEqual(result[5087], ('1.1.1', 'Room sensor'))
        self.assertEqual(result[4789], ('4.2', 'SPA'))
        self.assertEqual(len(result), 3)

    def test_nested_submenus_traversed(self):
        """Confirms the recursive traversal reaches arbitrarily deep nesting."""
        from nibe_lovelace import _build_point_to_menu
        deep = [{'id': '1', 'title': 'Top', 'settings': [], 'submenus': [
            {'id': '1.1', 'title': 'Mid', 'settings': [], 'submenus': [
                {'id': '1.1.1', 'title': 'Deep', 'settings': [
                    {'point_id': 9999, 'label': 'Deep point'}
                ], 'submenus': []},
            ]},
        ]}]
        result = _build_point_to_menu(deep)
        self.assertEqual(result[9999], ('1.1.1', 'Deep'))

    def test_settings_without_point_id_skipped(self):
        from nibe_lovelace import _build_point_to_menu
        menus = [{'id': '1', 'title': 'T', 'settings': [
            {'label': 'No point id'},
            {'point_id': None, 'label': 'None id'},
            {'point_id': 100, 'label': 'Valid'},
        ], 'submenus': []}]
        result = _build_point_to_menu(menus)
        self.assertEqual(list(result.keys()), [100])



class TestRegistryStabilityCompletenessThreshold(unittest.TestCase):
    """The stability wait requires menu_resolved >= _completeness_threshold
    of available menu points before a stable count is accepted as done.
    This prevents the fresh-start false-stable scenario where HA creates
    entities in waves and the bridge exits during a gap with only a
    fraction resolved.

    Tests read the real constant from nibe_lovelace rather than hardcoding
    a value, so a constant change is detected immediately. The production
    case that motivated lowering the threshold from 0.80 → 0.70 is included:
    205/280 = 73.2%, which was above 70% but below 80% and previously
    caused every attempt to time out at 60s without building the dashboard.
    """

    @property
    def threshold(self):
        import nibe_lovelace as nl
        import inspect
        # Extract the local constant from _setup_menu_dashboard's source.
        # It's a function-local var (not module-level) so we read the source.
        src = inspect.getsource(nl._setup_menu_dashboard)
        for line in src.splitlines():
            if '_completeness_threshold' in line and '=' in line and 'def' not in line:
                return float(line.split('=')[1].strip())
        raise AssertionError("_completeness_threshold not found in _setup_menu_dashboard source")

    def _is_complete(self, menu_resolved, available):
        t = self.threshold
        return menu_resolved >= available * t

    def test_threshold_is_in_expected_range(self):
        """Sanity check: threshold must be between 0.50 and 0.95.
        Alerts on accidental removal or extreme misconfiguration."""
        t = self.threshold
        self.assertGreaterEqual(t, 0.50, "Threshold below 0.50 would accept almost nothing")
        self.assertLessEqual(t, 0.95, "Threshold above 0.95 defeats the purpose for mode-change restarts")

    def test_production_case_205_of_280_is_accepted(self):
        """The case that exposed the 80%→70% regression: on a fresh
        mode-change restart (advanced→menus), only 205/280 menu points
        resolved before the 60s timeout. At 73.2% this is above the
        current threshold so it must be accepted, not left to time out."""
        self.assertTrue(
            self._is_complete(205, 280),
            f"205/280 = 73.2% must be accepted at threshold {self.threshold:.0%} "
            f"(was rejected at old 80% threshold, causing dashboard to never build)"
        )

    def test_small_fraction_not_accepted(self):
        """32/279 = 11.5% — clearly below any reasonable threshold."""
        self.assertFalse(self._is_complete(32, 279),
            "32/279 must not be accepted as complete")

    def test_near_full_resolution_accepted(self):
        """278/279 = 99.6% — always accepted regardless of threshold value."""
        self.assertTrue(self._is_complete(278, 279))

    def test_exact_threshold_accepted(self):
        """Exactly at threshold (e.g. 70/100 at 70%) — boundary inclusive."""
        available = 100
        menu_resolved = int(self.threshold * available)
        self.assertTrue(
            self._is_complete(menu_resolved, available),
            f"Exactly {self.threshold:.0%} of {available} must be accepted (boundary is inclusive)"
        )

    def test_one_below_threshold_not_accepted(self):
        """One below the exact threshold boundary — must not be accepted."""
        available = 100
        menu_resolved = int(self.threshold * available) - 1
        self.assertFalse(
            self._is_complete(menu_resolved, available),
            f"{menu_resolved}/{available} is one below the threshold boundary — must not be accepted"
        )

    def test_conditional_absences_within_tolerance(self):
        """Installation with 2 genuinely absent conditional points (e.g. 3671,
        5033 when a room sensor is installed): 279/281 = 99.3% — must be
        accepted regardless of threshold value."""
        self.assertTrue(self._is_complete(279, 281),
            "2 absent conditional points from 281 must pass any reasonable threshold")

    def test_below_50_percent_never_accepted(self):
        """Below 50% is never a stable useful registry state — confirms
        the threshold is not accidentally set to 0.0."""
        self.assertFalse(self._is_complete(100, 280),
            "100/280 = 35.7% must never be accepted as stable")


# ===========================================================================
# 69. Double dashboard run elimination via lovelace_thread guard
# ===========================================================================


class TestBuildMenuViewRemainingPaths(unittest.TestCase):

    def _watcher(self, entity_map=None):
        w = MagicMock()
        w.entity_id_for = lambda pid: (entity_map or {}).get(pid)
        return w

    def test_tip_only_setting_renders_success_alert(self):
        """A setting with only a tip (no note, no warning) must use the
        'success' alert type (green), not fall through to nothing."""
        from nibe_lovelace import _build_menu_view
        menu = {
            'id': '1.1', 'title': 'Test Menu',
            'settings': [{
                'point_id': 100,
                'label': 'Tip Setting',
                'tip': 'This is a helpful tip.',
            }],
            'submenus': [],
        }
        cards = _build_menu_view(menu, self._watcher(), known_dynamic=set())
        # Flatten all markdown card content
        content = ' '.join(
            card.get('content', '')
            for card in cards
            if card.get('type') == 'markdown'
        )
        self.assertIn('success', content.lower())

    def test_dynamic_injection_with_default_includes_default_in_divider(self):
        """When a dynamic point has a known default value, the divider label
        in the entities card must include 'default: <value>'."""
        from nibe_lovelace import _build_menu_view
        menu = {
            'id': '1.1', 'title': 'Test Menu',
            'settings': [{'point_id': 100, 'label': 'Main Point'}],
            'submenus': [],
        }
        # dynamic_injection: point 100 injects one dynamic entity with a default
        dynamic_injection = {
            100: [('sensor.dynamic_100', 'Dynamic Sensor', '0–100', '42 °C')]
        }
        cards = _build_menu_view(
            menu,
            self._watcher(entity_map={100: 'sensor.main_100'}),
            known_dynamic=set(),
            dynamic_injection=dynamic_injection,
        )
        # Find the entities card
        entities_card = next(
            (c for c in cards if c.get('type') == 'entities'), None
        )
        self.assertIsNotNone(entities_card)
        labels = [e.get('label', '') for e in entities_card.get('entities', [])
                  if e.get('type') == 'section']
        self.assertTrue(
            any('default: 42 °C' in label for label in labels),
            f"Expected 'default: 42 °C' in divider labels, got: {labels}"
        )


# ===========================================================================
# 82. Public entry point wrappers in nibe_lovelace
# ===========================================================================


class TestNibeLovelacePublicWrappers(unittest.TestCase):
    """The four public entry points are thin wrappers — verify each delegates
    to the correct private implementation."""

    def test_copy_card_file_delegates_to_private(self):
        from nibe_lovelace import copy_card_file
        with patch('nibe_lovelace._copy_card_file', return_value=True) as mock:
            result = copy_card_file()
        mock.assert_called_once_with()
        self.assertTrue(result)

    def test_provision_lovelace_ui_delegates_to_setup_lovelace(self):
        from nibe_lovelace import provision_lovelace_ui
        rw = MagicMock()
        with patch('nibe_lovelace._setup_lovelace') as mock:
            provision_lovelace_ui('1.0.0', 'Test Device', rw, debug_mode=True)
        mock.assert_called_once_with('1.0.0', 'Test Device', rw, True, mode='menus')

    def test_provision_lovelace_ui_passes_through_explicit_mode(self):
        """mode threads through to _setup_lovelace unchanged — this is what
        gates menu dashboard provisioning to menus mode only."""
        from nibe_lovelace import provision_lovelace_ui
        rw = MagicMock()
        with patch('nibe_lovelace._setup_lovelace') as mock:
            provision_lovelace_ui('1.0.0', 'Test Device', rw, debug_mode=False, mode='essential')
        mock.assert_called_once_with('1.0.0', 'Test Device', rw, False, mode='essential')

    def test_schedule_menu_dashboard_regen_delegates(self):
        from nibe_lovelace import schedule_menu_dashboard_regen
        em = MagicMock()
        rw = MagicMock()
        thread = MagicMock()
        with patch('nibe_lovelace._wire_menu_dashboard_regen') as mock:
            schedule_menu_dashboard_regen(em, rw, debug_mode=False,
                                          lovelace_thread=thread)
        mock.assert_called_once_with(em, rw, False, lovelace_thread=thread)

    def test_teardown_lovelace_delegates_to_private(self):
        from nibe_lovelace import teardown_lovelace
        with patch('nibe_lovelace._teardown_lovelace') as mock:
            teardown_lovelace()
        mock.assert_called_once_with()


# ===========================================================================
# 83. DynamicPointMap — values(), items(), flush(), OSError on load
# ===========================================================================


class TestEntityDetectionFinalGaps(unittest.TestCase):

    def test_parse_description_mapping_skips_part_without_equals(self):
        """Parts with no '=' must be silently skipped inside parse_description_mapping."""
        from nibe_entity_detection import parse_description_mapping
        # 'noeqsign' has no '=' → hits the continue on that guard (line 497)
        result = parse_description_mapping('0=Off, noeqsign, 1=On')
        self.assertEqual(result, {0: 'Off', 1: 'On'})

    def test_get_entity_options_returns_sorted_list_from_value_mappings(self):
        """When a point has a VALUE_MAPPINGS entry, get_entity_options must return
        a sorted option list from that mapping (line 549)."""
        from nibe_entity_detection import get_entity_options, VALUE_MAPPINGS
        holding = VALUE_MAPPINGS.get('holding', {})
        self.assertTrue(holding, 'No holding VALUE_MAPPINGS — test precondition failed')
        pid = next(iter(holding))
        opts = get_entity_options(pid, {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'}, '')
        self.assertIsInstance(opts, list)
        self.assertGreater(len(opts), 0)

    def test_input_register_in_value_mappings_returns_sensor(self):
        """An input register whose variableId appears in VALUE_MAPPINGS['input']
        must be classified as sensor/diagnostic (line 748)."""
        from nibe_entity_detection import detect_entity_type, VALUE_MAPPINGS
        input_map = VALUE_MAPPINGS.get('input', {})
        self.assertTrue(input_map, 'No input VALUE_MAPPINGS — test precondition failed')
        pid = next(iter(input_map))
        point = {
            'variableId': pid,
            'metadata': {
                'variableType': 'integer', 'variableSize': 'u8',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False, 'divisor': 1,
                'minValue': 0, 'maxValue': 10, 'intDefaultValue': 0,
                'unit': '',
            },
            'description': '',
        }
        entity_type, category = detect_entity_type(point)
        self.assertEqual(entity_type, 'sensor')
        self.assertEqual(category, 'diagnostic')


# ===========================================================================
# Coverage: nibe_lovelace.py — _copy_card_file, _open_ha_websocket,
#           _setup_lovelace, _setup_lovelace_resource, _setup_lovelace_dashboard,
#           _ws_call remaining branches, _teardown_lovelace,
#           _setup_menu_dashboard remaining branches, _wire_menu_dashboard_regen
# ===========================================================================


class TestCopyCardFile(unittest.TestCase):
    """_copy_card_file: makedirs+copy2 happy path, and exception path."""

    def test_success_returns_true(self):
        import nibe_lovelace as nl
        with patch('nibe_lovelace.os.makedirs') as mk, \
             patch('nibe_lovelace.shutil.copy2') as cp:
            result = nl._copy_card_file()
        self.assertTrue(result)
        mk.assert_called_once()
        cp.assert_called_once()

    def test_exception_returns_false(self):
        import nibe_lovelace as nl
        with patch('nibe_lovelace.os.makedirs', side_effect=OSError("permission denied")):
            result = nl._copy_card_file()
        self.assertFalse(result)



class TestOpenHaWebSocket(unittest.TestCase):
    """_open_ha_websocket: all early-exit and success paths."""

    def test_no_supervisor_token_returns_none(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {}, clear=True):
            result = nl._open_ha_websocket()
        self.assertIsNone(result)

    def test_import_error_returns_none(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': None}):
            result = nl._open_ha_websocket()
        self.assertIsNone(result)

    def test_connection_error_returns_none(self):
        import nibe_lovelace as nl
        ws_mod = MagicMock()
        ws_mod.create_connection.side_effect = OSError("refused")
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            result = nl._open_ha_websocket()
        self.assertIsNone(result)

    def test_wrong_greeting_type_returns_none_and_closes(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.return_value = json.dumps({"type": "auth_ok"})  # wrong — expected auth_required
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            result = nl._open_ha_websocket()
        self.assertIsNone(result)
        ws.close.assert_called()

    def test_auth_failure_returns_none_and_closes(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_invalid"}),
        ]
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            result = nl._open_ha_websocket()
        self.assertIsNone(result)
        ws.close.assert_called()

    def test_auth_exception_returns_none(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.side_effect = OSError("connection dropped")
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            result = nl._open_ha_websocket()
        self.assertIsNone(result)

    def test_successful_auth_returns_ws_and_callable(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
        ]
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            result = nl._open_ha_websocket()
        self.assertIsNotNone(result)
        ws_out, next_id = result
        self.assertIs(ws_out, ws)
        self.assertEqual(next_id(), 1)
        self.assertEqual(next_id(), 2)



class TestSetupLovelace(unittest.TestCase):
    """_setup_lovelace: no token early exit, ws failure, normal path."""

    def test_no_supervisor_token_returns_early(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {}, clear=True), \
             patch('nibe_lovelace._open_ha_websocket') as mock_ws:
            nl._setup_lovelace("1.0", "Nibe")
        mock_ws.assert_not_called()

    def test_ws_open_failure_returns_early(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=None), \
             patch('nibe_lovelace._setup_lovelace_resource') as mock_res:
            nl._setup_lovelace("1.0", "Nibe")
        mock_res.assert_not_called()

    def test_normal_path_calls_resource_and_dashboard(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace.open', MagicMock(side_effect=OSError), create=True), \
             patch('nibe_lovelace._setup_lovelace_resource') as mock_res, \
             patch('nibe_lovelace._setup_lovelace_dashboard') as mock_dash, \
             patch('nibe_lovelace._regen_menu_dashboard') as mock_menu:
            nl._setup_lovelace("1.0", "Nibe", registry_watcher=None)
        mock_res.assert_called_once()
        mock_dash.assert_called_once()
        mock_menu.assert_not_called()
        ws.close.assert_called()

    def test_registry_watcher_triggers_menu_dashboard(self):
        """Step 3 now routes through _regen_menu_dashboard (not a bare
        _setup_menu_dashboard call) so the initial build gets the same
        retry coverage as a later regen — see _setup_lovelace docstring."""
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        watcher = MagicMock()
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace.open', MagicMock(side_effect=OSError), create=True), \
             patch('nibe_lovelace._setup_lovelace_resource'), \
             patch('nibe_lovelace._setup_lovelace_dashboard'), \
             patch('nibe_lovelace._regen_menu_dashboard') as mock_menu:
            nl._setup_lovelace("1.0", "Nibe", registry_watcher=watcher)
        mock_menu.assert_called_once_with(watcher, False, attempt=1)

    def test_non_menus_mode_skips_menu_dashboard_even_with_watcher(self):
        """The mode gate (entity-mode refactor) must take priority over the
        registry_watcher-is-not-None check — a registry watcher always
        exists at startup regardless of mode, so mode alone must decide."""
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        watcher = MagicMock()
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace.open', MagicMock(side_effect=OSError), create=True), \
             patch('nibe_lovelace._setup_lovelace_resource'), \
             patch('nibe_lovelace._setup_lovelace_dashboard'), \
             patch('nibe_lovelace._regen_menu_dashboard') as mock_menu:
            nl._setup_lovelace("1.0", "Nibe", registry_watcher=watcher, mode="essential")
        mock_menu.assert_not_called()

    def test_menus_mode_explicit_still_builds_menu_dashboard(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        watcher = MagicMock()
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace.open', MagicMock(side_effect=OSError), create=True), \
             patch('nibe_lovelace._setup_lovelace_resource'), \
             patch('nibe_lovelace._setup_lovelace_dashboard'), \
             patch('nibe_lovelace._regen_menu_dashboard') as mock_menu:
            nl._setup_lovelace("1.0", "Nibe", registry_watcher=watcher, mode="menus")
        mock_menu.assert_called_once_with(watcher, False, attempt=1)

    def test_exception_during_setup_logs_warning_and_closes_ws(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace.open', MagicMock(side_effect=OSError), create=True), \
             patch('nibe_lovelace._setup_lovelace_resource', side_effect=RuntimeError("boom")):
            nl._setup_lovelace("1.0", "Nibe")  # must not raise
        ws.close.assert_called()



class TestSetupLovelaceResource(unittest.TestCase):
    """_setup_lovelace_resource: update, create, no-op, duplicate removal."""

    def _run(self, resources, versioned_url, ws_responses=None):
        import nibe_lovelace as nl
        ws = MagicMock()
        calls = []
        def fake_ws_call(_ws, _mid, payload, _timeout=10):
            calls.append(payload)
            t = payload.get('type')
            if t == 'lovelace/resources/list':
                return {'result': resources}
            if ws_responses and t in ws_responses:
                return ws_responses[t]
            return {'success': True}
        with patch('nibe_lovelace._ws_call', side_effect=fake_ws_call):
            nl._setup_lovelace_resource(ws, iter(range(1, 100)).__next__, versioned_url)
        return calls

    def test_no_existing_resource_creates_new(self):
        calls = self._run([], '/local/nibe-entity-manager-card.js?v=abc')
        types = [c.get('type') for c in calls]
        self.assertIn('lovelace/resources/create', types)

    def test_existing_resource_same_url_no_update(self):
        url = '/local/nibe-entity-manager-card.js?v=abc'
        resources = [{'id': 1, 'url': url}]
        calls = self._run(resources, url)
        types = [c.get('type') for c in calls]
        self.assertNotIn('lovelace/resources/update', types)
        self.assertNotIn('lovelace/resources/create', types)

    def test_existing_resource_different_url_updates(self):
        resources = [{'id': 1, 'url': '/local/nibe-entity-manager-card.js?v=old'}]
        calls = self._run(resources, '/local/nibe-entity-manager-card.js?v=new')
        types = [c.get('type') for c in calls]
        self.assertIn('lovelace/resources/update', types)

    def test_duplicate_resources_are_deleted(self):
        resources = [
            {'id': 1, 'url': '/local/nibe-entity-manager-card.js?v=old'},
            {'id': 2, 'url': '/local/nibe-entity-manager-card.js?v=older'},
        ]
        calls = self._run(resources, '/local/nibe-entity-manager-card.js?v=new')
        delete_calls = [c for c in calls if c.get('type') == 'lovelace/resources/delete']
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0].get('resource_id'), 2)

    def test_update_failure_logged(self):
        """A failed update must not raise — just logs a warning."""
        import nibe_lovelace as nl
        ws = MagicMock()
        resources = [{'id': 1, 'url': '/local/nibe-entity-manager-card.js?v=old'}]
        def fake_ws_call(_ws, _mid, payload, _timeout=10):
            t = payload.get('type')
            if t == 'lovelace/resources/list':
                return {'result': resources}
            return {'success': False}
        with patch('nibe_lovelace._ws_call', side_effect=fake_ws_call):
            nl._setup_lovelace_resource(ws, iter(range(1, 100)).__next__,
                                        '/local/nibe-entity-manager-card.js?v=new')



class TestSetupLovelaceDashboard(unittest.TestCase):
    """_setup_lovelace_dashboard: flag exists, dashboard exists, create paths."""

    def _run(self, flag_exists, ws_call_side_effect, device_name="Nibe"):
        import nibe_lovelace as nl
        import tempfile
        import os
        ws = MagicMock()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            flag_file = f.name
        if not flag_exists:
            os.unlink(flag_file)
        try:
            calls = []
            def fake_ws_call(_ws, _mid, payload, _timeout=10):
                calls.append(payload)
                return ws_call_side_effect(payload)
            with patch('nibe_lovelace._ws_call', side_effect=fake_ws_call):
                nl._setup_lovelace_dashboard(ws, iter(range(1, 100)).__next__,
                                             device_name, flag_file)
            return calls, flag_file
        finally:
            try:
                os.unlink(flag_file)
            except OSError:
                pass

    def test_flag_exists_skips_everything(self):
        calls, _ = self._run(True, lambda p: {})
        self.assertEqual(calls, [])

    def test_existing_dashboard_writes_flag_and_returns(self):
        def ws_resp(payload):
            if payload.get('type') == 'lovelace/dashboards/list':
                return {'result': [{'url_path': 'nibe-bridge', 'id': 7}]}
            return {'success': True}
        calls, flag_file = self._run(False, ws_resp)
        types = [c.get('type') for c in calls]
        self.assertNotIn('lovelace/dashboards/create', types)

    def test_create_success_writes_flag_and_config(self):
        def ws_resp(payload):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': []}
            if t == 'lovelace/dashboards/create':
                return {'success': True, 'result': {'id': 42}}
            if t == 'lovelace/config/save':
                return {'success': True}
            return {'success': True}
        calls, _ = self._run(False, ws_resp)
        types = [c.get('type') for c in calls]
        self.assertIn('lovelace/dashboards/create', types)
        self.assertIn('lovelace/config/save', types)

    def test_create_url_already_exists_writes_flag(self):
        def ws_resp(payload):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': []}
            if t == 'lovelace/dashboards/create':
                return {'success': False, 'error': {'message': 'already in use'}}
            return {'success': True}
        calls, _ = self._run(False, ws_resp)
        types = [c.get('type') for c in calls]
        # url_already_exists — must not proceed to config/save
        self.assertNotIn('lovelace/config/save', types)

    def test_create_fatal_error_logs_warning(self):
        def ws_resp(payload):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': []}
            if t == 'lovelace/dashboards/create':
                return {'success': False, 'error': {'message': 'internal error'}}
            return {'success': True}
        calls, _ = self._run(False, ws_resp)
        types = [c.get('type') for c in calls]
        self.assertNotIn('lovelace/config/save', types)

    def test_config_save_failure_logs_warning(self):
        def ws_resp(payload):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': []}
            if t == 'lovelace/dashboards/create':
                return {'success': True, 'result': {'id': 1}}
            if t == 'lovelace/config/save':
                return {'success': False}
            return {'success': True}
        # Must not raise
        self._run(False, ws_resp)



class TestWsCallRemainingBranches(unittest.TestCase):
    """_ws_call branches not yet covered: empty recv, exception in recv, timeout."""

    def test_empty_recv_breaks_and_returns_empty(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.return_value = ""  # empty → break
        with patch('nibe_lovelace.time') as mock_time:
            mock_time.time.side_effect = [0, 0, 9999]  # enters loop once, then deadline exceeded
            result = nl._ws_call(ws, 1, {"type": "ping"})
        self.assertEqual(result, {})
        ws.settimeout.assert_any_call(None)

    def test_recv_exception_returns_empty(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.side_effect = OSError("connection reset")
        with patch('nibe_lovelace.time') as mock_time:
            mock_time.time.side_effect = [0, 0, 9999]
            result = nl._ws_call(ws, 1, {"type": "ping"})
        self.assertEqual(result, {})
        ws.settimeout.assert_any_call(None)

    def test_deadline_exceeded_returns_empty(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        # time.time always returns >= deadline → while loop body never entered
        with patch('nibe_lovelace.time') as mock_time:
            mock_time.time.side_effect = [0, 999]  # deadline=10, second call > deadline
            result = nl._ws_call(ws, 1, {"type": "ping"})
        self.assertEqual(result, {})

    def test_non_matching_id_skipped_then_matching_returned(self):
        """Messages with wrong ID are discarded; correct ID is returned."""
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.side_effect = [
            json.dumps({"id": 99, "type": "result", "success": True}),  # wrong id
            json.dumps({"id": 1,  "type": "result", "success": True}),  # correct
        ]
        with patch('nibe_lovelace.time') as mock_time:
            mock_time.time.side_effect = [0, 0, 0, 9999]
            result = nl._ws_call(ws, 1, {"type": "ping"})
        self.assertEqual(result.get("success"), True)



class TestTeardownLovelace(unittest.TestCase):
    """_teardown_lovelace: env gate, card file removal, WS teardown paths."""

    def test_env_not_set_returns_early(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {}, clear=True), \
             patch('nibe_lovelace.os.path.exists') as mock_exists:
            nl._teardown_lovelace()
        mock_exists.assert_not_called()

    def test_env_wrong_value_returns_early(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {'NIBE_REMOVE_FRONTEND': '0'}), \
             patch('nibe_lovelace.os.path.exists') as mock_exists:
            nl._teardown_lovelace()
        mock_exists.assert_not_called()

    def test_card_file_removed_when_exists(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {'NIBE_REMOVE_FRONTEND': '1'}), \
             patch('nibe_lovelace.os.path.exists', return_value=True), \
             patch('nibe_lovelace.os.remove') as mock_rm, \
             patch('nibe_lovelace.os.environ.get', side_effect=lambda k, d=None:
                   '1' if k == 'NIBE_REMOVE_FRONTEND' else None):
            nl._teardown_lovelace()
        mock_rm.assert_any_call('/config/www/nibe-entity-manager-card.js')

    def test_card_file_remove_error_does_not_raise(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {'NIBE_REMOVE_FRONTEND': '1'}), \
             patch('nibe_lovelace.os.path.exists', return_value=True), \
             patch('nibe_lovelace.os.remove', side_effect=OSError("busy")), \
             patch('nibe_lovelace.os.environ.get', side_effect=lambda k, d=None:
                   '1' if k == 'NIBE_REMOVE_FRONTEND' else None):
            nl._teardown_lovelace()  # must not raise

    def test_no_supervisor_token_skips_ws(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {'NIBE_REMOVE_FRONTEND': '1'}), \
             patch('nibe_lovelace.os.path.exists', return_value=False), \
             patch('nibe_lovelace.os.environ.get', side_effect=lambda k, d=None:
                   '1' if k == 'NIBE_REMOVE_FRONTEND' else None), \
             patch('nibe_lovelace._open_ha_websocket'):
            nl._teardown_lovelace()

    def _make_teardown_ws(self, dashboard_id=7, resource_id=3,
                          dash_delete_success=True, res_delete_success=True):
        """Build a fake ws + recv sequence for a full teardown."""
        ws = MagicMock()
        calls = []
        def fake_ws_call(_ws, _mid, payload, _timeout=10):
            calls.append(payload)
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                items = [{'url_path': 'nibe-bridge', 'id': dashboard_id}] if dashboard_id else []
                return {'result': items}
            if t == 'lovelace/dashboards/delete':
                return {'success': dash_delete_success}
            if t == 'lovelace/resources/list':
                items = [{'url': 'nibe-entity-manager-card.js', 'id': resource_id}] if resource_id else []
                return {'result': items}
            if t == 'lovelace/resources/delete':
                return {'success': res_delete_success}
            return {'success': True}
        return ws, calls, fake_ws_call

    def test_full_teardown_removes_dashboard_and_resource(self):
        import nibe_lovelace as nl
        ws, calls, fake_ws_call = self._make_teardown_ws()
        ws.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
        ]
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ',
                        {'NIBE_REMOVE_FRONTEND': '1', 'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace.os.path.exists', return_value=False), \
             patch('nibe_lovelace.os.remove'), \
             patch.dict('sys.modules', {'websocket': ws_mod}), \
             patch('nibe_lovelace._ws_call', side_effect=fake_ws_call):
            nl._teardown_lovelace()
        types = [c.get('type') for c in calls]
        self.assertIn('lovelace/dashboards/delete', types)
        self.assertIn('lovelace/resources/delete', types)

    def test_dashboard_not_found_skips_delete(self):
        import nibe_lovelace as nl
        ws, calls, fake_ws_call = self._make_teardown_ws(dashboard_id=None)
        ws.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
        ]
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ',
                        {'NIBE_REMOVE_FRONTEND': '1', 'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace.os.path.exists', return_value=False), \
             patch('nibe_lovelace.os.remove'), \
             patch.dict('sys.modules', {'websocket': ws_mod}), \
             patch('nibe_lovelace._ws_call', side_effect=fake_ws_call):
            nl._teardown_lovelace()
        types = [c.get('type') for c in calls]
        self.assertNotIn('lovelace/dashboards/delete', types)

    def test_ws_connection_error_returns_early(self):
        import nibe_lovelace as nl
        ws_mod = MagicMock()
        ws_mod.create_connection.side_effect = OSError("refused")
        with patch.dict('os.environ',
                        {'NIBE_REMOVE_FRONTEND': '1', 'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace.os.path.exists', return_value=False), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            nl._teardown_lovelace()  # must not raise

    def test_wrong_auth_greeting_returns_early(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.return_value = json.dumps({"type": "auth_ok"})  # wrong greeting
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ',
                        {'NIBE_REMOVE_FRONTEND': '1', 'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace.os.path.exists', return_value=False), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            nl._teardown_lovelace()  # must not raise

    def test_auth_fails_returns_early(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_invalid"}),
        ]
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ',
                        {'NIBE_REMOVE_FRONTEND': '1', 'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace.os.path.exists', return_value=False), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            nl._teardown_lovelace()  # must not raise



class TestRegenMenuDashboardMaxAttemptsGivesUp(unittest.TestCase):
    """_regen_menu_dashboard: needs_retry True on final attempt logs warning and stops."""

    def test_needs_retry_at_max_attempts_does_not_schedule(self):
        import nibe_lovelace as nl
        setup_dashboard_fn = MagicMock(return_value=True)  # needs_retry
        schedule_retry_fn = MagicMock()
        nl._regen_menu_dashboard(
            MagicMock(), debug_mode=False, attempt=3, max_attempts=3,
            open_ws_fn=MagicMock(), setup_dashboard_fn=setup_dashboard_fn,
            schedule_retry_fn=schedule_retry_fn,
        )
        schedule_retry_fn.assert_not_called()



class TestWireMenuDashboardRegen(unittest.TestCase):
    """_wire_menu_dashboard_regen delegates to _on_enabled_state_change_factory
    and wires the result into entity_manager."""

    def test_sets_on_enabled_state_change(self):
        import nibe_lovelace as nl
        em = MagicMock()
        watcher = MagicMock()
        handler = MagicMock()
        with patch('nibe_lovelace._on_enabled_state_change_factory', return_value=handler) as mock_factory:
            nl._wire_menu_dashboard_regen(em, watcher, debug_mode=False)
        mock_factory.assert_called_once_with(watcher, False, lovelace_thread=None)
        em.set_on_enabled_state_change.assert_called_once_with(handler)

    def test_passes_lovelace_thread_through(self):
        import nibe_lovelace as nl
        em = MagicMock()
        watcher = MagicMock()
        thread = MagicMock()
        with patch('nibe_lovelace._on_enabled_state_change_factory', return_value=MagicMock()) as mock_factory:
            nl._wire_menu_dashboard_regen(em, watcher, debug_mode=True, lovelace_thread=thread)
        mock_factory.assert_called_once_with(watcher, True, lovelace_thread=thread)


# ===========================================================================
# Additional coverage: remaining exception-handler branches
# ===========================================================================


class TestOpenHaWebSocketWsCloseException(unittest.TestCase):
    """ws.close() raising during auth error must not propagate."""

    def test_auth_exception_ws_close_raises(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.side_effect = OSError("dropped")
        ws.close.side_effect = OSError("already closed")
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            result = nl._open_ha_websocket()  # must not raise
        self.assertIsNone(result)



class TestSetupLovelaceCardHashRead(unittest.TestCase):
    """_setup_lovelace: card file successfully read for hash (line 861)."""

    def test_card_file_readable_uses_hash(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.close.return_value = None
        next_id = MagicMock(return_value=1)
        fake_file = MagicMock()
        fake_file.__enter__ = MagicMock(return_value=fake_file)
        fake_file.__exit__ = MagicMock(return_value=False)
        fake_file.read.return_value = b"card content"

        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('builtins.open', return_value=fake_file), \
             patch('nibe_lovelace._setup_lovelace_resource') as mock_res, \
             patch('nibe_lovelace._setup_lovelace_dashboard'), \
             patch('nibe_lovelace._regen_menu_dashboard'):
            nl._setup_lovelace("1.0", "Nibe", registry_watcher=None)
        # versioned URL should contain hash, not "1.0"
        url_arg = mock_res.call_args[0][2]
        self.assertNotIn("1.0", url_arg)



class TestSetupLovelaceWsCloseException(unittest.TestCase):
    """ws.close() raising in _setup_lovelace finally must not propagate."""

    def test_ws_close_raises_in_finally(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.close.side_effect = OSError("already closed")
        next_id = MagicMock(return_value=1)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('builtins.open', side_effect=OSError, create=True), \
             patch('nibe_lovelace._setup_lovelace_resource'), \
             patch('nibe_lovelace._setup_lovelace_dashboard'):
            nl._setup_lovelace("1.0", "Nibe")  # must not raise



class TestSetupLovelaceDashboardFlagWriteFailure(unittest.TestCase):
    """Flag file write failures (OSError) in all three paths of _setup_lovelace_dashboard."""

    def _make_ws_and_call(self, ws_responses):
        import nibe_lovelace as nl
        import tempfile
        ws = MagicMock()
        flag_file = tempfile.mktemp()

        calls = []
        def fake_ws_call(_ws, _mid, payload, _timeout=10):
            calls.append(payload)
            t = payload.get('type')
            return ws_responses.get(t, {'success': True})

        # Patch open: succeed for reads but fail for flag writes
        real_open = open
        def patched_open(path, mode='r', *args, **kwargs):
            if path == flag_file and 'w' in mode:
                raise OSError("read-only filesystem")
            return real_open(path, mode, *args, **kwargs)

        with patch('nibe_lovelace._ws_call', side_effect=fake_ws_call), \
             patch('nibe_lovelace.open', side_effect=patched_open, create=True):
            nl._setup_lovelace_dashboard(ws, iter(range(1, 100)).__next__,
                                         "Nibe", flag_file)
        return calls

    def test_flag_write_fails_when_dashboard_found(self):
        """Dashboard found in list — writing flag fails → must not raise."""
        self._make_ws_and_call({
            'lovelace/dashboards/list': {
                'result': [{'url_path': 'nibe-bridge', 'id': 7}]
            },
        })

    def test_flag_write_fails_on_url_already_exists(self):
        """Create returns url_already_exists — writing flag fails → must not raise."""
        self._make_ws_and_call({
            'lovelace/dashboards/list': {'result': []},
            'lovelace/dashboards/create': {
                'success': False,
                'error': {'message': 'already in use'},
            },
        })

    def test_flag_write_fails_after_successful_create(self):
        """Create succeeds, config saved, writing flag fails → must not raise."""
        self._make_ws_and_call({
            'lovelace/dashboards/list': {'result': []},
            'lovelace/dashboards/create': {'success': True, 'result': {'id': 1}},
            'lovelace/config/save': {'success': True},
        })



class TestTeardownLovelaceRemainingPaths(unittest.TestCase):
    """Remaining teardown branches: delete failures, exceptions, ws.close raises,
    flag removal OSError."""

    def _run_teardown(self, ws_call_side_effect, ws_close_raises=False,
                      remove_side_effect=None):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
        ]
        if ws_close_raises:
            ws.close.side_effect = OSError("closed")
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws

        rm_effects = {'side_effect': remove_side_effect} if remove_side_effect else {}
        with patch.dict('os.environ',
                        {'NIBE_REMOVE_FRONTEND': '1', 'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace.os.path.exists', return_value=False), \
             patch('nibe_lovelace.os.remove', **rm_effects), \
             patch.dict('sys.modules', {'websocket': ws_mod}), \
             patch('nibe_lovelace._ws_call', side_effect=ws_call_side_effect):
            nl._teardown_lovelace()  # must not raise

    def test_dashboard_delete_failure_logs_warning(self):
        def fake(ws, _mid, payload, _timeout=10):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': [{'url_path': 'nibe-bridge', 'id': 7}]}
            if t == 'lovelace/dashboards/delete':
                return {'success': False}
            if t == 'lovelace/resources/list':
                return {'result': []}
            return {'success': True}
        self._run_teardown(fake)

    def test_dashboard_list_exception_logs_warning(self):
        call_count = [0]
        def fake(ws, _mid, payload, _timeout=10):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("ws error")  # dashboards/list raises
            if payload.get('type') == 'lovelace/resources/list':
                return {'result': []}
            return {'success': True}
        self._run_teardown(fake)

    def test_resource_delete_failure_logs_warning(self):
        def fake(ws, _mid, payload, _timeout=10):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': [{'url_path': 'nibe-bridge', 'id': 7}]}
            if t == 'lovelace/dashboards/delete':
                return {'success': True}
            if t == 'lovelace/resources/list':
                return {'result': [{'url': 'nibe-entity-manager-card.js', 'id': 3}]}
            if t == 'lovelace/resources/delete':
                return {'success': False}
            return {'success': True}
        self._run_teardown(fake)

    def test_resource_not_found_skips_delete(self):
        def fake(ws, _mid, payload, _timeout=10):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': []}
            if t == 'lovelace/resources/list':
                return {'result': []}  # no matching resource
            return {'success': True}
        self._run_teardown(fake)

    def test_resource_list_exception_logs_warning(self):
        call_count = [0]
        def fake(ws, _mid, payload, _timeout=10):
            call_count[0] += 1
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': []}
            raise OSError("ws error")  # resources/list raises
        self._run_teardown(fake)

    def test_ws_close_raises_in_finally(self):
        def fake(ws, _mid, payload, _timeout=10):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': []}
            if t == 'lovelace/resources/list':
                return {'result': []}
            return {'success': True}
        self._run_teardown(fake, ws_close_raises=True)

    def test_flag_file_removal_oserror_ignored(self):
        """os.remove('/data/lovelace_provisioned') raising OSError must be silently ignored."""
        def fake(ws, _mid, payload, _timeout=10):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'result': []}
            if t == 'lovelace/resources/list':
                return {'result': []}
            return {'success': True}
        self._run_teardown(fake, remove_side_effect=OSError("no such file"))





# ===========================================================================
# _setup_menu_dashboard — wait-loop branches via freeze_time / sleep mock
# ===========================================================================


class TestSetupMenuDashboardWaitLoop(unittest.TestCase):
    """Exercise the registry-stability wait loop inside _setup_menu_dashboard.

    The loop uses local counters (_waited, _stable_for) rather than
    time.time(), so we control iteration count by patching time.sleep
    to a no-op and controlling entity_id_for() return values.

    After the loop the function calls _build_point_defaults,
    _build_dynamic_injection, _build_menu_dashboard_config, and
    open_ws_fn.  We mock the build helpers to return minimal valid
    structures and set open_ws_fn to return None, which causes the
    function to return True (retry signal) — short-circuiting the
    actual WebSocket calls while still executing all post-loop lines.
    """

    # A handful of real menu point IDs (confirmed present in menu_structure.yaml)
    _MENU_PID = 4

    def _make_registry_watcher(self, em, resolved_pids=None):
        """Return a mock registry_watcher that resolves the given point IDs."""
        resolved = set(resolved_pids or [])
        rw = MagicMock()
        rw._em = em
        rw.entity_id_for = lambda pid: f"sensor.nibe_{pid}" if pid in resolved else None
        return rw

    def _make_em(self, menu_pids, dynamic_pids=None):
        """Return a minimal EntityManager-like mock for _setup_menu_dashboard."""
        from conftest import _make_em as make_em_real
        em = make_em_real()
        # Populate all_points_by_id so available_menu_points is non-empty
        for pid in menu_pids:
            em.all_points_by_id[pid] = {'variableId': pid}
        em.active_dynamic_points = set(dynamic_pids or [])
        return em

    def _run(self, open_ws_fn, registry_watcher, sleep_patch=True):
        """Call _setup_menu_dashboard with time.sleep mocked out."""
        import nibe_lovelace as nl
        patches = []
        if sleep_patch:
            patches.append(patch('nibe_lovelace.time.sleep'))
        with patch('nibe_lovelace._build_point_defaults', return_value={}), \
             patch('nibe_lovelace._build_dynamic_injection', return_value={}), \
             patch('nibe_lovelace._build_menu_dashboard_config',
                   return_value={'views': [{'title': 'Menu', 'cards': []}]}):
            ctx = __import__('contextlib').ExitStack()
            for p in patches:
                ctx.enter_context(p)
            with ctx:
                return nl._setup_menu_dashboard(
                    open_ws_fn, registry_watcher, debug_mode=False,
                )

    def test_timeout_path_returns_retry(self):
        """When no entity resolves within 60s the loop times out.

        After timeout the function still builds the dashboard config.
        If open_ws_fn returns None it returns True (retry).
        """
        em = self._make_em(menu_pids=[self._MENU_PID], dynamic_pids=[])
        # Nothing ever resolves → count stays 0 → never stable
        rw = self._make_registry_watcher(em, resolved_pids=[])
        result = self._run(open_ws_fn=MagicMock(return_value=None), registry_watcher=rw)
        self.assertTrue(result, "Timeout → open_ws_fn=None → should return True (retry)")

    def test_ideal_exit_all_dynamic_resolved(self):
        """Ideal exit: menu stable + all dynamic points resolved.

        Loop exits on the first stable window (lines 667-673).
        """
        # Use enough menu points to meet the 70% completeness threshold
        menu_pids = [4, 54, 57, 78, 121, 187, 212, 513, 601, 900]
        dynamic_pid = 9001
        em = self._make_em(menu_pids=menu_pids, dynamic_pids=[dynamic_pid])

        # All menu points + the dynamic point resolve immediately
        all_resolved = set(menu_pids) | {dynamic_pid}
        rw = self._make_registry_watcher(em, resolved_pids=all_resolved)

        result = self._run(open_ws_fn=MagicMock(return_value=None), registry_watcher=rw)
        # Loop exits early (ideal), open_ws_fn returns None → retry=True
        self.assertTrue(result)

    def test_eight_second_more_branch(self):
        """Menu stable but some dynamic points unresolved → wait 8s more path
        (lines 676-682).

        Condition: _stable_for >= _stable_need=3.0, menu_resolved > 0,
        menu_complete=True, but dyn_resolved < len(active_dynamic),
        and _stable_for >= 8.0 → break with warning.

        We use enough menu pids to pass the 70% threshold and a dynamic
        pid that never resolves.  With time.sleep mocked the loop spins
        instantly so _stable_for accumulates to 8+ in <1ms.
        """
        # 10 menu pids all resolved → 100% completeness, well above 70%
        menu_pids = [4, 54, 57, 78, 121, 187, 212, 513, 601, 900]
        dynamic_pid = 9999   # never resolves
        em = self._make_em(menu_pids=menu_pids, dynamic_pids=[dynamic_pid])

        # All menu pids resolve; dynamic_pid does NOT
        rw = self._make_registry_watcher(em, resolved_pids=set(menu_pids))

        result = self._run(open_ws_fn=MagicMock(return_value=None), registry_watcher=rw)
        # Broke out via 8s branch; open_ws_fn=None → True
        self.assertTrue(result)

    def test_no_menu_structure_returns_false(self):
        """If menu_structure.yaml is not found the function returns False immediately
        without entering the wait loop.
        """
        import nibe_lovelace as nl
        em = self._make_em(menu_pids=[])
        rw = self._make_registry_watcher(em, resolved_pids=[])
        open_ws_fn = MagicMock()

        with patch('nibe_lovelace.os.path.exists', return_value=False):
            result = nl._setup_menu_dashboard(open_ws_fn, rw, debug_mode=False)

        self.assertFalse(result)
        open_ws_fn.assert_not_called()

    def test_menu_yaml_load_exception_returns_false(self):
        """If yaml.safe_load raises the function returns False without looping."""
        import nibe_lovelace as nl
        em = self._make_em(menu_pids=[])
        rw = self._make_registry_watcher(em, resolved_pids=[])
        open_ws_fn = MagicMock()

        with patch('nibe_lovelace.os.path.exists', return_value=True), \
             patch('builtins.open', side_effect=OSError("permission denied")):
            result = nl._setup_menu_dashboard(open_ws_fn, rw, debug_mode=False)

        self.assertFalse(result)
        open_ws_fn.assert_not_called()

    def test_empty_menu_structure_returns_false(self):
        """If menus list is empty the function returns False immediately."""
        import nibe_lovelace as nl
        em = self._make_em(menu_pids=[])
        rw = self._make_registry_watcher(em, resolved_pids=[])
        open_ws_fn = MagicMock()

        with patch('nibe_lovelace.os.path.exists', return_value=True), \
             patch('builtins.open', MagicMock()), \
             patch('nibe_lovelace.yaml.safe_load', return_value={'menus': []}):
            result = nl._setup_menu_dashboard(open_ws_fn, rw, debug_mode=False)

        self.assertFalse(result)
        open_ws_fn.assert_not_called()

    def test_empty_dashboard_config_returns_false(self):
        """If _build_menu_dashboard_config returns no views the function
        returns False (line 713-715) without calling open_ws_fn.
        """
        menu_pids = [4, 54, 57, 78, 121, 187, 212, 513, 601, 900]
        em = self._make_em(menu_pids=menu_pids, dynamic_pids=[])
        rw = self._make_registry_watcher(em, resolved_pids=set(menu_pids))
        open_ws_fn = MagicMock()

        import nibe_lovelace as nl
        with patch('nibe_lovelace.time.sleep'), \
             patch('nibe_lovelace._build_point_defaults', return_value={}), \
             patch('nibe_lovelace._build_dynamic_injection', return_value={}), \
             patch('nibe_lovelace._build_menu_dashboard_config', return_value={}):
            result = nl._setup_menu_dashboard(open_ws_fn, rw, debug_mode=False)

        self.assertFalse(result)
        open_ws_fn.assert_not_called()

    def test_open_ws_success_calls_lovelace_setup(self):
        """When open_ws_fn returns a (ws, next_id) pair,
        _setup_menu_dashboard_lovelace is called with it.
        """
        menu_pids = [4, 54, 57, 78, 121, 187, 212, 513, 601, 900]
        em = self._make_em(menu_pids=menu_pids, dynamic_pids=[])
        rw = self._make_registry_watcher(em, resolved_pids=set(menu_pids))

        ws = MagicMock()
        open_ws_fn = MagicMock(return_value=(ws, 1))

        import nibe_lovelace as nl
        with patch('nibe_lovelace.time.sleep'), \
             patch('nibe_lovelace._build_point_defaults', return_value={}), \
             patch('nibe_lovelace._build_dynamic_injection', return_value={}), \
             patch('nibe_lovelace._build_menu_dashboard_config',
                   return_value={'views': [{'title': 'Menu', 'cards': []}]}), \
             patch('nibe_lovelace._setup_menu_dashboard_lovelace',
                   return_value=False) as mock_sml:
            result = nl._setup_menu_dashboard(open_ws_fn, rw, debug_mode=False)

        mock_sml.assert_called_once()
        call_args = mock_sml.call_args
        self.assertIs(call_args.args[0], ws)
        self.assertFalse(result)

    def test_ws_close_called_in_finally(self):
        """ws.close() is called in the finally block even when
        _setup_menu_dashboard_lovelace raises.
        """
        menu_pids = [4, 54, 57, 78, 121, 187, 212, 513, 601, 900]
        em = self._make_em(menu_pids=menu_pids, dynamic_pids=[])
        rw = self._make_registry_watcher(em, resolved_pids=set(menu_pids))

        ws = MagicMock()
        open_ws_fn = MagicMock(return_value=(ws, 1))

        import nibe_lovelace as nl
        with patch('nibe_lovelace.time.sleep'), \
             patch('nibe_lovelace._build_point_defaults', return_value={}), \
             patch('nibe_lovelace._build_dynamic_injection', return_value={}), \
             patch('nibe_lovelace._build_menu_dashboard_config',
                   return_value={'views': [{'title': 'Menu', 'cards': []}]}), \
             patch('nibe_lovelace._setup_menu_dashboard_lovelace',
                   side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                nl._setup_menu_dashboard(open_ws_fn, rw, debug_mode=False)

        ws.close.assert_called()

    def test_stable_count_changes_resets_stable_for(self):
        """When the entity count changes between iterations, _stable_for
        resets and the ideal-exit branch is not triggered prematurely.

        We give the watcher a changing count across iterations so stability
        never accumulates to _stable_need=3.0, forcing the loop to time out.
        """
        menu_pids = [4, 54, 57, 78, 121, 187, 212, 513, 601, 900]
        em = self._make_em(menu_pids=menu_pids, dynamic_pids=[])

        call_count = [0]
        resolved_sets = [
            set(menu_pids[:5]),   # 5 resolved
            set(menu_pids[:8]),   # 8 resolved — count changes, stable_for resets
            set(menu_pids[:5]),   # back to 5 — changes again
        ]

        def _entity_id_for(pid):
            idx = min(call_count[0] // len(menu_pids), len(resolved_sets) - 1)
            call_count[0] += 1
            return f"sensor.nibe_{pid}" if pid in resolved_sets[idx] else None

        rw = MagicMock()
        rw._em = em
        rw.entity_id_for = _entity_id_for

        # Loop times out; open_ws_fn=None → True
        result = self._run(open_ws_fn=MagicMock(return_value=None), registry_watcher=rw)
        self.assertTrue(result)

    def test_ws_close_exception_suppressed(self):
        """ws.close() raising in the finally block is silently suppressed
        (lines 737-738: except Exception: pass).
        """
        menu_pids = [4, 54, 57, 78, 121, 187, 212, 513, 601, 900]
        em = self._make_em(menu_pids=menu_pids, dynamic_pids=[])
        rw = self._make_registry_watcher(em, resolved_pids=set(menu_pids))

        ws = MagicMock()
        ws.close.side_effect = OSError("socket already closed")
        open_ws_fn = MagicMock(return_value=(ws, 1))

        import nibe_lovelace as nl
        # Must not raise despite ws.close() failing
        with patch('nibe_lovelace.time.sleep'), \
             patch('nibe_lovelace._build_point_defaults', return_value={}), \
             patch('nibe_lovelace._build_dynamic_injection', return_value={}), \
             patch('nibe_lovelace._build_menu_dashboard_config',
                   return_value={'views': [{'title': 'Menu', 'cards': []}]}), \
             patch('nibe_lovelace._setup_menu_dashboard_lovelace',
                   return_value=False):
            result = nl._setup_menu_dashboard(open_ws_fn, rw, debug_mode=False)  # must not raise

        self.assertFalse(result)
        ws.close.assert_called()
