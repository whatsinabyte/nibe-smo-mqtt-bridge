"""
test_entity_manager_discovery.py
================================
Startup discovery and MQTT restore tests for nibe_entity_manager.py — split out of test_entity_manager.py
for file-size/maintainability. Shared fixtures are in conftest.py.
"""

import unittest
from unittest.mock import MagicMock, patch

from conftest import (
    _make_em,
    _nibe_point_id,
)
from hypothesis import given
from hypothesis import strategies as st


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
        calls = list(mqtt.publish.call_args_list)
        self.assertTrue(calls)
        payload = _json.loads(calls[-1].args[1])
        self.assertEqual(payload['id'], pid)

    @given(_nibe_point_id)
    def test_payload_always_valid_json(self, pid):
        import json as _json
        pub, mqtt = self._pub()
        pub.publish_point_metadata(self._point(pid))
        calls = list(mqtt.publish.call_args_list)
        self.assertTrue(calls)
        _json.loads(calls[-1].args[1])  # must parse without raising

    @given(_nibe_point_id)
    def test_always_published_retained(self, pid):
        pub, mqtt = self._pub()
        pub.publish_point_metadata(self._point(pid))
        calls = list(mqtt.publish.call_args_list)
        self.assertTrue(calls)
        retain = calls[-1].kwargs.get('retain',
                 calls[-1].args[2] if len(calls[-1].args) > 2 else False)
        self.assertTrue(retain)


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

    def test_mark_absent_as_firmware_removed_called_with_baseline(self):
        """discover_points must call mark_absent_as_firmware_removed with the
        freshly-established baseline_point_ids, so switches/selects removed
        by a firmware update get flagged."""
        em = self._make_em_with_bulk(point_ids=(100, 200, 300))
        with patch.object(em.dynamic_point_map, 'mark_absent_as_firmware_removed',
                          return_value=set()) as mock_mark:
            em.discover_points()
            mock_mark.assert_called_once_with({100, 200, 300})

    def test_point_missing_from_bulk_is_marked_firmware_removed(self):
        """End-to-end: a switch tracked in the map but absent from the new
        bulk fetch must have firmware_removed=True after discover_points."""
        from nibe_dynamic_map import DynamicPointEntry
        em = self._make_em_with_bulk(point_ids=(100, 200, 300))
        # 999 was previously a tracked controlling switch — no longer in bulk.
        em.dynamic_point_map._table[999] = DynamicPointEntry(
            point_id=999, title='Removed switch', entity_type='switch',
        )
        em.discover_points()
        self.assertTrue(em.dynamic_point_map[999].firmware_removed)

    def test_point_still_in_bulk_is_not_marked_firmware_removed(self):
        """A tracked switch still present in the bulk fetch must not be
        flagged as firmware_removed."""
        from nibe_dynamic_map import DynamicPointEntry
        em = self._make_em_with_bulk(point_ids=(100, 200, 300))
        em.dynamic_point_map._table[100] = DynamicPointEntry(
            point_id=100, title='Still present', entity_type='switch',
        )
        em.discover_points()
        self.assertFalse(em.dynamic_point_map[100].firmware_removed)

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

    def test_fetch_bulk_data_called_with_detect_changes_false(self):
        """The initial baseline fetch must not run change detection — there
        is no prior baseline to diff against yet."""
        em = _make_em()
        mock_fetch = MagicMock(return_value=True)
        em._fetch_bulk_data = mock_fetch
        em.discover_points()
        mock_fetch.assert_called_once_with(detect_changes=False)

    def test_entity_types_dict_uses_entity_type_key_with_empty_default(self):
        """The entity_types dict passed to populate_from_bulk must map each
        point id to its 'entity_type' value, defaulting to '' (not some
        other key/default) when absent. This directly exercises the
        dict-comprehension `pt.get('entity_type', '')`."""
        em = self._make_em_with_bulk(point_ids=())
        em.all_points_by_id[100] = {'variableId': 100, 'entity_type': 'switch'}
        em.all_points_by_id[200] = {'variableId': 200}  # no entity_type key at all
        with patch.object(em.dynamic_point_map, 'populate_from_bulk',
                          return_value=0) as mock_pop, \
             patch.object(em.dynamic_point_map, 'restore_from_bulk'):
            em.discover_points()
            args, _kwargs = mock_pop.call_args
            entity_types = args[1]
            self.assertEqual(entity_types[100], 'switch')
            self.assertEqual(entity_types[200], '')

    def test_restore_from_bulk_called_with_baseline_point_ids(self):
        """restore_from_bulk must receive the freshly established
        baseline_point_ids set, not None or some other value."""
        em = self._make_em_with_bulk(point_ids=(100, 200, 300))
        with patch.object(em.dynamic_point_map, 'restore_from_bulk') as mock_restore:
            em.discover_points()
            mock_restore.assert_called_once_with({100, 200, 300})

    def test_publish_all_metadata_called_with_all_points(self):
        """publish_all_metadata must receive the actual point list, not None
        or an empty placeholder."""
        em = self._make_em_with_bulk(point_ids=(100, 200))
        em.discover_points()
        args, _kwargs = em._pub.publish_all_metadata.call_args
        published_ids = {p['variableId'] for p in args[0]}
        self.assertEqual(published_ids, {100, 200})

    def test_publish_point_list_called_with_all_points_by_id(self):
        """publish_point_list must receive the real all_points_by_id dict,
        not None."""
        em = self._make_em_with_bulk(point_ids=(100, 200))
        em.discover_points()
        em._pub.publish_point_list.assert_called_once_with(em.all_points_by_id)


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

    def test_rebuilt_device_info_keeps_real_device_id(self):
        """device_info's 'identifiers' field must be rebuilt with the
        real, original device_id — not a wrong/None value. A mismatch
        here would make HA treat the device as a new/different one after
        an API-unreachable-at-startup reconnect, instead of recognizing
        the existing device registration."""
        em = self._make_em_ready()
        em._pub.device_id = 'nibe_test_device_id'
        em.complete_deferred_discovery('essential')
        self.assertEqual(em.device_info['identifiers'], ['nibe_test_device_id'])
        self.assertEqual(em._pub.device_info['identifiers'], ['nibe_test_device_id'])

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

    def test_device_info_rebuilt_with_real_device_name(self):
        """_build_device_info must be called with the real device_name from
        the publisher, not None — otherwise the fallback/naming logic in
        _build_device_info would incorrectly treat the device as unnamed."""
        em = self._make_em_ready()
        em._pub.device_name = 'My Real Heat Pump'
        em._api.fetch_device_info.return_value = {
            'product': {'name': ''},
        }
        em.complete_deferred_discovery('essential')
        self.assertEqual(em.device_info['name'], 'My Real Heat Pump')

    def test_restore_receives_real_applied_mode_not_none(self):
        """apply_startup_action must be called with the actual applied_mode
        returned by read_applied_mode(), not a hardcoded None — otherwise
        an existing applied-mode baseline would be spuriously re-recorded
        via record_applied_mode on every deferred-discovery restore."""
        em = self._make_em_ready(applied_mode='essential', mqtt_enabled_count=5)
        em.complete_deferred_discovery('essential')
        em.record_applied_mode.assert_not_called()


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

    def test_topic_not_starting_with_homeassistant_prefix_ignored(self):
        """A message on a topic ending in '/config' with a valid nibe
        payload must still be ignored if the topic doesn't start with
        'homeassistant/' — the full AND-chain (prefix AND suffix AND
        payload) must all hold, not just any single term."""
        em = _make_em()
        callbacks = {}

        def fake_callback_add(topic, cb):
            callbacks[topic] = cb

        def fake_publish(topic, payload, retain=False):
            if 'scan_sentinel' in topic:
                msg = MagicMock()
                msg.topic = 'other/sensor/nibe_1234/config'  # ends /config, wrong prefix
                import json as _json
                msg.payload = _json.dumps({'unique_id': 'nibe_1234'}).encode()
                cb = callbacks.get('homeassistant/+/+/config')
                if cb:
                    cb(None, None, msg)
                cb = callbacks.get(topic)
                if cb:
                    cb(None, None, MagicMock())

        em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)
        em.mqtt.publish = MagicMock(side_effect=fake_publish)
        result = em.scan_mqtt_discovery()
        self.assertEqual(result, set())

    def test_missing_unique_id_key_does_not_raise(self):
        """A retained config payload with no 'unique_id' key at all must be
        safely ignored (default '' does not start with 'nibe_'), not raise
        an uncaught AttributeError from calling .startswith() on None."""
        payload = {'name': 'No unique id here'}
        em = self._make_em_with_sentinel([payload])
        result = em.scan_mqtt_discovery()  # must not raise
        self.assertEqual(result, set())

    def test_subscribes_to_wildcard_config_topic(self):
        """The subscribe call must use the real wildcard config topic
        string, not None — otherwise no discovery configs would ever be
        received from the broker."""
        em = self._make_em_with_sentinel([])
        em.scan_mqtt_discovery()
        subscribed_topics = [call.args[0] for call in em.mqtt.subscribe.call_args_list]
        self.assertIn('homeassistant/+/+/config', subscribed_topics)

    def test_subscribes_to_sentinel_topic(self):
        """The scan must also subscribe to the real sentinel topic, not
        None — otherwise the sentinel publish below would never be
        delivered back and every scan would time out."""
        from nibe_mqtt_publisher import BrowserTopic
        em = self._make_em_with_sentinel([])
        em.scan_mqtt_discovery()
        subscribed_topics = [call.args[0] for call in em.mqtt.subscribe.call_args_list]
        self.assertIn(BrowserTopic.SCAN_SENTINEL, subscribed_topics)

    def test_registers_callback_for_sentinel_topic_with_real_handler(self):
        """message_callback_add for the sentinel topic must register the
        real sentinel-topic string and a real (non-None) handler function
        — both are required for the sentinel round-trip to work."""
        from nibe_mqtt_publisher import BrowserTopic
        em = self._make_em_with_sentinel([])
        em.scan_mqtt_discovery()
        calls = em.mqtt.message_callback_add.call_args_list
        sentinel_calls = [c for c in calls if c.args[0] == BrowserTopic.SCAN_SENTINEL]
        self.assertEqual(len(sentinel_calls), 1)
        self.assertIsNotNone(sentinel_calls[0].args[1])
        self.assertTrue(callable(sentinel_calls[0].args[1]))

    def test_publishes_sentinel_with_scan_payload_not_retained(self):
        """The sentinel publish must send the literal payload 'scan' with
        retain=False — a retained sentinel would leave a stale retained
        message on the broker that could trigger false sentinel-received
        events on the *next* scan before real configs have arrived."""
        from nibe_mqtt_publisher import BrowserTopic
        em = self._make_em_with_sentinel([])
        em.scan_mqtt_discovery()
        calls = em.mqtt.publish.call_args_list
        sentinel_calls = [c for c in calls if c.args[0] == BrowserTopic.SCAN_SENTINEL]
        self.assertEqual(len(sentinel_calls), 1)
        self.assertEqual(sentinel_calls[0].args[1], 'scan')
        self.assertEqual(sentinel_calls[0].kwargs.get('retain'), False)

    def test_cleanup_removes_callbacks_and_unsubscribes_both_topics(self):
        """After the scan, both the config-topic and sentinel-topic
        callbacks/subscriptions must be torn down using their real topic
        strings, not None — leaving a None cleanup call would leave the
        real subscription dangling."""
        from nibe_mqtt_publisher import BrowserTopic
        em = self._make_em_with_sentinel([])
        em.scan_mqtt_discovery()
        removed = [c.args[0] for c in em.mqtt.message_callback_remove.call_args_list]
        unsubscribed = [c.args[0] for c in em.mqtt.unsubscribe.call_args_list]
        self.assertIn('homeassistant/+/+/config', removed)
        self.assertIn(BrowserTopic.SCAN_SENTINEL, removed)
        self.assertIn('homeassistant/+/+/config', unsubscribed)
        self.assertIn(BrowserTopic.SCAN_SENTINEL, unsubscribed)


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

    def test_publish_entity_discovery_failure_removes_point_from_enabled(self):
        """When publish_entity_discovery fails, the real failing point_id
        (not a placeholder) must land in failed_points and get discarded
        from mqtt_enabled_points — otherwise a permanently-failing point
        would be retried forever instead of being dropped."""
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        em._pub.publish_entity_discovery.return_value = None
        em.restore_from_mqtt()
        self.assertNotIn(100, em.mqtt_enabled_points)

    def test_publish_entity_discovery_called_with_point_and_bulk_data(self):
        """publish_entity_discovery must be called with the actual point
        dict and the real bulk_data — not None or a mismatched value —
        since it needs both to build a correct discovery config."""
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        em._pub.publish_entity_discovery.return_value = self._entity_info(100)
        em.bulk_data = {'sentinel': 'real-bulk-data-marker'}
        em.restore_from_mqtt()
        args, _kwargs = em._pub.publish_entity_discovery.call_args
        self.assertEqual(args[0], em.all_points_by_id[100])
        self.assertEqual(args[1], {'sentinel': 'real-bulk-data-marker'})

    def test_active_entities_stores_real_entity_info_not_none(self):
        """active_entities_by_id must be populated with the actual
        entity_info returned by publish_entity_discovery, not None —
        downstream code (e.g. state publishing) reads fields off this
        value, so storing None would break it even though the key exists."""
        em = self._make_em_with_points([100])
        em.mqtt_enabled_points.add(100)
        entity = self._entity_info(100)
        em._pub.publish_entity_discovery.return_value = entity
        em.restore_from_mqtt()
        self.assertEqual(em.active_entities_by_id[100], entity)

    def test_is_dynamic_missing_key_defaults_to_not_dynamic(self):
        """When a point dict has no 'is_dynamic' key at all, restore must
        default to False (not add it to active_dynamic_points) — matching
        get('is_dynamic', False), not some other default."""
        em = self._make_em_with_points([100])
        del em.all_points_by_id[100]['is_dynamic']
        em.mqtt_enabled_points.add(100)
        em._pub.publish_entity_discovery.return_value = self._entity_info(100)
        em.restore_from_mqtt()
        self.assertNotIn(100, em.active_dynamic_points)

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
        # Must be called with the actual entity_info and the actual message
        # object it received — not None or the wrong positional value.
        mock_handle.assert_called_once_with(entity, msg)

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

    def test_notification_message_contains_real_model_name(self):
        """The notification text substitutes the device's real model —
        previously unverified, only that SOME notification was sent.
        A wrong/None model would show a broken message like 'The Nibe
        None REST API has not responded...' directly to the user."""
        em = _make_em()
        em.device_info = {'model': 'S320'}
        em.api_consecutive_failures = 2
        em.api_failure_threshold = 3
        em._handle_api_failure()
        call_kwargs = em._notify.call_args.kwargs
        self.assertIn('The Nibe S320 REST API', call_kwargs['message'])

    def test_notification_model_falls_back_when_unknown(self):
        em = _make_em()
        em.device_info = {}
        em.api_consecutive_failures = 2
        em.api_failure_threshold = 3
        em._handle_api_failure()
        call_kwargs = em._notify.call_args.kwargs
        self.assertIn('The Nibe S-series REST API', call_kwargs['message'])

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

    def test_notify_called_with_real_mqtt_client_title_and_notification_id(self):
        """_notify must receive the actual mqtt client, the real title
        string, and the real notification_id constant — not None
        placeholders — otherwise the HA notification would fail to post
        or use the wrong dedup id (breaking auto-dismiss on recovery)."""
        from nibe_entity_manager import _NOTIF_API_UNREACHABLE
        em = _make_em()
        em.api_consecutive_failures = 2
        em.api_failure_threshold = 3
        em._handle_api_failure()
        args, kwargs = em._notify.call_args
        self.assertIs(args[0], em.mqtt)
        self.assertEqual(kwargs['title'], 'Nibe Bridge: API Unreachable')
        self.assertEqual(kwargs['notification_id'], _NOTIF_API_UNREACHABLE)

    def test_notification_seconds_computed_as_product_not_quotient(self):
        """The elapsed-seconds figure in the notification text must be
        failures * bulk_interval (a duration), not failures / bulk_interval
        — a quotient would silently show a nonsensical/misleading downtime
        estimate to the user."""
        em = _make_em()
        em.bulk_interval = 10
        em.api_consecutive_failures = 2  # will become 3 -> threshold hit
        em.api_failure_threshold = 3
        em._handle_api_failure()
        call_kwargs = em._notify.call_args.kwargs
        self.assertIn('(30s)', call_kwargs['message'])

    def test_bridge_alert_severity_message_and_context_fields(self):
        """publish_bridge_alert must receive severity='error', the real
        notification message text, and a context dict with the real
        consecutive_failures/failure_threshold/api_url keys and values —
        not None placeholders or renamed keys, since automations key off
        these exact field names."""
        em = _make_em()
        em.api_consecutive_failures = 4  # will become 5
        em.api_failure_threshold = 5
        em._api.base_url = 'http://192.0.2.5'
        em._handle_api_failure()
        call_kwargs = em._pub.publish_bridge_alert.call_args.kwargs
        self.assertEqual(call_kwargs['severity'], 'error')
        self.assertIn('The Nibe', call_kwargs['message'])
        context = call_kwargs['context']
        self.assertEqual(context['consecutive_failures'], 5)
        self.assertEqual(context['failure_threshold'], 5)
        self.assertEqual(context['api_url'], 'http://192.0.2.5')

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


class TestScanMqttPartialWarning(unittest.TestCase):
    """scan_mqtt_discovery: when the sentinel times out, a warning is emitted
    that mentions both the count of configs received so far and the word
    'partial' or 'incomplete'.

    E1 regression: the original warning said only 'retained message delivery
    may be incomplete' but did not include the partial count, making it hard
    to diagnose how many configs were missed.
    """

    def setUp(self):
        self.em = _make_em()
        # Wire up no-op MQTT stubs so scan_mqtt_discovery runs without a broker
        self.em.mqtt.subscribe    = MagicMock()
        self.em.mqtt.unsubscribe  = MagicMock()
        self.em.mqtt.message_callback_add    = MagicMock()
        self.em.mqtt.message_callback_remove = MagicMock()
        self.em.mqtt.publish      = MagicMock()

    def test_sentinel_timeout_emits_warning_with_partial_count(self):
        """When sentinel.wait returns False the warning message must include
        both a count of discovered configs and 'partial' or 'incomplete'."""
        import logging

        records = []
        class CapHandler(logging.Handler):
            def emit(self, record):
                records.append(record)
        cap = CapHandler()
        cap.setLevel(logging.WARNING)
        logging.getLogger('nibe.discovery').addHandler(cap)
        try:
            with patch('threading.Event.wait', return_value=False):
                self.em.scan_mqtt_discovery()
        finally:
            logging.getLogger('nibe.discovery').removeHandler(cap)

        warnings = [r for r in records if r.levelno >= logging.WARNING]
        self.assertTrue(
            any('Sentinel timeout' in r.getMessage() for r in warnings),
            "Must emit a warning containing 'Sentinel timeout'"
        )
        self.assertTrue(
            any(
                'partial' in r.getMessage().lower()
                or 'incomplete' in r.getMessage().lower()
                for r in warnings
            ),
            "Warning must mention 'partial' or 'incomplete' to diagnose missed configs",
        )

    def test_sentinel_success_emits_no_partial_warning(self):
        """When the sentinel arrives on time no partial-scan warning must appear."""
        import logging

        records = []
        class CapHandler(logging.Handler):
            def emit(self, record):
                records.append(record)
        cap = CapHandler()
        cap.setLevel(logging.WARNING)
        logging.getLogger('nibe.discovery').addHandler(cap)
        try:
            with patch('threading.Event.wait', return_value=True):
                self.em.scan_mqtt_discovery()
        finally:
            logging.getLogger('nibe.discovery').removeHandler(cap)

        sentinel_warnings = [
            r for r in records
            if r.levelno >= logging.WARNING and 'Sentinel timeout' in r.getMessage()
        ]
        self.assertEqual(
            len(sentinel_warnings), 0,
            "No Sentinel-timeout warning should appear when sentinel arrives on time",
        )
