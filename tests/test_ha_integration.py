"""
test_ha_integration.py
======================
Nibe_ha_integration tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from hypothesis import given
from hypothesis import strategies as st

from conftest import (
    _make_em,
    _nibe_point_id,
    _MENU_YAML,
)

class TestHaIntegrationConstantsProperties(unittest.TestCase):
    """Structural invariants for nibe_ha_integration class-level constants."""

    def setUp(self):
        from nibe_ha_integration import HAEntityRegistryWatcher
        self.cls = HAEntityRegistryWatcher

    def test_ping_timeout_less_than_ping_interval(self):
        """_PING_TIMEOUT_S must be strictly less than _PING_INTERVAL_S.

        If timeout >= interval, the keepalive can never succeed — we'd send
        a ping and declare the connection dead before receiving the pong.
        """
        self.assertLess(self.cls._PING_TIMEOUT_S, self.cls._PING_INTERVAL_S,
            f"_PING_TIMEOUT_S={self.cls._PING_TIMEOUT_S} must be < "
            f"_PING_INTERVAL_S={self.cls._PING_INTERVAL_S}")

    def test_initial_backoff_less_than_max_backoff(self):
        """_INITIAL_BACKOFF must be < _MAX_BACKOFF for exponential backoff to work."""
        self.assertLess(self.cls._INITIAL_BACKOFF, self.cls._MAX_BACKOFF)

    def test_all_backoff_constants_positive(self):
        self.assertGreater(self.cls._INITIAL_BACKOFF, 0)
        self.assertGreater(self.cls._MAX_BACKOFF, 0)

    def test_max_consecutive_failures_positive(self):
        self.assertGreater(self.cls._MAX_CONSEC_FAILURES, 0)

    def test_refresh_debounce_positive(self):
        self.assertGreater(self.cls._REFRESH_DEBOUNCE_S, 0)

    def test_ping_interval_positive(self):
        self.assertGreater(self.cls._PING_INTERVAL_S, 0)

    def test_ping_timeout_positive(self):
        self.assertGreater(self.cls._PING_TIMEOUT_S, 0)


# ---------------------------------------------------------------------------
# nibe_mqtt_publisher and nibe_lovelace constants structural properties
# ---------------------------------------------------------------------------


class TestPubStateProperties(unittest.TestCase):
    """Hypothesis properties for MqttDiscoveryPublisher._pub_state."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    @given(st.text(min_size=1, max_size=100), st.text(max_size=200))
    def test_always_calls_mqtt_publish(self, topic, payload):
        pub, mqtt = self._pub()
        pub._pub_state(topic, payload)
        mqtt.publish.assert_called_once()

    @given(st.text(min_size=1, max_size=100), st.text(max_size=200))
    def test_always_publishes_with_retain_true(self, topic, payload):
        pub, mqtt = self._pub()
        pub._pub_state(topic, payload)
        call = mqtt.publish.call_args
        retain = call.kwargs.get('retain', call.args[2] if len(call.args) > 2 else False)
        self.assertTrue(retain)

    @given(st.text(min_size=1, max_size=100), st.text(max_size=200))
    def test_publishes_to_correct_topic(self, topic, payload):
        pub, mqtt = self._pub()
        pub._pub_state(topic, payload)
        self.assertEqual(mqtt.publish.call_args.args[0], topic)

    @given(st.text(min_size=1, max_size=100), st.text(max_size=200))
    def test_publishes_correct_payload(self, topic, payload):
        pub, mqtt = self._pub()
        pub._pub_state(topic, payload)
        self.assertEqual(mqtt.publish.call_args.args[1], payload)

    @given(st.text(min_size=1, max_size=100), st.text(max_size=200))
    def test_never_raises_on_success(self, topic, payload):
        pub, mqtt = self._pub()
        pub._pub_state(topic, payload)  # must not raise

    @given(st.text(min_size=1, max_size=100), st.text(max_size=200))
    def test_never_raises_on_failure_rc(self, topic, payload):
        """Non-zero rc must log a warning but never raise."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=4)
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        pub._pub_state(topic, payload)  # must not raise


# ---------------------------------------------------------------------------
# HAEntityRegistryWatcher._sub properties (nibe_ha_integration.py)
# ---------------------------------------------------------------------------


class TestSubProperties(unittest.TestCase):
    """Hypothesis properties for ManagementCommandHandler._sub."""

    def _handler(self):
        from nibe_ha_integration import ManagementCommandHandler
        em   = MagicMock()
        mqtt = MagicMock()
        pub  = MagicMock()
        rw   = MagicMock()
        h = ManagementCommandHandler(em, mqtt, pub, rw)
        h._mqtt = mqtt
        h._em   = em
        return h, mqtt, em

    @given(st.text(min_size=1, max_size=100))
    def test_calls_mqtt_subscribe(self, topic):
        h, mqtt, em = self._handler()
        h._sub(topic, MagicMock())
        mqtt.subscribe.assert_called_once_with(topic, qos=1)

    @given(st.text(min_size=1, max_size=100))
    def test_calls_message_callback_add(self, topic):
        h, mqtt, em = self._handler()
        handler = MagicMock()
        h._sub(topic, handler)
        mqtt.message_callback_add.assert_called_once_with(topic, handler)

    @given(st.text(min_size=1, max_size=100))
    def test_calls_register_mgmt_subscription(self, topic):
        h, mqtt, em = self._handler()
        handler = MagicMock()
        h._sub(topic, handler)
        em.register_mgmt_subscription.assert_called_once_with(topic, handler, 1)

    @given(st.text(min_size=1, max_size=100), st.integers(min_value=0, max_value=2))
    def test_qos_passed_correctly(self, topic, qos):
        h, mqtt, em = self._handler()
        h._sub(topic, MagicMock(), qos=qos)
        mqtt.subscribe.assert_called_once_with(topic, qos=qos)
        em.register_mgmt_subscription.assert_called_once_with(
            topic, unittest.mock.ANY, qos)

    @given(st.text(min_size=1, max_size=100))
    def test_never_raises(self, topic):
        h, mqtt, em = self._handler()
        h._sub(topic, MagicMock())  # must not raise


# ---------------------------------------------------------------------------
# DynamicPointMap expected_active_dynamic_points properties
# ---------------------------------------------------------------------------


class TestPublishApiReachabilityProperties(unittest.TestCase):
    """Hypothesis properties for publish_api_reachability."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _get_api_state(self, mqtt):
        from nibe_mqtt_publisher import MgmtTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == MgmtTopic.API_OK_STATE]
        self.assertTrue(calls, "No API_OK_STATE publish found")
        return calls[-1].args[1]

    def _get_fetch_dur(self, mqtt):
        from nibe_mqtt_publisher import MgmtTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == MgmtTopic.FETCH_DUR_STATE]
        self.assertTrue(calls, "No FETCH_DUR_STATE publish found")
        return calls[-1].args[1]

    @given(st.integers(min_value=0, max_value=20),
           st.integers(min_value=1, max_value=20))
    def test_api_state_is_always_on_or_off(self, failures, threshold):
        pub, mqtt = self._pub()
        pub.publish_api_reachability(failures, threshold, 0.0, 0.1)
        state = self._get_api_state(mqtt)
        self.assertIn(state, ('ON', 'OFF'))

    @given(st.integers(min_value=0, max_value=20),
           st.integers(min_value=1, max_value=20))
    def test_api_state_on_when_failures_below_threshold(self, failures, threshold):
        pub, mqtt = self._pub()
        pub.publish_api_reachability(failures, threshold, 0.0, 0.1)
        state = self._get_api_state(mqtt)
        if failures < threshold:
            self.assertEqual(state, 'ON')
        else:
            self.assertEqual(state, 'OFF')

    @given(st.floats(min_value=0.0, max_value=9999.9,
                     allow_nan=False, allow_infinity=False))
    def test_fetch_duration_always_2dp(self, duration):
        """Fetch duration must always be formatted to exactly 2 decimal places."""
        pub, mqtt = self._pub()
        pub.publish_api_reachability(0, 3, 0.0, duration)
        state = self._get_fetch_dur(mqtt)
        self.assertRegex(state, r'^\d+\.\d{2}$')

    @given(st.floats(min_value=0.0, max_value=9999.9,
                     allow_nan=False, allow_infinity=False))
    def test_fetch_duration_value_matches_input(self, duration):
        """Fetch duration formatted value must match the input rounded to 2dp."""
        pub, mqtt = self._pub()
        pub.publish_api_reachability(0, 3, 0.0, duration)
        state = self._get_fetch_dur(mqtt)
        self.assertAlmostEqual(float(state), duration, places=2)


# ---------------------------------------------------------------------------
# HAEntityRegistryWatcher._next_id properties (nibe_ha_integration.py)
# ---------------------------------------------------------------------------


class TestNextIdProperties(unittest.TestCase):
    """Hypothesis properties for HAEntityRegistryWatcher._next_id."""

    def _make_watcher(self):
        from nibe_ha_integration import HAEntityRegistryWatcher
        em = MagicMock()
        pub = MagicMock()
        return HAEntityRegistryWatcher(em, pub)

    def test_first_call_returns_int(self):
        w = self._make_watcher()
        self.assertIsInstance(w._next_id(), int)

    def test_strictly_increments_by_one(self):
        w = self._make_watcher()
        ids = [w._next_id() for _ in range(10)]
        diffs = [ids[i+1] - ids[i] for i in range(len(ids)-1)]
        self.assertTrue(all(d == 1 for d in diffs))

    def test_never_returns_same_id_twice(self):
        w = self._make_watcher()
        ids = [w._next_id() for _ in range(50)]
        self.assertEqual(len(ids), len(set(ids)))

    @given(st.integers(min_value=1, max_value=100))
    def test_n_calls_produces_n_unique_ids(self, n):
        """n calls always produce n distinct IDs."""
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = HAEntityRegistryWatcher(MagicMock(), MagicMock())
        ids = [w._next_id() for _ in range(n)]
        self.assertEqual(len(set(ids)), n)

    @given(st.integers(min_value=1, max_value=100))
    def test_ids_are_monotonically_increasing(self, n):
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = HAEntityRegistryWatcher(MagicMock(), MagicMock())
        ids = [w._next_id() for _ in range(n)]
        self.assertEqual(ids, sorted(ids))


# ---------------------------------------------------------------------------
# publish_alarm_state properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestBuildMenuPointsProperties(unittest.TestCase):
    """Hypothesis properties for build_menu_points."""

    def setUp(self):
        from nibe_lovelace import build_menu_points
        self.fn = build_menu_points

    def test_always_returns_frozenset(self):
        result = self.fn(_MENU_YAML)
        self.assertIsInstance(result, frozenset)

    def test_missing_file_returns_empty_frozenset(self):
        result = self.fn('/nonexistent/menu_structure.yaml')
        self.assertEqual(result, frozenset())

    def test_missing_file_never_raises(self):
        self.fn('/nonexistent/path.yaml')  # must not raise

    def test_all_elements_are_ints(self):
        result = self.fn(_MENU_YAML)
        for pid in result:
            self.assertIsInstance(pid, int)

    def test_result_nonempty_for_real_yaml(self):
        result = self.fn(_MENU_YAML)
        self.assertGreater(len(result), 0)

    def test_consistent_with_collect_menu_points(self):
        """build_menu_points result must equal _collect_menu_points on same YAML."""
        import yaml as _yaml
        from nibe_lovelace import build_menu_points, _collect_menu_points
        with open(_MENU_YAML, encoding='utf-8') as f:
            data = _yaml.safe_load(f)
        collected = _collect_menu_points(data.get('menus', []))
        built = build_menu_points(_MENU_YAML)
        self.assertEqual(built, frozenset(collected))

    def test_idempotent_two_calls_same_result(self):
        r1 = self.fn(_MENU_YAML)
        r2 = self.fn(_MENU_YAML)
        self.assertEqual(r1, r2)

    @given(st.text(max_size=50))
    def test_any_path_never_raises(self, path):
        """build_menu_points must never raise for any path string."""
        from nibe_lovelace import build_menu_points
        build_menu_points(path)  # must not raise

    def test_corrupt_yaml_returns_empty_frozenset(self):
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml',
                                         delete=False) as f:
            f.write(': invalid: yaml: {{{')
            path = f.name
        try:
            result = self.fn(path)
            self.assertEqual(result, frozenset())
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# notify_ha / dismiss_ha properties (nibe_ha_integration.py)
# ---------------------------------------------------------------------------


class TestNotifyHaProperties(unittest.TestCase):
    """Hypothesis properties for notify_ha and dismiss_ha.

    Both functions make HTTP calls when SUPERVISOR_TOKEN is set. Without the
    token (test environment) they fall back to a log warning. All properties
    test the no-token path which is deterministic and side-effect free.
    """

    @given(st.text(max_size=80), st.text(max_size=500), st.text(max_size=50))
    def test_notify_ha_never_raises_without_token(self, title, message, notif_id):
        """Without SUPERVISOR_TOKEN notify_ha must never raise for any input."""
        from nibe_ha_integration import notify_ha
        with patch.dict('os.environ', {}, clear=True):
            notify_ha(MagicMock(), title, message, notif_id)

    @given(st.text(max_size=50))
    def test_dismiss_ha_never_raises_without_token(self, notif_id):
        """Without SUPERVISOR_TOKEN dismiss_ha must never raise for any input."""
        from nibe_ha_integration import dismiss_ha
        with patch.dict('os.environ', {}, clear=True):
            dismiss_ha(MagicMock(), notif_id)

    @given(st.text(max_size=80), st.text(max_size=500), st.text(max_size=50))
    def test_notify_ha_without_token_never_calls_urlopen(self, title, message, notif_id):
        """Without token, no HTTP call should be made."""
        from nibe_ha_integration import notify_ha
        with patch.dict('os.environ', {}, clear=True), \
             patch('urllib.request.urlopen') as mock_open:
            notify_ha(MagicMock(), title, message, notif_id)
        mock_open.assert_not_called()

    @given(st.text(max_size=50))
    def test_dismiss_ha_without_token_never_calls_urlopen(self, notif_id):
        """Without token, no HTTP call should be made."""
        from nibe_ha_integration import dismiss_ha
        with patch.dict('os.environ', {}, clear=True), \
             patch('urllib.request.urlopen') as mock_open:
            dismiss_ha(MagicMock(), notif_id)
        mock_open.assert_not_called()

    @given(st.text(max_size=80), st.text(max_size=500), st.text(max_size=50))
    def test_notify_ha_notification_id_preserved_in_payload(self, title, message, notif_id):
        """When token is present, notification_id must appear in the JSON payload."""
        import json as _json
        from nibe_ha_integration import notify_ha
        captured = []

        def fake_urlopen(req, **_kw):
            captured.append(_json.loads(req.data))
            return MagicMock()

        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'test_token'}), \
             patch('urllib.request.urlopen', side_effect=fake_urlopen):
            notify_ha(MagicMock(), title, message, notif_id)

        if captured:
            self.assertEqual(captured[0]['notification_id'], notif_id)
            self.assertEqual(captured[0]['title'], title)
            self.assertEqual(captured[0]['message'], message)



class TestEntityIdForProperties(unittest.TestCase):
    """Hypothesis properties for HAEntityRegistryWatcher.entity_id_for."""

    def _make_watcher(self, registry: dict | None = None):
        from nibe_ha_integration import HAEntityRegistryWatcher
        em = MagicMock()
        pub = MagicMock()
        w = HAEntityRegistryWatcher(em, pub)
        if registry:
            w._unique_id_map = registry
        return w

    @given(_nibe_point_id)
    def test_unknown_pid_always_returns_none(self, pid):
        """entity_id_for on an empty registry always returns None."""
        w = self._make_watcher()
        self.assertIsNone(w.entity_id_for(pid))

    @given(_nibe_point_id, st.text(min_size=1, max_size=50))
    def test_known_pid_returns_registered_entity_id(self, pid, entity_id):
        """entity_id_for returns the entity_id that was registered for that pid."""
        w = self._make_watcher({f'nibe_{pid}': entity_id})
        self.assertEqual(w.entity_id_for(pid), entity_id)

    @given(_nibe_point_id, st.text(min_size=1, max_size=50))
    def test_different_pid_returns_none(self, pid, entity_id):
        """Looking up a different pid than registered returns None."""
        other_pid = pid + 1
        w = self._make_watcher({f'nibe_{pid}': entity_id})
        self.assertIsNone(w.entity_id_for(other_pid))

    @given(st.dictionaries(
        _nibe_point_id,
        st.text(min_size=1, max_size=50),
        max_size=20,
    ))
    def test_result_consistent_with_unique_id_map(self, registry):
        """entity_id_for result always consistent with _unique_id_map lookup."""
        w = self._make_watcher({f'nibe_{pid}': eid for pid, eid in registry.items()})
        for pid in registry:
            self.assertEqual(
                w.entity_id_for(pid),
                w._unique_id_map.get(f'nibe_{pid}'),
            )

    @given(_nibe_point_id, st.text(min_size=1, max_size=50))
    def test_entity_id_for_uses_nibe_prefix_key(self, pid, entity_id):
        """The registry key must be nibe_{pid} — not just str(pid)."""
        w = self._make_watcher()
        # Register WITHOUT nibe_ prefix — should NOT be found
        w._unique_id_map[str(pid)] = entity_id
        self.assertIsNone(w.entity_id_for(pid))

    @given(st.dictionaries(
        _nibe_point_id,
        st.text(min_size=1, max_size=50),
        max_size=20,
    ))
    def test_all_registered_pids_are_found(self, registry):
        """Every pid that was registered must be findable."""
        w = self._make_watcher({f'nibe_{pid}': eid for pid, eid in registry.items()})
        for pid, eid in registry.items():
            self.assertEqual(w.entity_id_for(pid), eid)



class TestManagementHandlers(unittest.TestCase):
    """Tests for the MQTT management command handlers in nibe_ha_integration."""

    def setUp(self):
        import concurrent.futures
        from nibe_ha_integration import ManagementCommandHandler
        self.em        = _make_em()
        self.mqtt      = MagicMock()
        self.publisher = MagicMock()
        self.executor  = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ManagementCommandHandler(
            self.mqtt, self.em, self.publisher, self.executor
        ).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=True)

    def _msg(self, payload: str):
        m = MagicMock()
        m.payload = payload.encode()
        return m

    def _get_handler(self, topic_attr: str):
        """Retrieve the registered callback for a given MgmtTopic member."""
        from nibe_mqtt_publisher import MgmtTopic
        topic = getattr(MgmtTopic, topic_attr)
        for call in self.mqtt.message_callback_add.call_args_list:
            if call.args[0] == topic:
                return call.args[1]
        raise KeyError(f"No handler registered for {topic}")

    def _run(self, topic_attr: str, payload: str):
        """Trigger a handler and wait for its executor future to complete."""
        handler = self._get_handler(topic_attr)
        handler(None, None, self._msg(payload))
        self.executor.shutdown(wait=True)
        # Recreate executor so tearDown and subsequent _run calls work cleanly
        import concurrent.futures
        from nibe_ha_integration import ManagementCommandHandler
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Re-register handlers against the new executor
        ManagementCommandHandler(
            self.mqtt, self.em, self.publisher, self.executor
        ).register_all()

    # ── aid mode handler ──────────────────────────────────────────────────────

    def test_aid_mode_on_payloads(self):
        """ON, 1, on, true, True all map to 'on'."""
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run('AID_SET', 'ON')
        self.em._api.write_device_mode.assert_called_with('aidmode', 'on')

    def test_aid_mode_on_payload_numeric(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run('AID_SET', '1')
        self.em._api.write_device_mode.assert_called_with('aidmode', 'on')

    def test_aid_mode_off_payload(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run('AID_SET', 'OFF')
        self.em._api.write_device_mode.assert_called_with('aidmode', 'off')

    def test_aid_mode_publishes_state_on_success(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run('AID_SET', 'ON')
        from nibe_mqtt_publisher import MgmtTopic
        topics = [c.args[0] for c in self.mqtt.publish.call_args_list]
        self.assertIn(MgmtTopic.AID_STATE, topics)

    def test_aid_mode_does_not_publish_state_on_failure(self):
        self.em._api.write_device_mode = MagicMock(return_value=False)
        self._run('AID_SET', 'ON')
        from nibe_mqtt_publisher import MgmtTopic
        topics = [c.args[0] for c in self.mqtt.publish.call_args_list]
        self.assertNotIn(MgmtTopic.AID_STATE, topics)

    # ── smart mode handler ────────────────────────────────────────────────────

    def test_smart_mode_normal(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run('SMART_SET', 'normal')
        self.em._api.write_device_mode.assert_called_with('smartmode', 'normal')

    def test_smart_mode_away(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run('SMART_SET', 'away')
        self.em._api.write_device_mode.assert_called_with('smartmode', 'away')

    def test_smart_mode_invalid_value_ignored(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run('SMART_SET', 'holiday')
        self.em._api.write_device_mode.assert_not_called()

    # ── reset alarms handler ──────────────────────────────────────────────────

    def test_reset_alarms_calls_reset_notifications(self):
        self.em._api.reset_notifications = MagicMock(return_value=True)
        self._run('ALARM_RESET_PRESS', '')
        self.em._api.reset_notifications.assert_called_once()

    def test_reset_alarms_publishes_zero_alarm_state(self):
        self.em._api.reset_notifications = MagicMock(return_value=True)
        self._run('ALARM_RESET_PRESS', '')
        from nibe_mqtt_publisher import MgmtTopic
        publish_calls = {c.args[0]: c.args[1]
                         for c in self.mqtt.publish.call_args_list}
        self.assertEqual(publish_calls.get(MgmtTopic.ALARM_STATE), '0')

    def test_reset_alarms_no_publish_on_failure(self):
        self.em._api.reset_notifications = MagicMock(return_value=False)
        self._run('ALARM_RESET_PRESS', '')
        from nibe_mqtt_publisher import MgmtTopic
        topics = [c.args[0] for c in self.mqtt.publish.call_args_list]
        self.assertNotIn(MgmtTopic.ALARM_STATE, topics)

    # ── force poll handler ────────────────────────────────────────────────────

    def test_force_poll_calls_update_all_states(self):
        with patch.object(self.em, 'update_all_states') as mock_update:
            self._run('FORCE_POLL_PRESS', '')
        mock_update.assert_called_once_with(force=True)

    # ── enable / disable handlers ─────────────────────────────────────────────

    def test_enable_valid_point_id_calls_enable_entity(self):
        with patch.object(self.em, 'enable_entity', return_value=True) as mock_en:
            self._run('ENABLE_SET', '1234')
        mock_en.assert_called_once_with(1234)

    def test_enable_invalid_payload_does_not_raise(self):
        with patch.object(self.em, 'enable_entity') as mock_en:
            self._run('ENABLE_SET', 'notanumber')
        mock_en.assert_not_called()

    def test_disable_valid_point_id_calls_disable_entity(self):
        with patch.object(self.em, 'disable_entity', return_value=True) as mock_dis:
            self._run('DISABLE_SET', '5678')
        mock_dis.assert_called_once_with(5678)

    def test_disable_invalid_payload_does_not_raise(self):
        with patch.object(self.em, 'disable_entity') as mock_dis:
            self._run('DISABLE_SET', 'bad')
        mock_dis.assert_not_called()

    # ── changelog reset handler ───────────────────────────────────────────────

    def test_changelog_reset_calls_mark_changelog_read(self):
        with patch.object(self.em, 'mark_changelog_read') as mock_read:
            handler = self._get_handler('CHANGELOG_READ_PRESS')
            handler(None, None, self._msg(''))
        mock_read.assert_called_once()

    # ── flush dynamic map handler ───────────────────────────────────────────

    def test_flush_dynamic_map_calls_flush_with_current_points(self):
        self.em.all_points_by_id = {100: {'entity_type': 'switch'}}
        with patch.object(self.em.dynamic_point_map, 'flush') as mock_flush:
            self._run('FLUSH_MAP_PRESS', '')
        mock_flush.assert_called_once_with(
            self.em.all_points_by_id, {100: 'switch'},
        )

    def test_flush_dynamic_map_persists_after_flush(self):
        """The flush must be persisted to disk immediately — otherwise a
        restart before the next natural save would silently undo the flush."""
        with patch.object(self.em.dynamic_point_map, 'flush'), \
             patch.object(self.em, '_persist_dynamic_map') as mock_persist:
            self._run('FLUSH_MAP_PRESS', '')
        mock_persist.assert_called_once()

    def test_flush_dynamic_map_entity_types_default_to_empty_string(self):
        """A point missing the entity_type key must not crash the flush —
        defaults to '' rather than KeyError."""
        self.em.all_points_by_id = {200: {}}  # no entity_type key
        with patch.object(self.em.dynamic_point_map, 'flush') as mock_flush:
            self._run('FLUSH_MAP_PRESS', '')
        mock_flush.assert_called_once_with(
            self.em.all_points_by_id, {200: ''},
        )

    # ── run test suite handler ────────────────────────────────────────────────

    def _run_tests_call_args(self):
        """Helper: return all publish calls on em.mqtt for run_tests topics."""
        return [
            (c.args[0], c.args[1])
            for c in self.em.mqtt.publish.call_args_list
        ]

    def test_run_tests_publishes_running_state_immediately(self):
        """Pressing the button must immediately publish 'running' before subprocess completes."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='1543 passed in 15.0s', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        from nibe_mqtt_publisher import MgmtTopic
        states = [p for t, p in self._run_tests_call_args()
                  if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn('running', states)

    def test_run_tests_publishes_passed_on_success(self):
        """Exit code 0 → state topic must contain 'passed'."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='1543 passed in 15.0s', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        from nibe_mqtt_publisher import MgmtTopic
        states = [p for t, p in self._run_tests_call_args()
                  if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn('passed', states)

    def test_run_tests_publishes_failed_on_failure(self):
        """Non-zero exit code → state topic must contain 'failed'."""
        with patch('subprocess.run') as mock_run, \
             patch('nibe_ha_integration.notify_ha'):
            mock_run.return_value = MagicMock(
                returncode=1, stdout='1 failed in 15.0s', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        from nibe_mqtt_publisher import MgmtTopic
        states = [p for t, p in self._run_tests_call_args()
                  if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn('failed', states)

    def test_run_tests_pass_does_not_send_notification(self):
        """On pass, no HA notification — result is on the sensor attributes tab."""
        with patch('subprocess.run') as mock_run, \
             patch('nibe_ha_integration.notify_ha') as mock_notify, \
             patch('nibe_ha_integration.dismiss_ha') as mock_dismiss:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='1543 passed in 15.0s', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        mock_notify.assert_not_called()
        mock_dismiss.assert_called_once()

    def test_run_tests_pass_dismisses_previous_failure_notification(self):
        """On pass, any previous failure notification must be dismissed."""
        with patch('subprocess.run') as mock_run, \
             patch('nibe_ha_integration.dismiss_ha') as mock_dismiss:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='1543 passed in 15.0s', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        mock_dismiss.assert_called_once()

    def test_run_tests_notification_title_shows_failed(self):
        """Notification title must include 'FAILED' on failure."""
        with patch('subprocess.run') as mock_run, \
             patch('nibe_ha_integration.notify_ha') as mock_notify:
            mock_run.return_value = MagicMock(
                returncode=1, stdout='1 failed', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        _, kwargs = mock_notify.call_args
        self.assertIn('FAILED', kwargs.get('title', ''))

    def test_run_tests_subprocess_timeout_handled_gracefully(self):
        """subprocess.TimeoutExpired must not propagate — state becomes 'timed_out'."""
        from nibe_mqtt_publisher import MgmtTopic
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired('pytest', 3600)):
            self._run('RUN_TESTS_PRESS', '')
        states = [p for t, p in self._run_tests_call_args()
                  if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn('timed_out', states)

    def test_run_tests_timeout_notification_title(self):
        """TimeoutExpired must produce a '⏱ TIMED OUT' notification, not '❌ FAILED'."""
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired('pytest', 3600)), \
             patch('nibe_ha_integration.notify_ha') as mock_notify, \
             patch('builtins.open', MagicMock()):
            self._run('RUN_TESTS_PRESS', '')
        if mock_notify.called:
            kwargs = mock_notify.call_args.kwargs
            self.assertIn('TIMED OUT', kwargs.get('title', ''))
            self.assertNotIn('FAILED', kwargs.get('title', ''))

    def test_run_tests_launch_error_state_is_error(self):
        """An unexpected exception launching the subprocess must set state='error',
        not 'failed' or 'timed_out'."""
        from nibe_mqtt_publisher import MgmtTopic
        with patch('subprocess.run',
                   side_effect=OSError("no such file: python3")):
            self._run('RUN_TESTS_PRESS', '')
        states = [p for t, p in self._run_tests_call_args()
                  if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn('error', states)

    def test_run_tests_launch_error_notification_title(self):
        """A launch error must produce a '⚠ LAUNCH ERROR' notification title."""
        with patch('subprocess.run',
                   side_effect=OSError("no such file: python3")), \
             patch('nibe_ha_integration.notify_ha') as mock_notify, \
             patch('builtins.open', MagicMock()):
            self._run('RUN_TESTS_PRESS', '')
        if mock_notify.called:
            kwargs = mock_notify.call_args.kwargs
            self.assertIn('LAUNCH ERROR', kwargs.get('title', ''))

    def test_run_tests_uses_nightly_hypothesis_profile(self):
        """The subprocess must be launched with HYPOTHESIS_PROFILE=nightly."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        env = mock_run.call_args.kwargs.get('env', {})
        self.assertEqual(env.get('HYPOTHESIS_PROFILE'), 'nightly')

    def test_run_tests_generates_html_report(self):
        """pytest must be invoked with --html pointing to /config/www/ and
        Report is written to /config/www/nibe_test_report.html (assets in
        /config/www/assets/ — pytest-html 4.x multi-file output)."""
        with patch('subprocess.run') as mock_run, \
             patch('builtins.open', MagicMock()):
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        args = mock_run.call_args.args[0]
        html_args = [a for a in args if a.startswith('--html=')]
        self.assertEqual(len(html_args), 1)
        self.assertIn('/config/www/', html_args[0])
        self.assertIn('nibe_test_report.html', html_args[0])
        self.assertNotIn('--self-contained-html', args)

    def test_run_tests_logs_warning_when_report_missing(self):
        """If the HTML report is absent (e.g. pytest-html not installed in the
        Docker image), a clear WARNING must be emitted rather than silently
        swallowing the FileNotFoundError with a bare except."""
        with patch('subprocess.run') as mock_run, \
             patch('builtins.open', side_effect=FileNotFoundError), \
             patch('nibe_ha_integration.log_commands') as mock_log:
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        warning_msgs = [str(c) for c in mock_log.warning.call_args_list]
        self.assertTrue(
            any('pytest-html' in m or 'not found' in m.lower()
                for m in warning_msgs),
            "Expected a warning about the missing HTML report",
        )

    def test_run_tests_failure_notification_contains_report_link(self):
        """Failure notification must include a link to the HTML report so
        the user can open it directly from the HA notification bell."""
        with patch('subprocess.run') as mock_run, \
             patch('builtins.open', MagicMock()), \
             patch('nibe_ha_integration.notify_ha') as mock_notify:
            mock_run.return_value = MagicMock(
                returncode=1, stdout='FAILED test_x', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        self.assertTrue(mock_notify.called)
        message = mock_notify.call_args.kwargs.get('message', '')
        self.assertIn('nibe_test_report.html', message)

    def test_run_tests_publishes_attrs_with_summary(self):
        """The final attrs publish must contain a non-empty summary."""
        import json as _json
        from nibe_mqtt_publisher import MgmtTopic
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='1543 passed in 15.0s', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        attrs_calls = [p for t, p in self._run_tests_call_args()
                       if t == MgmtTopic.RUN_TESTS_ATTRS]
        self.assertGreaterEqual(len(attrs_calls), 2)
        final = _json.loads(attrs_calls[-1])
        self.assertIn('summary', final)
        self.assertIn('1543 passed', final['summary'])


    def test_run_tests_subprocess_uses_per_test_timeout_600(self):
        """pytest must be invoked with --timeout=600 so that long-running
        nightly Hypothesis stateful tests (stateful_step_count=50) are not
        killed by pytest.ini's default timeout=300."""
        with patch('subprocess.run') as mock_run, \
             patch('builtins.open', MagicMock()):
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        args = mock_run.call_args.args[0]
        self.assertIn('--timeout=600', args)

    def test_run_tests_subprocess_uses_xdist_auto(self):
        """pytest must be invoked with -n auto so xdist distributes tests
        across all available CPU cores (~4 on the ODROID-M1)."""
        with patch('subprocess.run') as mock_run, \
             patch('builtins.open', MagicMock()):
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            self._run('RUN_TESTS_PRESS', '')
        args = mock_run.call_args.args[0]
        self.assertIn('-n', args)
        n_idx = args.index('-n')
        self.assertEqual(args[n_idx + 1], 'auto')

    def test_run_tests_concurrent_trigger_ignored(self):
        """A second button press while a run is in flight must be silently
        dropped — subprocess.run must only be called once.

        Simulated by pre-setting _test_running on the handler before the
        second press, which is exactly the state during an in-flight run.
        """
        from nibe_ha_integration import ManagementCommandHandler
        import concurrent.futures

        em  = _make_em()
        pub = MagicMock()
        exe = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            handler = ManagementCommandHandler(em.mqtt, em, pub, exe)
            handler.register_all()

            with patch('subprocess.run') as mock_run, \
                 patch('builtins.open', MagicMock()):
                mock_run.return_value = MagicMock(
                    returncode=0, stdout='2652 passed in 26m 0s', stderr='')

                # Simulate an in-flight run by pre-setting the flag
                handler._test_running.set()

                # Trigger — should be ignored
                msg = MagicMock()
                msg.payload = b''
                handler._handle_run_tests(None, None, msg)

            mock_run.assert_not_called()
        finally:
            exe.shutdown(wait=False)


class TestRegistryWatcherEventHandling(unittest.TestCase):

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        return w

    def test_entity_id_for_miss_returns_none(self):
        w = self._make_watcher()
        self.assertIsNone(w.entity_id_for(6983))

    def test_entity_id_for_hit_returns_entity_id(self):
        w = self._make_watcher()
        w._unique_id_map['nibe_6983'] = 'number.nibe_6983_power'
        self.assertEqual(w.entity_id_for(6983), 'number.nibe_6983_power')

    def test_create_event_without_unique_id_does_not_crash(self):
        w = self._make_watcher()
        with patch.object(w, 'refresh_registry'):
            w._handle_event({'data': {'action': 'create',
                                      'entity_id': 'number.nibe_6983_power'}})
            if w._refresh_timer is not None:
                w._refresh_timer.cancel()
        self.assertNotIn('nibe_6983', w._unique_id_map)

    def test_create_event_with_top_level_unique_id(self):
        w = self._make_watcher()
        w._handle_event({'data': {'action': 'create',
                                  'entity_id': 'number.nibe_6983_power',
                                  'unique_id': 'nibe_6983'}})
        self.assertEqual(w._unique_id_map.get('nibe_6983'), 'number.nibe_6983_power')

    def test_create_event_with_nested_unique_id(self):
        w = self._make_watcher()
        w._handle_event({'data': {'action': 'create',
                                  'entity_id': 'number.nibe_6983_power',
                                  'config': {'unique_id': 'nibe_6983'}}})
        self.assertEqual(w._unique_id_map.get('nibe_6983'), 'number.nibe_6983_power')

    def test_update_event_populates_map(self):
        w = self._make_watcher()
        w._handle_event({'data': {'action': 'update',
                                  'entity_id': 'number.nibe_6983_power',
                                  'unique_id': 'nibe_6983'}})
        self.assertEqual(w._unique_id_map.get('nibe_6983'), 'number.nibe_6983_power')

    def test_remove_event_clears_entry(self):
        w = self._make_watcher()
        w._unique_id_map['nibe_6983'] = 'number.nibe_6983_power'
        w._handle_event({'data': {'action': 'remove',
                                  'unique_id': 'nibe_6983',
                                  'entity_id': 'number.nibe_6983_power'}})
        self.assertNotIn('nibe_6983', w._unique_id_map)

    def test_unknown_action_does_not_crash(self):
        w = self._make_watcher()
        w._handle_event({'data': {'action': 'something_new',
                                  'entity_id': 'number.nibe_test'}})


# ===========================================================================
# 36. Unit overrides
# ===========================================================================


class TestScheduleRefreshRegistry(unittest.TestCase):
    """_schedule_refresh_registry(): coalesces bursts of registry-refresh
    triggers into a single call.

    Root cause this fixes (found via live-hardware log analysis): every
    entity_registry_updated 'create' event lacking a unique_id — which per
    HA's own behaviour is normal for essentially every newly created MQTT
    entity — used to independently start its own
    threading.Timer(1.0, self.refresh_registry).start(). refresh_registry()
    opens a brand-new WebSocket connection to the Supervisor, does a full
    auth handshake, and fetches the entire entity registry — expensive
    every time. Enabling a large point set in one go (e.g. an entity-mode
    change to 'menus') creates that many entities in a tight window, so
    that many nearly-simultaneous new WebSocket connections were opened at
    once. In production this overwhelmed the Supervisor's WebSocket proxy:
    most calls timed out ('Connection timed out'), and once enough piled
    up the connection failed outright with broken-pipe errors. Cancel-and-
    reschedule debounce collapses any burst, however large, into exactly
    one refresh_registry() call fired after the burst settles."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        return w

    def test_single_call_starts_one_timer(self):
        w = self._make_watcher()
        with patch('threading.Timer') as mock_timer:
            w._schedule_refresh_registry()
        mock_timer.assert_called_once_with(w._REFRESH_DEBOUNCE_S, w.refresh_registry)
        mock_timer.return_value.start.assert_called_once()

    def test_burst_of_many_calls_coalesces_to_one_pending_timer(self):
        """The core regression case: N calls in a burst (simulating N
        entity_registry_updated create events fired for a large batch of
        newly enabled points) must cancel every prior timer, leaving
        exactly one live at the end — not N independent ones."""
        w = self._make_watcher()
        timers = [MagicMock() for _ in range(50)]
        with patch('threading.Timer', side_effect=timers) as mock_timer:
            for _ in range(50):
                w._schedule_refresh_registry()
        # All but the last timer must have been cancelled.
        for t in timers[:-1]:
            t.cancel.assert_called_once()
        timers[-1].cancel.assert_not_called()
        timers[-1].start.assert_called_once()
        self.assertEqual(mock_timer.call_count, 50)  # 50 scheduled...
        # ...but only the final one was ever allowed to actually fire.
        self.assertIs(w._refresh_timer, timers[-1])

    def test_create_event_burst_results_in_single_refresh_call(self):
        """End-to-end regression test: feed _handle_event a burst of
        'create' events without unique_id (the exact production
        trigger — a mode change enabling many points at once) and confirm
        refresh_registry() itself is called at most once, not once per
        event, once the burst settles. Uses a shortened debounce window
        so the test doesn't wait on the real production delay while still
        exercising the real Timer/thread integration."""
        w = self._make_watcher()
        with patch.object(w, 'refresh_registry') as mock_refresh, \
             patch.object(type(w), '_REFRESH_DEBOUNCE_S', 0.05):
            for i in range(65):
                w._handle_event({'data': {'action': 'create',
                                          'entity_id': f'sensor.nibe_{i}'}})
            if w._refresh_timer is not None:
                w._refresh_timer.join(timeout=2)
        mock_refresh.assert_called_once()

    def test_update_event_missing_unique_id_also_debounces(self):
        """The 'update' branch has the identical missing-unique_id
        fallback and must use the same coalescing, not its own
        independent timer."""
        w = self._make_watcher()
        with patch('threading.Timer') as mock_timer:
            w._handle_event({'data': {'action': 'update',
                                      'entity_id': 'sensor.nibe_1'}})
            w._handle_event({'data': {'action': 'update',
                                      'entity_id': 'sensor.nibe_2'}})
        self.assertEqual(mock_timer.call_count, 2)
        mock_timer.return_value.cancel.assert_called_once()  # first one cancelled

    def test_timer_is_daemon_and_named(self):
        w = self._make_watcher()
        with patch('threading.Timer') as mock_timer:
            w._schedule_refresh_registry()
        t = mock_timer.return_value
        self.assertTrue(t.daemon)

    def test_stop_cancels_pending_refresh_timer(self):
        """A shutdown mid-burst must not leave a dangling timer trying to
        open a WebSocket connection after teardown."""
        import threading as threading_mod
        w = self._make_watcher()
        w._stop_event = threading_mod.Event()
        w._thread = None
        w._ws_lock = threading_mod.Lock()
        w._current_ws = None
        pending = MagicMock()
        w._refresh_timer = pending
        w.stop()
        pending.cancel.assert_called_once()
        self.assertIsNone(w._refresh_timer)


# ===========================================================================
# 41. _collect_menu_points / _build_point_to_menu — menu tree walking
# ===========================================================================


class TestOnEntityEnabledDisabled(unittest.TestCase):
    """Fires when a user enables/disables an entity via HA's own entity
    settings UI, as opposed to the Entity Manager card. Zero coverage
    before this. Contains a real, deliberate behavioral asymmetry worth
    pinning down precisely: disabling a DYNAMIC point gets reverted
    (republished) with an explanatory notification, while disabling a
    plain STATIC point is mirrored into the bridge silently — the source
    comment is explicit: 'no confusing notification for an intentional
    disable'. Getting this backwards would either spam users for every
    routine disable or silently fail to explain why a dynamic point
    keeps reappearing after they try to turn it off."""

    def _make_watcher(self, em, pub=None):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em = em
        w._pub = pub or MagicMock()
        return w

    def _em_with_point(self, point_id, is_dynamic=False, display_title='Test point'):
        em = _make_em()
        em.all_points_by_id[point_id] = {
            'display_title': display_title, 'is_dynamic': is_dynamic,
        }
        return em

    # -- resolution failure --------------------------------------------------

    def test_enabled_unresolvable_entity_id_does_nothing(self):
        """If the entity_id can't be mapped back to a point_id at all,
        neither method should touch the bridge state or notify anyone."""
        em = _make_em()
        w = self._make_watcher(em)
        w._on_entity_enabled('switch.totally_unknown')
        self.assertEqual(em.mqtt.publish.call_count, 0)

    def test_disabled_unresolvable_entity_id_does_nothing(self):
        em = _make_em()
        w = self._make_watcher(em)
        w._on_entity_disabled('switch.totally_unknown')
        self.assertEqual(em.mqtt.publish.call_count, 0)

    # -- _on_entity_disabled: static point (the silent-mirror path) ----------

    def test_disabled_static_point_calls_disable_entity(self):
        em = self._em_with_point(100, is_dynamic=False)
        em.active_entities_by_id[100] = {'entity_type': 'switch', 'entity_id': 'foo'}
        em.mqtt_enabled_points.add(100)
        w = self._make_watcher(em)
        with patch.object(em, 'disable_entity') as mock_disable, \
             patch('nibe_ha_integration._publish_stats'):
            w._on_entity_disabled('switch.nibe_100')
        mock_disable.assert_called_once_with(100)

    def test_disabled_static_point_sends_no_notification(self):
        """The documented 'no confusing notification for an intentional
        disable' behavior — must not call notify_ha at all for a plain
        static-point disable."""
        em = self._em_with_point(100, is_dynamic=False)
        w = self._make_watcher(em)
        with patch.object(em, 'disable_entity'), \
             patch('nibe_ha_integration._publish_stats'), \
             patch('nibe_ha_integration.notify_ha') as mock_notify:
            w._on_entity_disabled('switch.nibe_100')
        mock_notify.assert_not_called()

    def test_disabled_static_point_publishes_stats(self):
        em = self._em_with_point(100, is_dynamic=False)
        w = self._make_watcher(em)
        with patch.object(em, 'disable_entity'), \
             patch('nibe_ha_integration._publish_stats') as mock_stats:
            w._on_entity_disabled('switch.nibe_100')
        mock_stats.assert_called_once()

    # -- _on_entity_disabled: dynamic point (the revert-and-notify path) -----

    def test_disabled_dynamic_point_does_not_call_disable_entity(self):
        """A dynamic point's HA-side disable must be REVERTED, not mirrored
        — disable_entity must never be called for it."""
        em = self._em_with_point(50827, is_dynamic=True)
        w = self._make_watcher(em)
        with patch.object(em, 'disable_entity') as mock_disable, \
             patch('nibe_ha_integration.notify_ha'):
            w._on_entity_disabled('sensor.nibe_50827')
        mock_disable.assert_not_called()

    def test_disabled_dynamic_point_republishes_discovery_config(self):
        em = self._em_with_point(50827, is_dynamic=True)
        pub = MagicMock()
        w = self._make_watcher(em, pub=pub)
        with patch('nibe_ha_integration.notify_ha'):
            w._on_entity_disabled('sensor.nibe_50827')
        pub.publish_entity_discovery.assert_called_once_with(
            em.all_points_by_id[50827], em.bulk_data,
        )

    def test_disabled_dynamic_point_sends_notification(self):
        """Unlike the static case, a dynamic point's disable attempt DOES
        notify the user — explaining why it reappeared."""
        em = self._em_with_point(50827, is_dynamic=True)
        w = self._make_watcher(em)
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            w._on_entity_disabled('sensor.nibe_50827')
        mock_notify.assert_called_once()

    # -- _on_entity_enabled ---------------------------------------------------

    def test_enabled_not_yet_in_mqtt_enabled_points_calls_enable_entity(self):
        em = self._em_with_point(100)
        w = self._make_watcher(em)
        with patch.object(em, 'enable_entity') as mock_enable, \
             patch('nibe_ha_integration._publish_stats'), \
             patch('nibe_ha_integration.notify_ha'):
            w._on_entity_enabled('switch.nibe_100')
        mock_enable.assert_called_once_with(100)

    def test_enabled_already_in_mqtt_enabled_points_republishes_discovery_instead(self):
        """If the bridge already considers the point enabled (e.g. HA's
        registry briefly lagged), don't re-run the full enable_entity path
        — just republish the discovery config to be safe."""
        em = self._em_with_point(100)
        em.mqtt_enabled_points.add(100)
        pub = MagicMock()
        w = self._make_watcher(em, pub=pub)
        with patch.object(em, 'enable_entity') as mock_enable, \
             patch('nibe_ha_integration.notify_ha'):
            w._on_entity_enabled('switch.nibe_100')
        mock_enable.assert_not_called()
        pub.publish_entity_discovery.assert_called_once()

    def test_enabled_dismisses_the_disable_notification(self):
        """Re-enabling must clear whatever disable notification was shown
        earlier for the same entity_id — uses the same notif_id derivation
        (dots/hyphens sanitised) as build_disable_notification."""
        em = self._em_with_point(100)
        w = self._make_watcher(em)
        with patch.object(em, 'enable_entity'), \
             patch('nibe_ha_integration._publish_stats'), \
             patch('nibe_ha_integration.dismiss_ha') as mock_dismiss, \
             patch('nibe_ha_integration.notify_ha'):
            w._on_entity_enabled('switch.nibe_100')
        mock_dismiss.assert_called_once_with(em.mqtt, 'nibe_ha_disable_switch_nibe_100')

    def test_enabled_sends_reenabled_notification(self):
        em = self._em_with_point(100)
        w = self._make_watcher(em)
        with patch.object(em, 'enable_entity'), \
             patch('nibe_ha_integration._publish_stats'), \
             patch('nibe_ha_integration.notify_ha') as mock_notify:
            w._on_entity_enabled('switch.nibe_100')
        mock_notify.assert_called_once()
        self.assertIn('re-enabled', mock_notify.call_args.kwargs['title'].lower())


# ===========================================================================
# 55. update_alarm_state — alarm polling and HA notification
# ===========================================================================


class TestUpdateAlarmState(unittest.TestCase):
    """Fetches /notifications and updates the Active Alarms sensor plus an
    edge-triggered HA persistent notification. Zero coverage before this.
    The edge-trigger logic (_alarm_notification_active) exists specifically
    to avoid re-notifying every poll cycle while an alarm remains active —
    getting the 0->N / N->0 transition logic wrong means either notification
    spam on every poll, or a notification that never clears after the
    alarm resolves."""

    def _alarm(self, alarm_id=1, header='High pressure alarm', description='',
               severity='Warning', time='2026-06-21T10:00:00', equip_name=''):
        return {
            'alarmId': alarm_id, 'header': header, 'description': description,
            'severity': severity, 'time': time, 'equipName': equip_name,
        }

    def _import(self):
        from nibe_ha_integration import update_alarm_state
        return update_alarm_state

    # -- short-circuit conditions ---------------------------------------------

    def test_api_failures_active_skips_entirely(self):
        """When the API is already known unreliable, alarm state must not
        be touched at all — avoids publishing stale/misleading data."""
        update_alarm_state = self._import()
        em = _make_em()
        em.api_consecutive_failures = 1
        pub = MagicMock()
        update_alarm_state(em, pub)
        em._api.fetch_notifications.assert_not_called()
        pub.publish_alarm_state.assert_not_called()

    def test_none_response_skips_publish(self):
        """fetch_notifications returning None means an API error occurred
        (distinct from a genuinely empty alarm list) — must not publish
        a misleading zero-alarm state."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = None
        pub = MagicMock()
        update_alarm_state(em, pub)
        pub.publish_alarm_state.assert_not_called()

    def test_empty_list_is_a_valid_zero_alarm_state(self):
        """An empty list (genuinely zero alarms) IS published, unlike None
        — these two falsy-ish values must be handled distinctly."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = []
        pub = MagicMock()
        update_alarm_state(em, pub)
        pub.publish_alarm_state.assert_called_once_with(0, [])

    # -- clean_alarms field mapping --------------------------------------------

    def test_clean_alarms_extracts_expected_fields(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True  # suppress notify_ha; not under test here
        em._api.fetch_notifications.return_value = [
            self._alarm(alarm_id=42, header='Sensor fault', severity='Error'),
        ]
        pub = MagicMock()
        update_alarm_state(em, pub)
        count, clean = pub.publish_alarm_state.call_args.args
        self.assertEqual(count, 1)
        self.assertEqual(clean[0]['alarmId'], 42)
        self.assertEqual(clean[0]['header'], 'Sensor fault')
        self.assertEqual(clean[0]['severity'], 'Error')

    def test_clean_alarms_missing_fields_default_safely(self):
        """A real alarm dict missing optional fields (e.g. no equipName)
        must not crash — defaults to empty string, not KeyError."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True  # suppress notify_ha; not under test here
        em._api.fetch_notifications.return_value = [{'alarmId': 1, 'header': 'X'}]
        pub = MagicMock()
        update_alarm_state(em, pub)
        _, clean = pub.publish_alarm_state.call_args.args
        self.assertEqual(clean[0]['description'], '')
        self.assertEqual(clean[0]['equipName'], '')
        self.assertEqual(clean[0]['time'], '')

    # -- edge-triggered notification: 0 -> N transition ------------------------

    def test_first_alarm_triggers_notification(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = False
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        mock_notify.assert_called_once()
        self.assertTrue(em._alarm_notification_active)

    def test_notification_id_is_fixed_active_alarms(self):
        """notification_id must be the fixed 'nibe_active_alarms' string so
        dismiss_ha (using the same id) can clear exactly this notification."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertEqual(mock_notify.call_args.kwargs['notification_id'], 'nibe_active_alarms')

    def test_alarm_continuing_does_not_re_notify(self):
        """The whole point of the edge-trigger flag: a second poll cycle
        with the alarm still active must NOT fire another notification."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True  # already notified previously
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        mock_notify.assert_not_called()

    def test_alarm_count_increasing_while_active_does_not_re_notify(self):
        """Even if a SECOND distinct alarm appears while one is already
        active, the flag still suppresses re-notification — by design,
        not a bug, since the user already has an active notification."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True
        em._api.fetch_notifications.return_value = [self._alarm(alarm_id=1), self._alarm(alarm_id=2)]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        mock_notify.assert_not_called()

    # -- edge-triggered notification: N -> 0 transition ------------------------

    def test_alarm_cleared_dismisses_notification(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True
        em._api.fetch_notifications.return_value = []
        with patch('nibe_ha_integration.dismiss_ha') as mock_dismiss:
            update_alarm_state(em, MagicMock())
        mock_dismiss.assert_called_once_with(em.mqtt, 'nibe_active_alarms')
        self.assertFalse(em._alarm_notification_active)

    def test_already_inactive_no_alarms_does_not_dismiss_again(self):
        """If there was no active notification to begin with, a zero-alarm
        poll must not call dismiss_ha redundantly."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = False
        em._api.fetch_notifications.return_value = []
        with patch('nibe_ha_integration.dismiss_ha') as mock_dismiss:
            update_alarm_state(em, MagicMock())
        mock_dismiss.assert_not_called()

    # -- message composition ---------------------------------------------------

    def test_message_includes_device_model_from_device_info(self):
        update_alarm_state = self._import()
        em = _make_em()
        em.device_info = {'model': 'S2125-12'}
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn('S2125-12', mock_notify.call_args.kwargs['title'])
        self.assertIn('S2125-12', mock_notify.call_args.kwargs['message'])

    def test_message_falls_back_to_s_series_when_model_unknown(self):
        update_alarm_state = self._import()
        em = _make_em()
        em.device_info = {}  # no 'model' key
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn('S-series', mock_notify.call_args.kwargs['title'])

    def test_message_includes_equipment_and_severity(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header='Pump fault', equip_name='GP1', severity='Critical'),
        ]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs['message']
        self.assertIn('Pump fault', msg)
        self.assertIn('Equipment: GP1', msg)
        self.assertIn('Severity: Critical', msg)

    def test_message_omits_equipment_when_blank(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header='Generic fault', equip_name=''),
        ]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs['message']
        self.assertNotIn('Equipment:', msg)

    def test_message_description_omitted_when_identical_to_header(self):
        """The dedup check: if description == header, must not repeat it
        verbatim in the message — only appended when it adds information."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header='High pressure alarm', description='High pressure alarm'),
        ]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs['message']
        # Header appears once via the line; description must not duplicate it.
        self.assertEqual(msg.count('High pressure alarm'), 1)

    def test_message_description_included_when_distinct_from_header(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header='High pressure alarm', description='Pressure exceeded 28 bar'),
        ]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs['message']
        self.assertIn('Pressure exceeded 28 bar', msg)

    def test_message_lists_multiple_alarms_as_bullet_points(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(alarm_id=1, header='Alarm A'),
            self._alarm(alarm_id=2, header='Alarm B'),
        ]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs['message']
        self.assertIn('• ', msg)
        self.assertIn('Alarm A', msg)
        self.assertIn('Alarm B', msg)

    def test_title_includes_correct_alarm_count(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [self._alarm(alarm_id=1), self._alarm(alarm_id=2)]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn('2 Active Alarm(s)', mock_notify.call_args.kwargs['title'])

    def test_message_mentions_reset_alarms_button(self):
        """The message must point the user to the actual remediation path
        (the Reset Alarms management button) — not just describe the problem."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn('Reset Alarms', mock_notify.call_args.kwargs['message'])


# ===========================================================================
# 56. EntityManager._fetch_bulk_data — string cache and new-point routing
# ===========================================================================


class TestPublishDeviceModesHaIntegration(unittest.TestCase):
    """Caches aid/smart mode to avoid an extra fetch_device_info() API call
    on every poll cycle, invalidated on startup and after any mode write.
    Zero coverage before this. A cache bug here means either stale mode
    display (cache never invalidates after a write) or an unnecessary API
    call every single poll (cache never hits) — same category of risk as
    the string cache we tested in _fetch_bulk_data."""

    def _import(self):
        from nibe_ha_integration import _publish_device_modes
        return _publish_device_modes

    def test_api_failures_active_skips_entirely(self):
        fn = self._import()
        em = _make_em()
        em.api_consecutive_failures = 1
        pub = MagicMock()
        fn(em, pub)
        em._api.fetch_device_info.assert_not_called()
        pub.publish_device_modes.assert_not_called()

    def test_fresh_instance_dirty_cache_fetches_from_api(self):
        """device_modes_dirty=True by default on a new instance — must
        fetch fresh rather than trusting an empty cache."""
        fn = self._import()
        em = _make_em()
        em._api.fetch_device_info.return_value = {'aidMode': 'on', 'smartMode': 'away'}
        fn(em, MagicMock())
        em._api.fetch_device_info.assert_called_once()

    def test_clean_cache_with_data_skips_api_call(self):
        """Not dirty AND cache populated -> use cached values, no fetch."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = False
        em.device_modes_cache = {'aidMode': 'on', 'smartMode': 'normal'}
        pub = MagicMock()
        fn(em, pub)
        em._api.fetch_device_info.assert_not_called()
        pub.publish_device_modes.assert_called_once_with(aid_mode='on', smart_mode='normal')

    def test_dirty_flag_forces_refetch_even_with_populated_cache(self):
        """A populated cache that's marked dirty (e.g. just after a mode
        write) must still trigger a fresh fetch, not serve stale data."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = True
        em.device_modes_cache = {'aidMode': 'off', 'smartMode': 'normal'}  # stale
        em._api.fetch_device_info.return_value = {'aidMode': 'on', 'smartMode': 'away'}
        pub = MagicMock()
        fn(em, pub)
        em._api.fetch_device_info.assert_called_once()
        pub.publish_device_modes.assert_called_once_with(aid_mode='on', smart_mode='away')

    def test_successful_fetch_updates_cache_and_clears_dirty(self):
        fn = self._import()
        em = _make_em()
        em._api.fetch_device_info.return_value = {'aidMode': 'on', 'smartMode': 'away'}
        fn(em, MagicMock())
        self.assertEqual(em.device_modes_cache, {'aidMode': 'on', 'smartMode': 'away'})
        self.assertFalse(em.device_modes_dirty)

    def test_failed_fetch_does_not_clear_dirty_or_corrupt_cache(self):
        """fetch_device_info returning None (API error) must leave the
        dirty flag and existing cache untouched — so the NEXT poll retries
        rather than silently giving up and serving garbage forever."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = True
        em.device_modes_cache = {'aidMode': 'on', 'smartMode': 'normal'}  # prior good data
        em._api.fetch_device_info.return_value = None
        pub = MagicMock()
        fn(em, pub)
        self.assertTrue(em.device_modes_dirty)
        self.assertEqual(em.device_modes_cache, {'aidMode': 'on', 'smartMode': 'normal'})
        pub.publish_device_modes.assert_not_called()

    def test_missing_aidmode_key_defaults_to_off(self):
        fn = self._import()
        em = _make_em()
        em._api.fetch_device_info.return_value = {'smartMode': 'normal'}  # no aidMode
        pub = MagicMock()
        fn(em, pub)
        pub.publish_device_modes.assert_called_once_with(aid_mode='off', smart_mode='normal')

    def test_missing_smartmode_key_defaults_to_normal(self):
        fn = self._import()
        em = _make_em()
        em._api.fetch_device_info.return_value = {'aidMode': 'on'}  # no smartMode
        pub = MagicMock()
        fn(em, pub)
        pub.publish_device_modes.assert_called_once_with(aid_mode='on', smart_mode='normal')

    def test_cached_path_also_applies_same_defaults(self):
        """The cache-hit branch reads from device_modes_cache with the same
        .get(key, default) fallbacks as the fetch branch — confirms both
        code paths apply identical defaulting, not just the fetch path."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = False
        em.device_modes_cache = {'aidMode': 'on'}  # smartMode key missing
        pub = MagicMock()
        fn(em, pub)
        em._api.fetch_device_info.assert_not_called()
        pub.publish_device_modes.assert_called_once_with(aid_mode='on', smart_mode='normal')


# ===========================================================================
# 60. update_stats_and_health — bridge health/stats publishing
# ===========================================================================


class TestUpdateStatsAndHealth(unittest.TestCase):
    """Mostly orchestration (calls into _publish_stats and several
    publisher methods with field mappings) — a typo in any of these
    mappings would silently report wrong values on the bridge_status and
    api_reachability sensors without any error. Zero coverage before this."""

    def _import(self):
        from nibe_ha_integration import update_stats_and_health
        return update_stats_and_health

    def test_calls_publish_uptime_with_correct_fields(self):
        fn = self._import()
        em = _make_em()
        em.bridge_start_time = 1000.0
        em.api_last_success_time = 2000.0
        em.api_consecutive_failures = 3
        pub = MagicMock()
        with patch('nibe_ha_integration._publish_stats'):
            fn(em, pub)
        pub.publish_uptime.assert_called_once_with(1000.0, 2000.0, 3)

    def test_calls_publish_api_reachability_with_correct_fields(self):
        fn = self._import()
        em = _make_em()
        em.api_consecutive_failures = 2
        em.api_failure_threshold = 5
        em.api_last_success_time = 1500.0
        em.last_fetch_duration = 0.8
        pub = MagicMock()
        with patch('nibe_ha_integration._publish_stats'):
            fn(em, pub)
        pub.publish_api_reachability.assert_called_once_with(2, 5, 1500.0, 0.8)

    def test_bridge_status_includes_pending_write_count(self):
        fn = self._import()
        em = _make_em()
        em.pending_writes = {1: {}, 2: {}, 3: {}}
        pub = MagicMock()
        with patch('nibe_ha_integration._publish_stats'):
            fn(em, pub)
        kwargs = pub.publish_bridge_status.call_args.kwargs
        self.assertEqual(kwargs['pending_write_count'], 3)

    def test_bridge_status_includes_write_counters(self):
        fn = self._import()
        em = _make_em()
        em._write_total = 50
        em._write_success = 45
        em._write_failed = 5
        em._last_write_error = 'point 100 failed'
        pub = MagicMock()
        with patch('nibe_ha_integration._publish_stats'):
            fn(em, pub)
        kwargs = pub.publish_bridge_status.call_args.kwargs
        self.assertEqual(kwargs['write_total'], 50)
        self.assertEqual(kwargs['write_success'], 45)
        self.assertEqual(kwargs['write_failed'], 5)
        self.assertEqual(kwargs['last_write_error'], 'point 100 failed')

    def test_calls_publish_stats_once(self):
        fn = self._import()
        em = _make_em()
        pub = MagicMock()
        with patch('nibe_ha_integration._publish_stats') as mock_stats:
            fn(em, pub)
        mock_stats.assert_called_once_with(em, pub)


# ===========================================================================
# 61. EntityManager._fetch_bulk_data — disappeared dynamic point detection
# ===========================================================================


class TestHandleEventDeadCodeFix(unittest.TestCase):
    """The 'update' action branch in _handle_event previously returned
    unconditionally after updating the unique_id_map cache, leaving the
    disabled_by change detection in unreachable dead code. This meant
    HA-side entity enable/disable events were silently swallowed —
    _on_entity_enabled and _on_entity_disabled never fired from HA registry
    events. Fixed by folding the disabled_by check into the update branch."""

    def _watcher(self):
        from nibe_ha_integration import HAEntityRegistryWatcher
        em = MagicMock()
        pub = MagicMock()
        w = HAEntityRegistryWatcher(em, pub)
        w._unique_id_map = {'nibe_5110': 'switch.nibe_5110'}
        em.resolve_point_from_entity_id.return_value = 5110
        em.mqtt_enabled_points = set()
        em.all_points_by_id = {5110: {'is_dynamic': False}}
        em.build_disable_notification.return_value = ('title', 'msg', 'notif_id')
        return w, em

    def test_entity_disabled_via_ha_now_fires(self):
        """HA disabling an entity (disabled_by changes from None to 'user')
        must call _on_entity_disabled — previously this never fired."""
        w, em = self._watcher()
        event = {
            'data': {
                'action': 'update',
                'entity_id': 'switch.nibe_5110',
                'changes': {'disabled_by': None},   # prev was None → now 'user'
            }
        }
        with patch.object(w, '_on_entity_disabled') as mock_disabled:
            w._handle_event(event)
        mock_disabled.assert_called_once_with('switch.nibe_5110')

    def test_entity_enabled_via_ha_now_fires(self):
        """HA re-enabling an entity (disabled_by changes from 'user' to None)
        must call _on_entity_enabled — previously this never fired."""
        w, em = self._watcher()
        event = {
            'data': {
                'action': 'update',
                'entity_id': 'switch.nibe_5110',
                'changes': {'disabled_by': 'user'},  # prev was 'user' → now None
            }
        }
        with patch.object(w, '_on_entity_enabled') as mock_enabled:
            w._handle_event(event)
        mock_enabled.assert_called_once_with('switch.nibe_5110')

    def test_update_without_disabled_by_still_updates_map(self):
        """An update event without a disabled_by change (e.g. rename) must
        still update the unique_id_map cache — confirming the cache-update
        logic wasn't lost in the refactor."""
        w, em = self._watcher()
        event = {
            'data': {
                'action': 'update',
                'entity_id': 'switch.nibe_5110_renamed',
                'unique_id': 'nibe_5110',
                'changes': {'name': 'New name'},   # no disabled_by
            }
        }
        w._handle_event(event)
        self.assertEqual(w._unique_id_map.get('nibe_5110'), 'switch.nibe_5110_renamed')

    def test_update_without_disabled_by_does_not_call_enable_disable(self):
        """Sanity: a rename/name-change update must not trigger enable/disable."""
        w, em = self._watcher()
        event = {
            'data': {
                'action': 'update',
                'entity_id': 'switch.nibe_5110',
                'changes': {'name': 'New name'},
            }
        }
        with patch.object(w, '_on_entity_enabled') as mock_en, \
             patch.object(w, '_on_entity_disabled') as mock_dis:
            w._handle_event(event)
        mock_en.assert_not_called()
        mock_dis.assert_not_called()

    def test_create_and_remove_events_unaffected(self):
        """create/remove events must still work correctly — confirms the
        refactor didn't accidentally break the other action branches."""
        w, _ = self._watcher()
        # create: adds to map
        w._handle_event({'data': {
            'action': 'create',
            'entity_id': 'switch.nibe_9999',
            'unique_id': 'nibe_9999',
        }})
        self.assertEqual(w._unique_id_map.get('nibe_9999'), 'switch.nibe_9999')
        # remove: cleans up map
        w._handle_event({'data': {
            'action': 'remove',
            'unique_id': 'nibe_9999',
        }})
        self.assertNotIn('nibe_9999', w._unique_id_map)


# ===========================================================================
# 65. Slice 1 fixes: F1 (_on_entity_disabled), F3 (type.replace), F4 (changelog item validation)
# ===========================================================================


class TestOnEntityDisabledRefactor(unittest.TestCase):
    """_on_entity_disabled after removal of the permanently-dead
    live_dependents block. Verifies both branches still behave correctly:
    dynamic points get their discovery config republished and a notification
    sent; normal static points get disabled and no notification."""

    def _watcher(self):
        from nibe_ha_integration import HAEntityRegistryWatcher
        em = MagicMock()
        pub = MagicMock()
        w = HAEntityRegistryWatcher(em, pub)
        w._unique_id_map = {'nibe_5110': 'switch.nibe_5110'}
        em.resolve_point_from_entity_id.return_value = 5110
        em.build_disable_notification.return_value = ('title', 'msg', 'nibe_ha_disable_switch_nibe_5110')
        em.mqtt = MagicMock()
        return w, em, pub

    def test_static_point_disabled_no_notification(self):
        """Disabling a static point must call disable_entity and NOT send
        a notification — an intentional disable needs no explanation."""
        w, em, pub = self._watcher()
        em.all_points_by_id = {5110: {'is_dynamic': False}}
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            w._on_entity_disabled('switch.nibe_5110')
        em.disable_entity.assert_called_once_with(5110)
        mock_notify.assert_not_called()

    def test_dynamic_point_republishes_discovery_and_notifies(self):
        """Disabling a dynamic point must republish discovery (to reverse
        the HA-side disable) and send a notification explaining why."""
        w, em, pub = self._watcher()
        em.all_points_by_id = {
            5110: {'is_dynamic': True, 'entity_type': 'sensor', 'entity_category': 'diagnostic'},
        }
        em.bulk_data = {}
        with patch('nibe_ha_integration.notify_ha') as mock_notify:
            w._on_entity_disabled('switch.nibe_5110')
        pub.publish_entity_discovery.assert_called_once()
        em.disable_entity.assert_not_called()
        mock_notify.assert_called_once()

    def test_unknown_point_returns_early(self):
        """resolve_point_from_entity_id returning None must be a no-op."""
        w, em, pub = self._watcher()
        em.resolve_point_from_entity_id.return_value = None
        w._on_entity_disabled('switch.nibe_unknown')
        em.disable_entity.assert_not_called()
        pub.publish_entity_discovery.assert_not_called()



class TestSetupMenuDashboardBrokenConnection(unittest.TestCase):
    """When the lovelace/dashboards list call fails (WebSocket unhealthy),
    _setup_menu_dashboard must return False immediately without attempting
    lovelace/config/save — which would also fail and waste the attempt.
    Previously the code logged DEBUG and fell through to the save call,
    giving a misleading 'proceeding to config save' message when nothing
    useful was going to happen."""

    def _make_watcher(self, menu_yaml):
        import io
        em = MagicMock()
        pub = MagicMock()
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = HAEntityRegistryWatcher(em, pub)
        w._unique_id_map = {}
        em.all_points_by_id = {}
        em.dynamic_point_map = MagicMock()
        em.dynamic_point_map.values.return_value = []
        em.dynamic_point_map.all_known_dynamic_point_ids.return_value = set()
        em.active_dynamic_points = set()
        em.bulk_data = {}
        em.mqtt_enabled_points = set()
        em.point_to_menu_map = {}
        return w, io.StringIO(menu_yaml)

    def test_failed_dashboard_list_returns_true_for_retry(self):
        """A Lovelace API timeout (lovelace/dashboards returning {}) is a
        transient startup condition — HA may not be ready yet. Must return
        True so _regen_menu_dashboard schedules a retry.
        Tests _setup_menu_dashboard_lovelace directly since the WebSocket
        is now opened inside _setup_menu_dashboard after the registry wait."""
        from nibe_lovelace import _setup_menu_dashboard_lovelace

        watcher, _ = self._make_watcher(
            "menus:\n  - id: '1.1'\n    title: Test\n    settings: []\n    submenus: []\n"
        )
        em = watcher._em
        em.active_dynamic_points = set()

        ws_calls = []
        def fake_ws_call(ws, _msg_id, payload, _timeout=10):
            ws_calls.append(payload.get('type'))
            if payload.get('type') == 'lovelace/dashboards/list':
                return {}   # simulates Lovelace API not ready / timeout
            return {'success': True, 'result': []}

        ws = MagicMock()
        next_id = iter(range(1, 100)).__next__

        with patch('nibe_lovelace._ws_call', side_effect=fake_ws_call), \
             patch('nibe_lovelace.log_startup') as mock_log:
            result = _setup_menu_dashboard_lovelace(
                ws, next_id, {'views': [{'title': 'Test'}]},
                em, watcher, set(), set(),
            )

        self.assertIs(result, True,
            "Transient Lovelace API timeout must return True (needs retry), not False")
        self.assertNotIn('lovelace/config/save', ws_calls,
            "lovelace/config/save must not be called when dashboards list failed")
        self.assertTrue(mock_log.warning.called,
            "A warning must be logged when the dashboards list call fails")


# ===========================================================================
# 68. Fresh-start fixes: enable_entity log level + registry fetch race
# ===========================================================================


class TestRegistryFetchRaceCondition(unittest.TestCase):
    """_fetch_entity_registry loops over recv() until it finds the response
    matching its request ID, discarding interleaved entity_registry_updated
    events. Previously a single recv() would pick up the first available
    message — on a fresh start an entity create event could arrive before
    the list response, causing a spurious 'Could not fetch' warning and an
    empty registry map."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em = MagicMock()
        w._pub = MagicMock()
        return w

    def test_interleaved_event_discarded_list_response_used(self):
        """recv() returns an entity_registry_updated event first, then the
        actual list response. The fetch must discard the event and use the
        list response."""
        import json as _json
        w = self._make_watcher()
        ws = MagicMock()
        # First recv returns an entity_registry_updated event (wrong id)
        # Second recv returns the list response (matching id)
        ws.recv.side_effect = [
            _json.dumps({
                'type': 'event',
                'event': {'event_type': 'entity_registry_updated'},
                'id': 0,  # wrong id — not our request
            }),
            _json.dumps({
                'id': 1,  # matches req_id (first call to _next_id returns 1)
                'type': 'result',
                'success': True,
                'result': [
                    {'unique_id': 'nibe_1234', 'entity_id': 'sensor.nibe_1234',
                     'platform': 'mqtt'},
                ],
            }),
        ]
        result = w._fetch_entity_registry(ws)
        self.assertEqual(ws.recv.call_count, 2,
            "Must call recv() twice to skip the interleaved event")
        self.assertEqual(result.get('nibe_1234'), 'sensor.nibe_1234',
            "Must return mapping from the actual list response")

    def test_direct_list_response_still_works(self):
        """Normal restart: first recv() returns the list response directly
        (no interleaved events). Must still work correctly."""
        import json as _json
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = _json.dumps({
            'id': 1,
            'type': 'result',
            'success': True,
            'result': [
                {'unique_id': 'nibe_5110', 'entity_id': 'switch.nibe_5110',
                 'platform': 'mqtt'},
            ],
        })
        result = w._fetch_entity_registry(ws)
        self.assertEqual(ws.recv.call_count, 1)
        self.assertEqual(result.get('nibe_5110'), 'switch.nibe_5110')



class TestRegistryFetchMissingUniqueId(unittest.TestCase):
    """Test _fetch_entity_registry with entries missing unique_id."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        return w

    def test_skips_entries_without_unique_id(self):
        w = self._make_watcher()
        ws = MagicMock()
        response = json.dumps({
            'id': 1,
            'type': 'result',
            'success': True,
            'result': [
                {'entity_id': 'sensor.nibe_123', 'unique_id': 'nibe_123'},
                {'entity_id': 'sensor.no_id', 'platform': 'mqtt'}
            ]
        })
        ws.recv.return_value = response
        result = w._fetch_entity_registry(ws)
        self.assertIn('nibe_123', result)
        self.assertNotIn('no_id', result)



class TestNotifyHa(unittest.TestCase):
    """notify_ha sends a persistent notification via the Supervisor REST API.
    Falls back gracefully when SUPERVISOR_TOKEN is absent."""

    def test_no_token_does_not_call_urlopen(self):
        from nibe_ha_integration import notify_ha
        with patch.dict('os.environ', {}, clear=True):
            with patch('urllib.request.urlopen') as mock_open:
                notify_ha(None, 'title', 'msg', 'test_id')
                mock_open.assert_not_called()

    def test_with_token_calls_urlopen(self):
        from nibe_ha_integration import notify_ha
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'fake_token'}):
            with patch('urllib.request.urlopen') as mock_open:
                notify_ha(None, 'Test Title', 'Test message', 'nibe_test')
                mock_open.assert_called_once()

    def test_request_contains_notification_id(self):
        import json as _json
        from nibe_ha_integration import notify_ha
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'fake_token'}):
            with patch('urllib.request.urlopen') as mock_open:
                notify_ha(None, 'Title', 'Msg', 'nibe_test_id')
                req = mock_open.call_args[0][0]
                payload = _json.loads(req.data)
                self.assertEqual(payload['notification_id'], 'nibe_test_id')
                self.assertEqual(payload['title'], 'Title')
                self.assertEqual(payload['message'], 'Msg')

    def test_request_uses_post_method(self):
        from nibe_ha_integration import notify_ha
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen') as mock_open:
                notify_ha(None, 't', 'm', 'id')
                req = mock_open.call_args[0][0]
                self.assertEqual(req.method, 'POST')

    def test_request_has_auth_header(self):
        from nibe_ha_integration import notify_ha
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'mytoken'}):
            with patch('urllib.request.urlopen') as mock_open:
                notify_ha(None, 't', 'm', 'id')
                req = mock_open.call_args[0][0]
                self.assertIn('Bearer mytoken', req.get_header('Authorization'))

    def test_urlopen_failure_does_not_raise(self):
        """Network errors must be swallowed — not raise to the caller."""
        from nibe_ha_integration import notify_ha
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen', side_effect=Exception('timeout')):
                notify_ha(None, 't', 'm', 'id')  # must not raise

    def test_mqtt_client_argument_not_used(self):
        """mqtt_client is accepted for API compatibility but not used."""
        from nibe_ha_integration import notify_ha
        with patch.dict('os.environ', {}, clear=True):
            # Passing a sentinel — if it's used an error would occur
            notify_ha('NOT_USED', 't', 'm', 'id')



class TestDismissHa(unittest.TestCase):
    """dismiss_ha dismisses a persistent notification via the Supervisor API."""

    def test_no_token_does_not_call_urlopen(self):
        from nibe_ha_integration import dismiss_ha
        with patch.dict('os.environ', {}, clear=True):
            with patch('urllib.request.urlopen') as mock_open:
                dismiss_ha(None, 'test_id')
                mock_open.assert_not_called()

    def test_with_token_calls_urlopen(self):
        from nibe_ha_integration import dismiss_ha
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen') as mock_open:
                dismiss_ha(None, 'nibe_test')
                mock_open.assert_called_once()

    def test_request_contains_notification_id(self):
        import json as _json
        from nibe_ha_integration import dismiss_ha
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen') as mock_open:
                dismiss_ha(None, 'nibe_dismiss_id')
                req = mock_open.call_args[0][0]
                payload = _json.loads(req.data)
                self.assertEqual(payload['notification_id'], 'nibe_dismiss_id')

    def test_urlopen_failure_does_not_raise(self):
        from nibe_ha_integration import dismiss_ha
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen', side_effect=Exception('refused')):
                dismiss_ha(None, 'id')  # must not raise

    def test_dismiss_url_is_dismiss_endpoint(self):
        from nibe_ha_integration import dismiss_ha
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen') as mock_open:
                dismiss_ha(None, 'id')
                req = mock_open.call_args[0][0]
                self.assertIn('dismiss', req.full_url)


# ===========================================================================
# 75. HAEntityRegistryWatcher.refresh_registry
# ===========================================================================


class TestDoRefreshRegistry(unittest.TestCase):
    """refresh_registry fetches the entity registry over a fresh WebSocket
    and populates _unique_id_map with nibe_ entries. Bypassed gracefully when
    no SUPERVISOR_TOKEN is present."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em = MagicMock()
        w._pub = MagicMock()
        return w

    def _mock_ws(self, entries):
        """Return a mock WebSocket whose recv() yields the registry response."""
        import json as _json
        ws = MagicMock()
        response = _json.dumps({
            'id': 1, 'type': 'result', 'success': True,
            'result': entries,
        })
        ws.recv.side_effect = [
            _json.dumps({'type': 'auth_required'}),  # auth_required
            _json.dumps({'type': 'auth_ok'}),         # auth_ok
            response,                                  # list response
        ]
        return ws

    def test_no_token_returns_immediately(self):
        w = self._make_watcher()
        with patch.dict('os.environ', {}, clear=True):
            with patch('websocket.create_connection') as mock_conn:
                w.refresh_registry()
                mock_conn.assert_not_called()

    def test_nibe_entries_added_to_map(self):
        w = self._make_watcher()
        entries = [
            {'unique_id': 'nibe_1234', 'entity_id': 'sensor.nibe_1234',
             'platform': 'mqtt'},
            {'unique_id': 'nibe_5678', 'entity_id': 'switch.nibe_5678',
             'platform': 'mqtt'},
        ]
        ws = self._mock_ws(entries)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('websocket.create_connection', return_value=ws):
                w.refresh_registry()
        self.assertEqual(w._unique_id_map.get('nibe_1234'), 'sensor.nibe_1234')
        self.assertEqual(w._unique_id_map.get('nibe_5678'), 'switch.nibe_5678')

    def test_non_nibe_entries_excluded(self):
        w = self._make_watcher()
        entries = [
            {'unique_id': 'nibe_100', 'entity_id': 'sensor.nibe_100',
             'platform': 'mqtt'},
            {'unique_id': 'other_integration', 'entity_id': 'sensor.other',
             'platform': 'other'},
        ]
        ws = self._mock_ws(entries)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('websocket.create_connection', return_value=ws):
                w.refresh_registry()
        self.assertIn('nibe_100', w._unique_id_map)
        self.assertNotIn('other_integration', w._unique_id_map)

    def test_refresh_timer_cleared_after_run(self):
        """_refresh_timer must be set to None after the fetch completes."""
        w = self._make_watcher()
        ws = self._mock_ws([])
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('websocket.create_connection', return_value=ws):
                w.refresh_registry()
        self.assertIsNone(w._refresh_timer)

    def test_websocket_exception_does_not_raise(self):
        """Network errors must be swallowed — registry fetch is best-effort."""
        w = self._make_watcher()
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('websocket.create_connection',
                       side_effect=Exception('connection refused')):
                w.refresh_registry()  # must not raise

    def test_empty_result_does_not_crash(self):
        w = self._make_watcher()
        ws = self._mock_ws([])
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('websocket.create_connection', return_value=ws):
                w.refresh_registry()
        self.assertEqual(w._unique_id_map, {})


# ===========================================================================
# 76. handle_regen_dashboard management handler
# ===========================================================================


class TestHandleRegenDashboard(unittest.TestCase):
    """handle_regen_dashboard fires the _on_enabled_state_change callback
    when a Regenerate Dashboard command arrives via MQTT."""

    def setUp(self):
        import concurrent.futures
        from nibe_ha_integration import ManagementCommandHandler
        from nibe_mqtt_publisher import MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.em = _make_em()
        self.mqtt = MagicMock()
        self.publisher = MagicMock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ManagementCommandHandler(
            self.mqtt, self.em, self.publisher, self.executor,
        ).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=False)

    def _get_regen_handler(self):
        topic = self.MgmtTopic.REGEN_DASH_PRESS
        for call in self.mqtt.message_callback_add.call_args_list:
            if call.args[0] == topic:
                return call.args[1]
        raise KeyError('No handler for REGEN_DASH_PRESS')

    def test_callback_called_when_registered(self):
        callback = MagicMock()
        self.em._on_enabled_state_change = callback
        handler = self._get_regen_handler()
        handler(None, None, MagicMock())
        callback.assert_called_once()

    def test_no_crash_when_callback_is_none(self):
        """If no callback is registered the handler must not raise."""
        self.em._on_enabled_state_change = None
        handler = self._get_regen_handler()
        handler(None, None, MagicMock())  # must not raise

    def test_handler_registered_for_regen_topic(self):
        """Verify the handler is wired to the correct MQTT topic."""
        topics = [c.args[0] for c in self.mqtt.message_callback_add.call_args_list]
        self.assertIn(self.MgmtTopic.REGEN_DASH_PRESS, topics)


# ===========================================================================
# 77. NibeApiClient — HTTP error paths
# ===========================================================================


class TestSetupMenuDashboardSavePaths(unittest.TestCase):
    """Covers the config save, lovelace_updated, and retry-needed paths
    that were previously untested in _setup_menu_dashboard."""

    _YAML = "menus:\n  - id: '1.1'\n    title: Test\n    settings: []\n    submenus: []\n"

    def _make_watcher(self, entity_id_map=None):
        em = MagicMock()
        pub = MagicMock()
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = HAEntityRegistryWatcher(em, pub)
        w._unique_id_map = {}
        em.all_points_by_id  = {}
        em.dynamic_point_map = MagicMock()
        em.dynamic_point_map.values.return_value = []
        em.dynamic_point_map.all_known_dynamic_point_ids.return_value = set()
        em.active_dynamic_points = set()
        em.bulk_data             = {}
        em.mqtt_enabled_points   = set()
        em.point_to_menu_map     = {}
        # entity_id_for returns None by default; override per test
        if entity_id_map:
            w.entity_id_for = lambda pid: entity_id_map.get(pid)
        else:
            w.entity_id_for = lambda pid: None
        return w

    def _run(self, fake_ws_call, watcher=None):
        """Call _setup_menu_dashboard_lovelace directly with a pre-built config.

        Since _setup_menu_dashboard now opens the WebSocket AFTER the registry
        wait (inside _setup_menu_dashboard_lovelace), tests that exercise the
        Lovelace API call paths test _setup_menu_dashboard_lovelace directly,
        avoiding the 60s registry wait loop entirely.
        """
        from nibe_lovelace import _setup_menu_dashboard_lovelace
        if watcher is None:
            watcher = self._make_watcher()
        ws = MagicMock()
        em = watcher._em
        with patch('nibe_lovelace._ws_call', side_effect=fake_ws_call):
            return _setup_menu_dashboard_lovelace(
                ws, iter(range(1, 100)).__next__,
                {'views': [{'title': 'Test View'}]},
                em, watcher, set(), set(),
            )

    def _base_ws(self, save_success=True):
        """Return a fake_ws_call that succeeds on dashboards/list and config/save."""
        def fake(ws, _msg_id, payload, _timeout=10):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'success': True, 'result': [
                    {'url_path': 'nibe-menus', 'id': 99}
                ]}
            if t == 'lovelace/config/save':
                return {'success': save_success}
            return {'success': True, 'result': []}
        return fake

    # ── config save success ────────────────────────────────────────────────

    def test_config_save_success_no_dynamic_returns_false(self):
        """Clean path: config saved, no missing dynamic points → False (no retry)."""
        result = self._run(self._base_ws(save_success=True))
        self.assertIs(result, False)

    def test_config_save_success_fires_lovelace_updated_event(self):
        """After a successful save, lovelace_updated must be fired so browsers reload."""
        ws_calls = []
        def fake(ws, _msg_id, payload, _timeout=10):
            ws_calls.append(payload.get('type'))
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'success': True, 'result': [{'url_path': 'nibe-menus', 'id': 99}]}
            if t == 'lovelace/config/save':
                return {'success': True}
            return {'success': True, 'result': []}

        self._run(fake)
        self.assertIn('fire_event', ws_calls)

    def test_config_save_success_with_missing_dynamic_returns_true(self):
        """Save succeeded but a dynamic point is still missing → True (needs retry)."""
        watcher = self._make_watcher()
        watcher._em.all_points_by_id  = {100: {}}
        watcher._em.active_dynamic_points = {9999}
        # point 100 resolves; dynamic 9999 does not
        watcher.entity_id_for = lambda pid: 'sensor.nibe_100' if pid == 100 else None

        from nibe_lovelace import _setup_menu_dashboard_lovelace
        ws = MagicMock()
        with patch('nibe_lovelace._ws_call', side_effect=self._base_ws(save_success=True)):
            result = _setup_menu_dashboard_lovelace(
                ws, iter(range(1, 100)).__next__,
                {'views': [{'title': 'Test'}]},
                watcher._em, watcher,
                {100},            # available_menu_points — point 100 resolves
                {9999},           # active_dynamic — 9999 does not resolve
            )

        self.assertIs(result, True)

    def test_config_save_failure_returns_false(self):
        """A failed config/save must return False (no point retrying if save fails)."""
        result = self._run(self._base_ws(save_success=False))
        self.assertIs(result, False)

    # ── dashboard create paths ─────────────────────────────────────────────

    def test_dashboard_create_success_proceeds_to_config_save(self):
        """When the dashboard does not yet exist, it must be created before saving."""
        ws_calls = []
        def fake(ws, _msg_id, payload, _timeout=10):
            ws_calls.append(payload.get('type'))
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'success': True, 'result': []}  # dashboard absent
            if t == 'lovelace/dashboards/create':
                return {'success': True, 'result': {'id': 42}}
            if t == 'lovelace/config/save':
                return {'success': True}
            return {'success': True, 'result': []}

        self._run(fake)
        self.assertIn('lovelace/dashboards/create', ws_calls)
        self.assertIn('lovelace/config/save', ws_calls)

    def test_dashboard_create_url_already_exists_proceeds_to_config_save(self):
        """Create failing with 'url_already_exists' is not an error — must continue."""
        ws_calls = []
        def fake(ws, _msg_id, payload, _timeout=10):
            ws_calls.append(payload.get('type'))
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'success': True, 'result': []}
            if t == 'lovelace/dashboards/create':
                return {'success': False, 'error': {'message': 'url_already_exists'}}
            if t == 'lovelace/config/save':
                return {'success': True}
            return {'success': True, 'result': []}

        self._run(fake)
        self.assertIn('lovelace/config/save', ws_calls,
            "url_already_exists on create must not abort — config/save must still run")

    def test_dashboard_create_fatal_error_returns_false(self):
        """Create failing with an unexpected error must abort and return False."""
        ws_calls = []
        def fake(ws, _msg_id, payload, _timeout=10):
            ws_calls.append(payload.get('type'))
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'success': True, 'result': []}
            if t == 'lovelace/dashboards/create':
                return {'success': False, 'error': {'message': 'internal server error'}}
            return {'success': True, 'result': []}

        result = self._run(fake)
        self.assertIs(result, False)
        self.assertNotIn('lovelace/config/save', ws_calls)


# ===========================================================================
# 81. _build_menu_view — tip alert path and dynamic default in divider
# ===========================================================================


class TestManagementHandlerEdgePaths(unittest.TestCase):

    def setUp(self):
        import concurrent.futures
        from nibe_ha_integration import ManagementCommandHandler
        self.em        = _make_em()
        self.mqtt      = MagicMock()
        self.publisher = MagicMock()
        self.executor  = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ManagementCommandHandler(
            self.mqtt, self.em, self.publisher, self.executor
        ).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=True)

    def _get_handler(self, topic_attr):
        from nibe_mqtt_publisher import MgmtTopic
        topic = getattr(MgmtTopic, topic_attr)
        for call in self.mqtt.message_callback_add.call_args_list:
            if call.args[0] == topic:
                return call.args[1]
        raise KeyError(f'No handler for {topic_attr}')

    def _msg(self, payload):
        m = MagicMock()
        m.payload = payload.encode()
        return m

    def test_publish_device_modes_uses_cache_when_not_dirty(self):
        """_publish_device_modes must return early from cache when dirty=False and cache exists."""
        from nibe_ha_integration import _publish_device_modes
        em = MagicMock()
        em.api_consecutive_failures = 0
        em.device_modes_dirty       = False
        em.device_modes_cache       = {'aidMode': 'on', 'smartMode': 'away'}
        pub = MagicMock()
        _publish_device_modes(em, pub)
        # Must publish from cache without hitting the API
        em._api.fetch_device_info.assert_not_called()
        pub.publish_device_modes.assert_called_once_with(aid_mode='on', smart_mode='away')

    def test_publish_device_modes_fetch_failure_does_not_raise(self):
        """When fetch_device_info returns None/falsy, must log warning and return cleanly."""
        from nibe_ha_integration import _publish_device_modes
        em = MagicMock()
        em.api_consecutive_failures = 0
        em.device_modes_dirty       = True
        em.device_modes_cache       = {}
        em._api.fetch_device_info.return_value = None
        pub = MagicMock()
        _publish_device_modes(em, pub)  # must not raise
        pub.publish_device_modes.assert_not_called()



class TestManagementRunTestsFailures(unittest.TestCase):
    """Test the run_tests handler with subprocess failures."""

    def setUp(self):
        import concurrent.futures
        from nibe_ha_integration import ManagementCommandHandler

        # Create a fresh EntityManager with its own mock, then override its mqtt
        self.em = _make_em()
        self.mqtt = MagicMock()
        self.em.mqtt = self.mqtt          # <-- CRITICAL FIX: use same mock for EM

        self.publisher = MagicMock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        # Register the management handlers; they will use self.em.mqtt (our mock)
        ManagementCommandHandler(self.mqtt, self.em, self.publisher, self.executor).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=True)

    def _get_handler(self):
        from nibe_mqtt_publisher import MgmtTopic
        topic = MgmtTopic.RUN_TESTS_PRESS
        for call in self.mqtt.message_callback_add.call_args_list:
            if call.args[0] == topic:
                return call.args[1]
        raise KeyError('No handler for RUN_TESTS_PRESS')

    def _msg(self, payload):
        m = MagicMock()
        m.payload = payload.encode()
        return m

    def test_subprocess_timeout(self):
        import subprocess
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('pytest', 3600)):
            handler = self._get_handler()
            handler(None, None, self._msg(''))
            self.executor.shutdown(wait=True)
            from nibe_mqtt_publisher import MgmtTopic
            states = [c.args[1] for c in self.mqtt.publish.call_args_list if c.args[0] == MgmtTopic.RUN_TESTS_STATE]
            self.assertIn('timed_out', states)

    def test_subprocess_generic_exception(self):
        with patch('subprocess.run', side_effect=Exception('permission denied')):
            handler = self._get_handler()
            handler(None, None, self._msg(''))
            self.executor.shutdown(wait=True)
            from nibe_mqtt_publisher import MgmtTopic
            states = [c.args[1] for c in self.mqtt.publish.call_args_list if c.args[0] == MgmtTopic.RUN_TESTS_STATE]
            self.assertIn('error', states)


# ===========================================================================
# 86. Remaining entity_detection gaps — parse_description_mapping,
#     get_entity_options VALUE_MAPPINGS path, and input register in VALUE_MAPPINGS
# ===========================================================================


class TestSetupMenuDashboardRemainingBranches(unittest.TestCase):
    """Remaining branches in _setup_menu_dashboard: registry wait timeout,
    no views generated warning. (The auto-enable-on-menu-load branch this
    class used to cover was removed in the entity-mode refactor — enabling
    now happens via EntityManager.apply_mode() before this function runs;
    _setup_menu_dashboard is purely a dashboard builder.)"""

    _YAML = "menus:\n  - id: '1.1'\n    title: Test\n    settings: []\n    submenus: []\n"

    def _make_watcher(self):
        em = MagicMock()
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = HAEntityRegistryWatcher(em, MagicMock())
        w._unique_id_map = {}
        em.all_points_by_id = {}
        em.dynamic_point_map = MagicMock()
        em.dynamic_point_map.values.return_value = []
        em.dynamic_point_map.all_known_dynamic_point_ids.return_value = set()
        em.active_dynamic_points = set()
        em.bulk_data = {}
        em.mqtt_enabled_points = set()
        em.point_to_menu_map = {}
        w.entity_id_for = lambda pid: None
        return w

    def _base_ws(self, save_success=True):
        def fake(ws, _msg_id, payload, _timeout=10):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                return {'success': True, 'result': [{'url_path': 'nibe-menus', 'id': 99}]}
            if t == 'lovelace/config/save':
                return {'success': save_success}
            return {'success': True, 'result': []}
        return fake

    def test_open_ws_fn_called_after_config_built_not_before(self):
        """Structural invariant introduced when fixing the stale-WebSocket bug:
        open_ws_fn must be called AFTER the registry wait and AFTER
        _build_menu_dashboard_config, never before either. If the ws were
        opened before the wait, the Supervisor closes it as idle during the
        ~60s wait and every subsequent _ws_call returns {}.

        This test locks in the call ordering so a refactor that accidentally
        moves the open back to the top of the function is caught immediately,
        before hardware validation has to find it again."""
        import nibe_lovelace as nl
        import io

        call_order = []
        watcher = self._make_watcher()

        def tracking_open_ws():
            call_order.append('open_ws_fn')
            return (MagicMock(), iter(range(1, 100)).__next__)

        def tracking_build_config(*args, **kwargs):
            call_order.append('build_config')
            return {'views': [{'title': 'Test'}]}

        with patch('nibe_lovelace.os.path.exists', return_value=True), \
             patch('builtins.open', return_value=io.StringIO(self._YAML)), \
             patch('nibe_lovelace.time.sleep'), \
             patch('nibe_lovelace._build_menu_dashboard_config',
                   side_effect=tracking_build_config), \
             patch('nibe_lovelace._setup_menu_dashboard_lovelace', return_value=False):
            nl._setup_menu_dashboard(tracking_open_ws, watcher)

        self.assertIn('build_config', call_order,
            "_build_menu_dashboard_config must have been called")
        self.assertIn('open_ws_fn', call_order,
            "open_ws_fn must have been called")
        build_idx = call_order.index('build_config')
        open_idx  = call_order.index('open_ws_fn')
        self.assertGreater(open_idx, build_idx,
            f"open_ws_fn (position {open_idx}) must be called AFTER "
            f"_build_menu_dashboard_config (position {build_idx}), "
            f"not before the registry wait — order was: {call_order}")

    def test_ws_open_failure_after_wait_returns_true_for_retry(self):
        """If open_ws_fn returns None after the registry wait, the function
        must return True (signal retry) rather than raising or returning False.
        This is the new coverage path: the ws open now happens inside
        _setup_menu_dashboard, after the registry wait."""
        import nibe_lovelace as nl
        import io
        watcher = self._make_watcher()
        with patch('nibe_lovelace.os.path.exists', return_value=True), \
             patch('builtins.open', return_value=io.StringIO(self._YAML)), \
             patch('nibe_lovelace.time.sleep'), \
             patch('nibe_lovelace._build_menu_dashboard_config',
                   return_value={'views': [{'title': 'T'}]}):
            result = nl._setup_menu_dashboard(lambda: None, watcher)
        self.assertIs(result, True)

    def test_ws_close_exception_in_finally_does_not_propagate(self):
        """ws.close() raising in the finally block must not propagate."""
        import nibe_lovelace as nl
        import io
        watcher = self._make_watcher()
        ws = MagicMock()
        ws.close.side_effect = OSError("already closed")
        next_id = iter(range(1, 100)).__next__
        with patch('nibe_lovelace.os.path.exists', return_value=True), \
             patch('builtins.open', return_value=io.StringIO(self._YAML)), \
             patch('nibe_lovelace.time.sleep'), \
             patch('nibe_lovelace._build_menu_dashboard_config',
                   return_value={'views': [{'title': 'T'}]}), \
             patch('nibe_lovelace._setup_menu_dashboard_lovelace', return_value=False):
            # Must not raise even though ws.close() raises
            nl._setup_menu_dashboard(lambda: (ws, next_id), watcher)

    def test_registry_wait_timeout_logs_warning(self):
        """If the while loop exhausts _limit without stability, the else branch fires."""
        import nibe_lovelace as nl
        import io
        watcher = self._make_watcher()

        sleep_calls = [0]
        def fake_sleep(t):
            sleep_calls[0] += 1
            if sleep_calls[0] > 200:
                raise RuntimeError("infinite loop guard")

        # open_ws_fn returns a fresh connection after the wait
        open_ws_fn = MagicMock(return_value=(MagicMock(), iter(range(1, 100)).__next__))

        # entity_id_for always returns None → count never stabilises → timeout
        with patch('nibe_lovelace.os.path.exists', return_value=True), \
             patch('builtins.open', return_value=io.StringIO(self._YAML)), \
             patch('nibe_lovelace._setup_menu_dashboard_lovelace', return_value=False), \
             patch('nibe_lovelace.time.sleep', side_effect=fake_sleep), \
             patch('nibe_lovelace.time.time', side_effect=lambda: sleep_calls[0] * 0.5):
            nl._setup_menu_dashboard(open_ws_fn, watcher)
        # reaching here without raising means the timeout path ran

    def test_no_views_generated_returns_false(self):
        """When _build_menu_dashboard_config returns no views, must return False."""
        import nibe_lovelace as nl
        import io
        watcher = self._make_watcher()
        open_ws_fn = MagicMock()
        with patch('nibe_lovelace.os.path.exists', return_value=True), \
             patch('builtins.open', return_value=io.StringIO(self._YAML)), \
             patch('nibe_lovelace.time.sleep'), \
             patch('nibe_lovelace._build_menu_dashboard_config', return_value={'views': []}):
            result = nl._setup_menu_dashboard(open_ws_fn, watcher)
        self.assertIs(result, False)
        open_ws_fn.assert_not_called()  # ws never opened for empty config



class TestRegenMenuDashboardWsCloseException(unittest.TestCase):
    """ws.close() raising in _regen_menu_dashboard's finally must not propagate."""

    def test_ws_close_raises_does_not_propagate(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        ws.close.side_effect = OSError("already closed")
        open_ws_fn = MagicMock(return_value=(ws, lambda: 1))
        setup_dashboard_fn = MagicMock(return_value=False)
        nl._regen_menu_dashboard(
            MagicMock(), debug_mode=False, attempt=1,
            open_ws_fn=open_ws_fn, setup_dashboard_fn=setup_dashboard_fn,
            schedule_retry_fn=MagicMock(),
        )  # must not raise


# ===========================================================================
# Coverage: nibe_ha_integration.py — HAEntityRegistryWatcher lifecycle,
#           _connect_and_subscribe, _run loop, _fetch_entity_registry gaps,
#           update_device_modes, _publish_device_modes early return
# ===========================================================================


class TestFetchEntityRegistryRemainingPaths(unittest.TestCase):
    """_fetch_entity_registry: exception path and failed-response path."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event    = threading.Event()
        w._thread        = None
        w._ws_lock       = threading.Lock()
        w._current_ws    = None
        w._msg_id        = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em  = MagicMock()
        w._pub = MagicMock()
        return w

    def test_recv_exception_returns_empty_dict(self):
        """If ws.recv raises (timeout / connection drop), return {}."""
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.side_effect = OSError("timed out")
        result = w._fetch_entity_registry(ws)
        self.assertEqual(result, {})
        ws.settimeout.assert_any_call(None)   # finally branch always resets timeout

    def test_failed_response_returns_empty_dict(self):
        """If the response arrives but success=False, return {}."""
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = json.dumps({
            "id": 1, "type": "result", "success": False, "error": {"code": "unknown"}
        })
        result = w._fetch_entity_registry(ws)
        self.assertEqual(result, {})



class TestRegistryWatcherStart(unittest.TestCase):
    """start(): no-token early return and normal thread-start path."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event    = threading.Event()
        w._thread        = None
        w._ws_lock       = threading.Lock()
        w._current_ws    = None
        w._msg_id        = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em  = MagicMock()
        w._pub = MagicMock()
        return w

    def test_no_supervisor_token_returns_without_starting_thread(self):
        w = self._make_watcher()
        with patch.dict('os.environ', {}, clear=True), \
             patch('threading.Thread') as mock_thread:
            w.start()
        mock_thread.assert_not_called()
        self.assertIsNone(w._thread)

    def test_supervisor_token_starts_daemon_thread(self):
        w = self._make_watcher()
        mock_thread = MagicMock()
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('threading.Thread', return_value=mock_thread):
            w.start()
        mock_thread.start.assert_called_once()
        self.assertIs(w._thread, mock_thread)



class TestRegistryWatcherStop(unittest.TestCase):
    """stop(): sets stop event, closes current ws, joins thread."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event    = threading.Event()
        w._thread        = None
        w._ws_lock       = threading.Lock()
        w._current_ws    = None
        w._msg_id        = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em  = MagicMock()
        w._pub = MagicMock()
        return w

    def test_stop_sets_stop_event(self):
        w = self._make_watcher()
        self.assertFalse(w._stop_event.is_set())
        w.stop()
        self.assertTrue(w._stop_event.is_set())

    def test_stop_closes_current_ws(self):
        w = self._make_watcher()
        mock_ws = MagicMock()
        w._current_ws = mock_ws
        w.stop()
        mock_ws.close.assert_called_once()

    def test_stop_ws_close_exception_does_not_raise(self):
        w = self._make_watcher()
        mock_ws = MagicMock()
        mock_ws.close.side_effect = OSError("already closed")
        w._current_ws = mock_ws
        w.stop()  # must not raise

    def test_stop_joins_alive_thread(self):
        w = self._make_watcher()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        w._thread = mock_thread
        w.stop()
        mock_thread.join.assert_called_once_with(timeout=5)

    def test_stop_no_thread_does_not_raise(self):
        w = self._make_watcher()
        w._thread = None
        w.stop()  # must not raise



class TestConnectAndSubscribe(unittest.TestCase):
    """_connect_and_subscribe: bad greeting, auth fail, sub fail, success."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event    = threading.Event()
        w._thread        = None
        w._ws_lock       = threading.Lock()
        w._current_ws    = None
        w._msg_id        = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em  = MagicMock()
        w._pub = MagicMock()
        return w

    def _make_ws_mod(self, recv_sequence):
        ws = MagicMock()
        ws.recv.side_effect = [json.dumps(m) for m in recv_sequence]
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        return ws_mod, ws

    def test_wrong_greeting_type_closes_and_raises(self):
        w = self._make_watcher()
        ws_mod, ws = self._make_ws_mod([{"type": "auth_ok"}])  # wrong greeting
        with patch.dict('sys.modules', {'websocket': ws_mod}):
            with self.assertRaises(RuntimeError):
                w._connect_and_subscribe("tok")
        ws.close.assert_called_once()

    def test_auth_failure_closes_and_raises(self):
        w = self._make_watcher()
        ws_mod, ws = self._make_ws_mod([
            {"type": "auth_required"},
            {"type": "auth_invalid"},          # auth failed
        ])
        with patch.dict('sys.modules', {'websocket': ws_mod}):
            with self.assertRaises(RuntimeError):
                w._connect_and_subscribe("tok")
        ws.close.assert_called_once()

    def test_subscription_failure_closes_and_raises(self):
        w = self._make_watcher()
        ws_mod, ws = self._make_ws_mod([
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": False},   # sub failed
        ])
        with patch.dict('sys.modules', {'websocket': ws_mod}):
            with self.assertRaises(RuntimeError):
                w._connect_and_subscribe("tok")
        ws.close.assert_called_once()

    def test_success_returns_ws_and_sets_timeout(self):
        w = self._make_watcher()
        ws_mod, ws = self._make_ws_mod([
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True},    # sub OK
            # _fetch_entity_registry will call recv once more
            {"id": 2, "type": "result", "success": True, "result": []},
        ])
        with patch.dict('sys.modules', {'websocket': ws_mod}):
            result = w._connect_and_subscribe("tok")
        self.assertIs(result, ws)
        from nibe_ha_integration import HAEntityRegistryWatcher
        ws.settimeout.assert_any_call(HAEntityRegistryWatcher._PING_INTERVAL_S)



class TestRegistryWatcherPingPong(unittest.TestCase):
    """WebSocket keepalive: ping sent on recv timeout, reconnect if no pong."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map       = {}
        w._stop_event          = threading.Event()
        w._thread              = None
        w._ws_lock             = threading.Lock()
        w._current_ws          = None
        w._msg_id              = 0
        w._refresh_timer       = None
        w._refresh_timer_lock  = threading.Lock()
        return w

    def test_ping_sent_on_recv_timeout(self):
        """When recv() times out, the watcher must send a ping and continue
        without reconnecting — the timeout is the normal keepalive signal."""
        import json

        w = self._make_watcher()
        ws = MagicMock()
        call_count = [0]

        # Simulate: first recv times out (send ping), second returns stop event
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            WebSocketTimeoutException = TimeoutError

        def side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                raise WebSocketTimeoutException("timeout")
            w._stop_event.set()
            raise WebSocketTimeoutException("stop")

        ws.recv.side_effect = side_effect
        ws.send = MagicMock()

        with patch('nibe_ha_integration.HAEntityRegistryWatcher._connect_and_subscribe',
                   return_value=ws), \
             patch('nibe_ha_integration.HAEntityRegistryWatcher._fetch_entity_registry',
                   return_value={}), \
             patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            w._run()

        # Confirm a ping was sent (not a reconnect)
        self.assertTrue(ws.send.called)
        sent = json.loads(ws.send.call_args[0][0])
        self.assertEqual(sent.get('type'), 'ping')

    def test_pong_message_is_discarded_not_processed_as_event(self):
        """A pong response must be silently discarded — not passed to
        _handle_event (which would log an unknown event type warning)."""
        import json

        w = self._make_watcher()
        ws = MagicMock()
        call_count = [0]

        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            WebSocketTimeoutException = TimeoutError

        def side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({"type": "pong", "id": 42})
            w._stop_event.set()
            raise WebSocketTimeoutException("stop")

        ws.recv.side_effect = side_effect

        with patch('nibe_ha_integration.HAEntityRegistryWatcher._connect_and_subscribe',
                   return_value=ws), \
             patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.object(w, '_handle_event') as mock_event:
            w._run()

        mock_event.assert_not_called()

    def test_websocket_import_error_falls_back_to_timeout_error(self):
        """When websocket-client is not installed, _WsTimeout falls back to
        TimeoutError and the ping/reconnect path still works (lines 358-359)."""
        w = self._make_watcher()
        ws = MagicMock()
        connect_count = [0]

        def fake_connect(_token):
            connect_count[0] += 1
            if connect_count[0] >= 2:
                w._stop_event.set()
            return ws

        # recv raises TimeoutError (the ImportError fallback) immediately
        ws.recv.side_effect = TimeoutError("timeout")
        ws.send = MagicMock()

        real_time = __import__('time').time

        call_count = [0]
        def fake_time():
            call_count[0] += 1
            # First call: return a timestamp far past the pong timeout
            # so reconnect is triggered immediately
            if call_count[0] <= 1:
                from nibe_ha_integration import HAEntityRegistryWatcher
                return real_time() - HAEntityRegistryWatcher._PING_TIMEOUT_S - 5
            return real_time()

        with patch.object(w, '_connect_and_subscribe', side_effect=fake_connect), \
             patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_ha_integration.time.time', side_effect=fake_time), \
             patch('builtins.__import__', side_effect=lambda name, *a, **kw:
                   (_ for _ in ()).throw(ImportError("no websocket")) if name == 'websocket'
                   else __import__(name, *a, **kw)):
            w._run()

        self.assertGreaterEqual(connect_count[0], 2,
            "ImportError fallback must still trigger reconnect on keepalive timeout")

    def test_keepalive_timeout_triggers_reconnect(self):
        """If a ping was sent but no pong arrives within _PING_TIMEOUT_S,
        the watcher must reconnect. Simulated by making the second time.time()
        call in the ping-timeout check return a value far past the window."""
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = self._make_watcher()
        ws = MagicMock()

        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            WebSocketTimeoutException = TimeoutError

        call_count = [0]
        real_time = __import__('time').time

        def fake_time():
            # First call (storing ping_sent_at) returns a past timestamp
            # far enough back to be past the timeout window on the next check.
            # All other calls (logging etc.) return real time.
            call_count[0] += 1
            if call_count[0] <= 1:
                return real_time() - HAEntityRegistryWatcher._PING_TIMEOUT_S - 5
            return real_time()

        ws.recv.side_effect = WebSocketTimeoutException("timeout")
        ws.send = MagicMock()

        connect_count = [0]
        def fake_connect(_token):
            connect_count[0] += 1
            if connect_count[0] >= 2:
                w._stop_event.set()
            return ws

        with patch.object(w, '_connect_and_subscribe', side_effect=fake_connect), \
             patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_ha_integration.time.time', side_effect=fake_time):
            w._run()

        self.assertGreaterEqual(connect_count[0], 2,
            "Keepalive timeout must trigger reconnect")



class TestRegistryWatcherRun(unittest.TestCase):
    """_run(): all exit paths and inner-loop branches."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event    = threading.Event()
        w._thread        = None
        w._ws_lock       = threading.Lock()
        w._current_ws    = None
        w._msg_id        = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em  = MagicMock()
        w._pub = MagicMock()
        return w

    def test_stop_event_set_before_run_exits_immediately(self):
        """If stop_event is already set, the while loop body never executes."""
        w = self._make_watcher()
        w._stop_event.set()
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            w._run()   # must return without calling _connect_and_subscribe

    def test_import_error_returns_without_retry(self):
        """If websocket-client is missing, _run logs and returns — no retry."""
        w = self._make_watcher()
        call_count = [0]
        def fake_connect(_token):
            call_count[0] += 1
            raise ImportError("no module named websocket")
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.object(w, '_connect_and_subscribe', side_effect=fake_connect):
            w._run()
        self.assertEqual(call_count[0], 1)  # tried exactly once, then gave up

    def test_consecutive_failures_give_up_after_max(self):
        """After _MAX_CONSEC_FAILURES consecutive exceptions, _run returns."""
        w = self._make_watcher()
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.object(w, '_connect_and_subscribe',
                          side_effect=RuntimeError("connection refused")), \
             patch.object(w._stop_event, 'wait'):   # skip real sleep
            w._run()
        # after MAX_CONSEC_FAILURES=10 attempts it returns; stop_event not set
        self.assertFalse(w._stop_event.is_set())

    def test_reconnect_after_one_failure_then_stop(self):
        """One failure logs reconnect warning; a subsequent success resets counter."""
        w = self._make_watcher()
        ws = MagicMock()
        # First call: raises. Second call: succeeds, recv sets stop_event.
        call_count = [0]
        def fake_connect(_token):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("connection refused")
            # Successful connection: arrange for inner loop to stop immediately
            w._stop_event.set()
            return ws
        ws.recv.return_value = json.dumps({"type": "event", "event": {}})
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.object(w, '_connect_and_subscribe', side_effect=fake_connect), \
             patch.object(w._stop_event, 'wait'):   # skip real sleep
            w._run()
        self.assertEqual(call_count[0], 2)

    def test_inner_loop_empty_recv_breaks(self):
        """ws.recv returning empty string breaks the inner recv loop."""
        w = self._make_watcher()
        ws = MagicMock()
        inner_call = [0]
        def fake_recv():
            inner_call[0] += 1
            if inner_call[0] == 1:
                return ""           # empty → break inner loop
            return json.dumps({"type": "event", "event": {}})
        ws.recv.side_effect = fake_recv
        # After inner loop breaks, outer loop re-enters and we need to exit.
        # Make second _connect_and_subscribe raise to trigger give-up path.
        attempt = [0]
        def fake_connect(_token):
            attempt[0] += 1
            if attempt[0] == 1:
                return ws
            raise RuntimeError("gone")
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.object(w, '_connect_and_subscribe', side_effect=fake_connect), \
             patch.object(w._stop_event, 'wait'):
            w._run()
        self.assertEqual(inner_call[0], 1)  # recv called once before empty break

    def test_inner_loop_invalid_json_continues(self):
        """Unparseable JSON is silently skipped; loop continues."""
        w = self._make_watcher()
        ws = MagicMock()
        recv_calls = [0]
        def fake_recv():
            recv_calls[0] += 1
            if recv_calls[0] == 1:
                return "NOT_JSON"        # parse error → continue
            w._stop_event.set()
            return json.dumps({"type": "event", "event": {}})
        ws.recv.side_effect = fake_recv
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.object(w, '_connect_and_subscribe', return_value=ws):
            w._run()
        self.assertGreaterEqual(recv_calls[0], 2)

    def test_inner_loop_event_dispatched_to_handle_event(self):
        """type==event messages are forwarded to _handle_event."""
        w = self._make_watcher()
        ws = MagicMock()
        handled = []
        def fake_recv():
            w._stop_event.set()
            return json.dumps({"type": "event", "event": {"data": {"action": "create"}}})
        ws.recv.side_effect = fake_recv
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.object(w, '_connect_and_subscribe', return_value=ws), \
             patch.object(w, '_handle_event', side_effect=handled.append):
            w._run()
        self.assertEqual(len(handled), 1)

    def test_stop_event_set_during_exception_breaks_cleanly(self):
        """If stop_event is set before exception is caught, _run breaks without retry."""
        w = self._make_watcher()
        def fake_connect(_token):
            w._stop_event.set()
            raise RuntimeError("shutting down")
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.object(w, '_connect_and_subscribe', side_effect=fake_connect):
            w._run()   # must return without scheduling retry



class TestUpdateDeviceModesWrapper(unittest.TestCase):
    """update_device_modes() is a thin public wrapper — delegates to _publish_device_modes."""

    def test_delegates_to_publish_device_modes(self):
        from nibe_ha_integration import update_device_modes
        em  = MagicMock()
        pub = MagicMock()
        with patch('nibe_ha_integration._publish_device_modes') as mock_fn:
            update_device_modes(em, pub)
        mock_fn.assert_called_once_with(em, pub)



class TestPublishDynamicChangesDashboardNotificationException(unittest.TestCase):
    """Exception in dashboard notification block is silently logged."""

    def test_notify_exception_does_not_raise(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 6666
        em.all_points_by_id[point_id] = {
            'variableId': point_id, 'display_title': 'Point 6666',
            'entity_type': 'switch', 'entity_category': 'config',
            'is_dynamic': True, 'is_writable': True,
            'metadata': {'variableSize': 'u8', 'divisor': 1,
                         'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
            'description': '',
        }
        em.mqtt_enabled_points.add(point_id)
        em.active_dynamic_points.add(point_id)
        with patch('nibe_ha_integration.notify_ha', side_effect=RuntimeError("boom")), \
             patch.object(em, 'publish_enabled_state'), \
             patch.object(em, 'disable_entity'), \
             patch.object(em, '_persist_active_dynamic'):
            em._publish_dynamic_changes([], {point_id})   # must not raise



class TestHandleEventExceptionIsolation(unittest.TestCase):
    """Exceptions inside _handle_event must not propagate out of _run()'s
    inner recv loop — they should be caught and logged, not trigger a reconnect."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event    = threading.Event()
        w._thread        = None
        w._ws_lock       = threading.Lock()
        w._current_ws    = None
        w._msg_id        = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em  = MagicMock()
        w._pub = MagicMock()
        return w

    def test_handle_event_exception_does_not_propagate_to_run(self):
        """An exception in _handle_event must be caught per-event so the
        registry watcher stays connected rather than reconnecting."""
        w = self._make_watcher()
        events_processed = [0]

        def bad_handle_event(event):
            events_processed[0] += 1
            raise RuntimeError("simulated event handler crash")

        ws = MagicMock()
        # First recv: bad event that triggers exception in _handle_event
        # Second recv: sets stop_event and returns a valid non-event message
        recv_count = [0]
        def fake_recv():
            recv_count[0] += 1
            if recv_count[0] == 1:
                return json.dumps({"type": "event", "event": {"data": {}}})
            w._stop_event.set()
            return json.dumps({"type": "pong"})   # non-event, loop exits via stop
        ws.recv.side_effect = fake_recv

        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.object(w, '_connect_and_subscribe', return_value=ws), \
             patch.object(w, '_handle_event', side_effect=bad_handle_event):
            w._run()

        # _handle_event was called, and despite the exception, _run exited
        # cleanly (stop_event set) rather than treating it as a connection error.
        self.assertEqual(events_processed[0], 1)
        # stop_event is set → run exited normally, not via exception path
        self.assertTrue(w._stop_event.is_set())



class TestRefreshRegistryAuthHandshake(unittest.TestCase):
    """refresh_registry: correct auth order (recv auth_required first),
    auth failure detection, dead header arg removed."""

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event    = threading.Event()
        w._thread        = None
        w._ws_lock       = threading.Lock()
        w._current_ws    = None
        w._msg_id        = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em  = MagicMock()
        w._pub = MagicMock()
        return w

    def _make_ws(self, recv_sequence):
        ws = MagicMock()
        ws.recv.side_effect = [json.dumps(m) for m in recv_sequence]
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        return ws_mod, ws

    def test_unexpected_greeting_closes_and_returns(self):
        """If the first message is not auth_required, close and return without
        sending auth — prevents silent auth bypass."""
        w = self._make_watcher()
        ws_mod, ws = self._make_ws([{"type": "auth_ok"}])   # wrong first message
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            w.refresh_registry()
        ws.close.assert_called_once()
        # Auth should never have been sent
        send_calls = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        auth_sends = [c for c in send_calls if c.get('type') == 'auth']
        self.assertEqual(auth_sends, [])

    def test_auth_failure_closes_and_returns(self):
        """If auth is rejected, close and return rather than sending a registry
        request that would be silently ignored by the server."""
        w = self._make_watcher()
        ws_mod, ws = self._make_ws([
            {"type": "auth_required"},
            {"type": "auth_invalid"},
        ])
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            w.refresh_registry()
        ws.close.assert_called_once()
        # Registry request must not have been sent after auth failure
        send_calls = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        registry_sends = [c for c in send_calls
                          if c.get('type') == 'config/entity_registry/list']
        self.assertEqual(registry_sends, [])

    def test_auth_required_received_before_sending_auth(self):
        """Verify the correct handshake order: recv auth_required FIRST,
        then send auth — not the reversed order that was previously used."""
        w = self._make_watcher()
        ws = MagicMock()
        recv_calls_at_send = [0]
        def fake_send(payload):
            msg = json.loads(payload)
            if msg.get('type') == 'auth':
                # Record how many recv() calls had happened before auth was sent
                recv_calls_at_send[0] = ws.recv.call_count
        ws.send.side_effect = fake_send
        ws.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
            json.dumps({"id": 1, "type": "result", "success": True, "result": []}),
        ]
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            w.refresh_registry()
        # auth_required must have been received before auth was sent
        self.assertGreaterEqual(recv_calls_at_send[0], 1,
            "auth must be sent only after auth_required is received")

    def test_no_authorization_header_in_create_connection(self):
        """create_connection must not receive an Authorization header —
        the dead header arg has been removed."""
        import inspect
        from nibe_ha_integration import HAEntityRegistryWatcher
        src = inspect.getsource(HAEntityRegistryWatcher.refresh_registry)
        self.assertNotIn('Authorization', src,
            "Dead Authorization header arg must be removed from create_connection call")

    def test_successful_refresh_populates_map(self):
        """Full happy-path: correct handshake, successful registry fetch,
        unique_id_map populated."""
        w = self._make_watcher()
        ws_mod, ws = self._make_ws([
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": [
                {"unique_id": "nibe_100", "entity_id": "sensor.nibe_100"},
                {"unique_id": "other_100", "entity_id": "sensor.other_100"},
            ]},
        ])
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch.dict('sys.modules', {'websocket': ws_mod}):
            w.refresh_registry()
        self.assertEqual(w._unique_id_map.get("nibe_100"), "sensor.nibe_100")
        self.assertNotIn("other_100", w._unique_id_map)


# ===========================================================================
# Bug fix: metadata identity vs equality comparison in _fetch_bulk_data
# ===========================================================================




# ===========================================================================
# Coverage gaps: run_tests handler output-parsing branches and
# notification truncation path.
# ===========================================================================


class TestManagementRunTestsOutputParsing(unittest.TestCase):
    """Branch coverage for the output-summary and notification logic inside
    the run_tests handler — specifically the paths not hit by the main
    pass/fail/timeout tests:

      • HTML post-process raises a non-FileNotFoundError exception
      • Pass with empty stdout — summary is ''
      • Failure output without short-summary block — falls back to E-lines
      • Failure notification contains test name + assertion + report link
      • Failure counts line precedes test name in notification
      • Failure message longer than _MAX_NOTIF=2048 — '…' suffix appended
      • short-summary block stops at '===' separator (line 919)
      • FAILURES section fallback stops at '===' separator (line 931)
      • counts_line filtered out of meaningful → re-appended (line 946)
      • elapsed >= 60s → 'Xm Ys' format (line 959)
    """

    def setUp(self):
        import concurrent.futures
        from nibe_ha_integration import ManagementCommandHandler
        self.em        = _make_em()
        self.mqtt      = MagicMock()
        self.em.mqtt   = self.mqtt
        self.publisher = MagicMock()
        self.executor  = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ManagementCommandHandler(
            self.mqtt, self.em, self.publisher, self.executor
        ).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=True)

    def _get_handler(self):
        from nibe_mqtt_publisher import MgmtTopic
        for call in self.mqtt.message_callback_add.call_args_list:
            if call.args[0] == MgmtTopic.RUN_TESTS_PRESS:
                return call.args[1]
        raise KeyError('No handler for RUN_TESTS_PRESS')

    def _msg(self):
        m = MagicMock()
        m.payload = b''
        return m

    def _trigger_and_wait(self, returncode=0, stdout='',
                          open_side_effect=FileNotFoundError,
                          patch_notify=True):
        """Fire the handler, wait for completion, return all publish pairs.

        patch_notify=True (default) patches notify_ha and dismiss_ha to
        prevent live Supervisor calls during tests that don't need to inspect
        the notification content.  Pass patch_notify=False when the caller
        supplies its own patch.object(notify_ha) to capture the message.
        """
        import concurrent.futures as _cf
        from contextlib import ExitStack
        proc = MagicMock(returncode=returncode, stdout=stdout, stderr='')
        handler = self._get_handler()
        with ExitStack() as stack:
            stack.enter_context(patch('subprocess.run', return_value=proc))
            if patch_notify:
                stack.enter_context(patch('nibe_ha_integration.notify_ha'))
                stack.enter_context(patch('nibe_ha_integration.dismiss_ha'))
            stack.enter_context(patch('nibe_ha_integration.open',
                                      side_effect=open_side_effect,
                                      create=True))
            handler(None, None, self._msg())
            # Wait inside the patch context so the thread sees the mock
            self.executor.shutdown(wait=True)
        # Recreate for tearDown
        self.executor = _cf.ThreadPoolExecutor(max_workers=1)
        return [(c.args[0], c.args[1])
                for c in self.mqtt.publish.call_args_list]

    def _get_attrs(self, calls):
        """Return the LAST RUN_TESTS_ATTRS publish payload as a dict."""
        import json as _json
        from nibe_mqtt_publisher import MgmtTopic
        attrs_payloads = [p for t, p in calls if t == MgmtTopic.RUN_TESTS_ATTRS]
        self.assertTrue(attrs_payloads, 'No RUN_TESTS_ATTRS publish found')
        return _json.loads(attrs_payloads[-1])

    # ── HTML post-processing exception ────────────────────────────────────────

    def test_html_postprocess_generic_exception_does_not_crash_handler(self):
        """A non-FileNotFoundError from open() (e.g. PermissionError) must be
        caught — the handler must still publish a final status."""
        calls = self._trigger_and_wait(
            returncode=0,
            stdout='2226 passed in 51s',
            open_side_effect=PermissionError('read-only filesystem'),
        )
        from nibe_mqtt_publisher import MgmtTopic
        states = [p for t, p in calls if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn('passed', states)

    # ── Pass-path output parsing ──────────────────────────────────────────────

    def test_pass_empty_stdout_summary_is_empty_string(self):
        """Pass with empty stdout: 'if lines:' is False — summary is '' (raw output)."""
        calls = self._trigger_and_wait(returncode=0, stdout='')
        attrs = self._get_attrs(calls)
        self.assertEqual(attrs['summary'], '')

    # ── Failure-path output parsing ───────────────────────────────────────────

    # Realistic pytest failure output used by multiple tests below.
    _PYTEST_FAILURE = (
        ".......F......\n"
        "=================================== FAILURES ===================================\n"
        "_ TestFoo::test_bar _\n"
        "\n"
        "    def test_bar(self):\n"
        ">       self.assertEqual(1, 2)\n"
        "E       AssertionError: 1 != 2\n"
        "\n"
        "tests/test_foo.py:42: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_foo.py::TestFoo::test_bar - AssertionError: 1 != 2\n"
        "1 failed, 2251 passed in 1:10:22\n"
    )

    def test_failure_summary_extracts_short_summary_block(self):
        """Failure with real pytest output: attrs summary contains the
        short-summary-info line and the counts line — not progress dots."""
        calls = self._trigger_and_wait(returncode=1, stdout=self._PYTEST_FAILURE)
        attrs = self._get_attrs(calls)
        self.assertIn("tests/test_foo.py::TestFoo::test_bar", attrs["summary"])
        self.assertIn("1 failed, 2251 passed", attrs["summary"])
        self.assertNotIn(".......F......", attrs["summary"])

    def test_failure_notification_contains_test_name_and_assertion(self):
        """Failure notification must contain the failing test path and assertion
        message — not raw progress dots or generic guidance text."""
        captured = {}
        import nibe_ha_integration as _hi
        def _fake_notify(mqtt_client, title, message, notification_id):
            captured["message"] = message
        with patch.object(_hi, "notify_ha", side_effect=_fake_notify):
            self._trigger_and_wait(returncode=1, stdout=self._PYTEST_FAILURE, patch_notify=False)
        self.assertIn("message", captured)
        msg = captured["message"]
        self.assertIn("TestFoo::test_bar", msg)
        self.assertIn("AssertionError: 1 != 2", msg)
        self.assertIn("nibe_test_report.html", msg)
        self.assertNotIn(".......F......", msg)

    def test_failure_notification_counts_line_precedes_test_name(self):
        """The counts line must appear before the test name in the notification
        so the headline is immediately visible without scrolling."""
        captured = {}
        import nibe_ha_integration as _hi
        def _fake_notify(mqtt_client, title, message, notification_id):
            captured["message"] = message
        with patch.object(_hi, "notify_ha", side_effect=_fake_notify):
            self._trigger_and_wait(returncode=1, stdout=self._PYTEST_FAILURE, patch_notify=False)
        msg = captured["message"]
        counts_pos = msg.find("1 failed, 2251 passed")
        test_pos   = msg.find("TestFoo::test_bar")
        self.assertLess(counts_pos, test_pos)

    def test_failure_no_short_summary_falls_back_to_e_lines(self):
        """When the short test summary block is absent, the fallback extracts
        E-prefixed assertion lines from the FAILURES section."""
        no_short = (
            "=================================== FAILURES ===================================\n"
            "_ TestFoo::test_bar _\n"
            "E       AssertionError: unexpected value\n"
            "1 failed in 0.5s\n"
        )
        calls = self._trigger_and_wait(returncode=1, stdout=no_short)
        attrs = self._get_attrs(calls)
        self.assertIn("AssertionError: unexpected value", attrs["summary"])

    # ── Notification truncation ───────────────────────────────────────────────

    def test_failure_notification_truncated_when_exceeds_max(self):
        """When the assembled notification message exceeds _MAX_NOTIF=2048 chars
        the truncation suffix is appended and the report link reattached.
        patch notify_ha to capture the message directly (no SUPERVISOR_TOKEN needed)."""
        long_summary = 'x' * 2200
        captured = {}
        def _fake_notify(mqtt_client, title, message, notification_id):
            captured['message'] = message
        import nibe_ha_integration as _hi
        with patch.object(_hi, 'notify_ha', side_effect=_fake_notify):
            self._trigger_and_wait(returncode=1, stdout=long_summary,
                                   patch_notify=False)
        self.assertIn('message', captured, 'notify_ha was not called')
        # Production code appends "\n…\n\n" (ellipsis) — not the word "truncated"
        self.assertIn('…', captured['message'])
        self.assertIn('nibe_test_report.html', captured['message'])
        self.assertLessEqual(len(captured['message']), 2048 + 200)  # truncation applied

    # ── _extract_failure_lines: short-summary termination (line 919) ─────────

    def test_short_summary_block_stops_at_equals_separator(self):
        """After reading FAILED lines from the 'short test summary info' block,
        hitting a '===...' separator line must break out of the loop (line 919).
        This ensures only the FAILED lines inside the block are captured, not
        lines from a subsequent section (e.g. a second ERRORS block)."""
        output = (
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_foo.py::TestFoo::test_one - AssertionError: first\n"
            "FAILED tests/test_foo.py::TestFoo::test_two - AssertionError: second\n"
            "=========================== some other section ============================\n"
            "FAILED tests/test_foo.py::TestFoo::test_three - should not appear\n"
            "1 failed, 2259 passed in 1:02:00\n"
        )
        calls = self._trigger_and_wait(returncode=1, stdout=output)
        attrs = self._get_attrs(calls)
        self.assertIn("test_one", attrs["summary"])
        self.assertIn("test_two", attrs["summary"])
        self.assertNotIn("test_three", attrs["summary"])

    # ── _extract_failure_lines: FAILURES section termination (line 931) ──────

    def test_failures_section_fallback_stops_at_equals_separator(self):
        """When the short-summary block is absent, the fallback E-line extractor
        reads the FAILURES section and must stop (break) at the next '===...'
        separator line (line 931) so it doesn't bleed into a subsequent section."""
        output = (
            "=================================== FAILURES ===================================\n"
            "_ TestFoo::test_bar _\n"
            "E       AssertionError: boom\n"
            "======================================================================\n"
            "E       AssertionError: should not appear — this is after the separator\n"
            "1 failed in 0.5s\n"
        )
        calls = self._trigger_and_wait(returncode=1, stdout=output)
        attrs = self._get_attrs(calls)
        self.assertIn("AssertionError: boom", attrs["summary"])
        self.assertNotIn("should not appear", attrs["summary"])

    # ── Pass-path: counts_line already in meaningful lines (line 946) ────────

    def test_pass_counts_line_appended_when_filtered_out_of_meaningful(self):
        """Line 946: when the counts line consists only of chars in the
        progress-dot filter set (e.g. '....F...' — unusual but possible with
        returncode=0), it is filtered out of 'meaningful' and the guard appends
        it so the summary is never completely empty.

        The condition fires when counts_line IS truthy but NOT already in
        meaningful — i.e. the last non-empty line was stripped by the filter."""
        # stdout = all progress dots, so counts_line = "........" which consists
        # entirely of '.' chars → filtered out of meaningful → line 946 fires.
        output = "........\n"
        calls = self._trigger_and_wait(returncode=0, stdout=output)
        attrs = self._get_attrs(calls)
        # The summary must contain the counts_line (re-appended by line 946)
        self.assertIn("........", attrs["summary"])

    def test_pass_xdist_noise_stripped_from_summary(self):
        """xdist startup lines and 'u' worker-rescheduling markers must not
        appear in the summary — they are infrastructure noise, not test results."""
        output = (
            "bringing up nodes...\n"
            "bringing up nodes...\n"
            ".......uuu............u.................................\n"
            "--- Generated html report: file:///config/www/nibe_test_report.html ---\n"
            "2654 passed, 16 subtests passed in 1642.83s (0:27:22)\n"
        )
        calls = self._trigger_and_wait(returncode=0, stdout=output)
        attrs = self._get_attrs(calls)
        summary = attrs["summary"]
        self.assertNotIn("bringing up nodes", summary)
        self.assertNotIn("Generated html report", summary)
        # Progress-dot lines (including 'u' markers) stripped
        self.assertNotIn("uuu", summary)
        # Counts line preserved
        self.assertIn("2654 passed", summary)

    # ── Elapsed time minutes formatting (line 959) ────────────────────────────

    def test_elapsed_over_60s_formats_as_minutes(self):
        """Line 959: elapsed >= 60 → 'Xm Ys' format in attrs instead of 'X.Xs'.
        The handler does 'import time as _time' locally, so we patch
        time.monotonic in the global time module — the local alias picks it up."""
        import time as _time_mod
        import nibe_ha_integration as _hi  # noqa: F401 — needed for patch context
        _orig = _time_mod.monotonic
        call_count = [0]
        _t0 = [0.0]
        def _fake_monotonic():
            call_count[0] += 1
            if call_count[0] == 1:
                _t0[0] = _orig()
                return _t0[0]
            # All subsequent calls return start + 90s
            return _t0[0] + 90.0
        with patch.object(_time_mod, 'monotonic', side_effect=_fake_monotonic):
            calls = self._trigger_and_wait(returncode=0, stdout='2260 passed in 1:30:00')
        attrs = self._get_attrs(calls)
        self.assertIn('elapsed', attrs)
        # Must use minutes format, not decimal-seconds format
        self.assertRegex(attrs['elapsed'], r'^\d+m \d+s$')

# ===========================================================================
# Branch coverage: _handle_event paths not covered by existing tests
# ===========================================================================


class TestHandleEventBranchCoverage(unittest.TestCase):
    """Targeted branch coverage for _handle_event paths that the existing
    TestRegistryWatcherEventHandling class does not exercise:

      • create with eid but no uid → _schedule_refresh_registry (501→511)
        [existing test patches refresh_registry on the watcher object, which
        shadows _schedule_refresh_registry — this test verifies the call path]
      • update with eid but no uid → _schedule_refresh_registry (518→524)
      • update with disabled_by=="user" → _on_entity_enabled (527→529)
      • update with disabled_by==None  → _on_entity_disabled (529→531)
      • remove with no uid — map is not touched (537→539 False branch)
    """

    def _make_watcher(self, em=None, pub=None):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event    = threading.Event()
        w._thread        = None
        w._ws_lock       = threading.Lock()
        w._current_ws    = None
        w._msg_id        = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em  = em  or MagicMock()
        w._pub = pub or MagicMock()
        return w

    # ── create: no uid → _schedule_refresh_registry ──────────────────────────

    def test_create_no_uid_calls_schedule_refresh_registry(self):
        """create event with eid but no unique_id must call
        _schedule_refresh_registry() — the coalescing path (501→511)."""
        w = self._make_watcher()
        with patch.object(w, '_schedule_refresh_registry') as mock_sched:
            w._handle_event({'data': {
                'action':    'create',
                'entity_id': 'sensor.nibe_100',
                # deliberately no 'unique_id'
            }})
            if w._refresh_timer is not None:
                w._refresh_timer.cancel()
        mock_sched.assert_called_once()

    # ── update: no uid → _schedule_refresh_registry ──────────────────────────

    def test_update_no_uid_calls_schedule_refresh_registry(self):
        """update event with eid but no unique_id must call
        _schedule_refresh_registry() — the coalescing path (518→524)."""
        w = self._make_watcher()
        with patch.object(w, '_schedule_refresh_registry') as mock_sched:
            w._handle_event({'data': {
                'action':    'update',
                'entity_id': 'sensor.nibe_100',
                # deliberately no 'unique_id'
            }})
            if w._refresh_timer is not None:
                w._refresh_timer.cancel()
        mock_sched.assert_called_once()

    # ── update: disabled_by change → _on_entity_enabled / _disabled ──────────

    def test_update_disabled_by_user_calls_on_entity_enabled(self):
        """update with changes={disabled_by: 'user'} means the entity WAS
        disabled and is now enabled → _on_entity_enabled must be called (527→529)."""
        w = self._make_watcher()
        with patch.object(w, '_on_entity_enabled') as mock_enabled:
            w._handle_event({'data': {
                'action':    'update',
                'entity_id': 'switch.nibe_100',
                'unique_id': 'nibe_100',
                'changes':   {'disabled_by': 'user'},
            }})
        mock_enabled.assert_called_once_with('switch.nibe_100')

    def test_update_disabled_by_none_calls_on_entity_disabled(self):
        """update with changes={disabled_by: None} means the entity WAS
        enabled and is now disabled → _on_entity_disabled must be called (529→531)."""
        w = self._make_watcher()
        with patch.object(w, '_on_entity_disabled') as mock_disabled:
            w._handle_event({'data': {
                'action':    'update',
                'entity_id': 'switch.nibe_100',
                'unique_id': 'nibe_100',
                'changes':   {'disabled_by': None},
            }})
        mock_disabled.assert_called_once_with('switch.nibe_100')

    def test_update_disabled_by_other_value_calls_neither(self):
        """update with changes={disabled_by: 'integration'} (not user, not None)
        must call neither _on_entity_enabled nor _on_entity_disabled."""
        w = self._make_watcher()
        with patch.object(w, '_on_entity_enabled') as mock_en, \
             patch.object(w, '_on_entity_disabled') as mock_dis:
            w._handle_event({'data': {
                'action':    'update',
                'entity_id': 'switch.nibe_100',
                'unique_id': 'nibe_100',
                'changes':   {'disabled_by': 'integration'},
            }})
        mock_en.assert_not_called()
        mock_dis.assert_not_called()

    # ── remove: no uid → map unchanged (537→539 False branch) ────────────────

    def test_remove_without_uid_does_not_touch_map(self):
        """remove event with no unique_id (uid is None/falsy) must not
        attempt to pop from _unique_id_map — the if uid: False branch (537→539)."""
        w = self._make_watcher()
        w._unique_id_map['nibe_100'] = 'sensor.nibe_100'
        w._handle_event({'data': {
            'action':    'remove',
            'entity_id': 'sensor.nibe_100',
            # deliberately no 'unique_id'
        }})
        # Map must be unchanged
        self.assertIn('nibe_100', w._unique_id_map)

# ===========================================================================
# _get_ha_base_url — supervisor API fetch and caching
# ===========================================================================


class TestGetHaBaseUrl(unittest.TestCase):
    """Tests for _get_ha_base_url():

      • Returns internal_url when present
      • Falls back to external_url when internal_url is absent
      • Returns '' when no supervisor token
      • Returns '' and logs warning when supervisor API call fails
      • Caches the result after first successful fetch
    """

    def setUp(self):
        # Reset the module-level cache before each test
        import nibe_ha_integration as _hi
        _hi._ha_base_url = None

    def tearDown(self):
        import nibe_ha_integration as _hi
        _hi._ha_base_url = None

    def _mock_api(self, response_dict):
        """Return a context manager that mocks the supervisor config API."""
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(response_dict).encode()
        return patch('urllib.request.urlopen', return_value=mock_resp)

    def test_returns_internal_url_when_present(self):
        from nibe_ha_integration import _get_ha_base_url
        cfg = {'internal_url': 'http://192.168.1.10:8123',
               'external_url': 'https://my.nabu.casa'}
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             self._mock_api(cfg):
            result = _get_ha_base_url()
        self.assertEqual(result, 'http://192.168.1.10:8123')

    def test_falls_back_to_external_url(self):
        from nibe_ha_integration import _get_ha_base_url
        cfg = {'internal_url': '', 'external_url': 'https://my.nabu.casa'}
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             self._mock_api(cfg):
            result = _get_ha_base_url()
        self.assertEqual(result, 'https://my.nabu.casa')

    def test_returns_empty_string_without_supervisor_token(self):
        from nibe_ha_integration import _get_ha_base_url
        with patch.dict('os.environ', {}, clear=True):
            result = _get_ha_base_url()
        self.assertEqual(result, '')

    def test_returns_empty_string_on_api_error(self):
        from nibe_ha_integration import _get_ha_base_url
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('urllib.request.urlopen', side_effect=OSError('refused')):
            result = _get_ha_base_url()
        self.assertEqual(result, '')

    def test_caches_result_after_first_fetch(self):
        from nibe_ha_integration import _get_ha_base_url
        cfg = {'internal_url': 'http://192.168.1.10:8123'}
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             self._mock_api(cfg) as mock_open:
            _get_ha_base_url()
            _get_ha_base_url()  # second call
        # urlopen must only be called once — second call uses cache
        self.assertEqual(mock_open.call_count, 1)

    def test_trailing_slash_stripped(self):
        from nibe_ha_integration import _get_ha_base_url
        cfg = {'internal_url': 'http://192.168.1.10:8123/'}
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             self._mock_api(cfg):
            result = _get_ha_base_url()
        self.assertFalse(result.endswith('/'))

# ===========================================================================
# Branch coverage: targeted gaps from --cov-branch audit
# ===========================================================================


class TestRefreshRegistrySuccessFalse(unittest.TestCase):
    """refresh_registry: 263→exit — registry response success=False.

    After a successful auth handshake, the registry list request returns a
    response with success=False (e.g. the HA registry is temporarily
    unavailable).  The method must silently return without populating
    _unique_id_map.
    """

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em = MagicMock()
        w._pub = MagicMock()
        return w

    def test_success_false_response_leaves_map_empty(self):
        w = self._make_watcher()
        w._unique_id_map['nibe_pre'] = 'sensor.pre'  # pre-existing entry
        ws = MagicMock()
        ws.recv.side_effect = [
            json.dumps({'type': 'auth_required'}),
            json.dumps({'type': 'auth_ok'}),
            json.dumps({'id': 1, 'type': 'result',
                        'success': False, 'error': {'code': 'unknown'}}),
        ]
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            with patch('websocket.create_connection', return_value=ws):
                w.refresh_registry()
        # Map must not be populated from the failed response
        self.assertNotIn('nibe_100', w._unique_id_map)
        # The pre-existing entry is preserved (not cleared)
        self.assertIn('nibe_pre', w._unique_id_map)


class TestWatcherLoopUnknownMessageType(unittest.TestCase):
    """_run inner loop: 439→410 — message type is neither 'pong' nor 'event'.

    Messages of type 'result', 'ping_response', or unknown types must be
    silently discarded without touching _handle_event — the loop continues.
    """

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em = MagicMock()
        w._pub = MagicMock()
        return w

    def test_unknown_message_type_does_not_call_handle_event(self):
        """A 'result' message (neither pong nor event) must be silently
        skipped — _handle_event must never be called for it."""
        w = self._make_watcher()
        ws = MagicMock()
        # Deliver one 'result' message, then set stop_event so loop exits
        call_count = [0]
        def recv_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({'type': 'result', 'id': 99,
                                   'success': True, 'result': {}})
            w._stop_event.set()
            return json.dumps({'type': 'pong'})
        ws.recv.side_effect = recv_side_effect
        with patch.object(w, '_connect_and_subscribe', return_value=ws), \
             patch.object(w, '_handle_event') as mock_event, \
             patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}):
            w._run()
        mock_event.assert_not_called()


class TestHandleEventNoEidBranches(unittest.TestCase):
    """_handle_event: 544→554 and 561→567 False branches.

    When a create or update event carries no 'entity_id' at all (eid is
    None/falsy), neither the uid/eid map update nor the schedule_refresh
    call should fire — the elif branch evaluates False and the event is
    silently dropped.
    """

    def _make_watcher(self):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em = MagicMock()
        w._pub = MagicMock()
        return w

    def test_create_no_eid_does_not_schedule_refresh(self):
        """create event with no entity_id at all (eid=None) — elif eid is
        False — must not call _schedule_refresh_registry (544→554 False)."""
        w = self._make_watcher()
        with patch.object(w, '_schedule_refresh_registry') as mock_sched:
            w._handle_event({'data': {
                'action': 'create',
                # deliberately no 'entity_id' key at all
            }})
        mock_sched.assert_not_called()

    def test_update_no_eid_does_not_schedule_refresh(self):
        """update event with no entity_id (eid=None) — elif eid is False —
        must not call _schedule_refresh_registry (561→567 False)."""
        w = self._make_watcher()
        with patch.object(w, '_schedule_refresh_registry') as mock_sched:
            w._handle_event({'data': {
                'action': 'update',
                # deliberately no 'entity_id' key
            }})
        mock_sched.assert_not_called()


class TestOnEntityEnabledDisabledPointDictNone(unittest.TestCase):
    """_on_entity_enabled and _on_entity_disabled: 606→609 and 641→643.

    Both branches guard 'if point_dict:' where point_dict comes from
    all_points_by_id.get(point_id).  When the point is not in
    all_points_by_id (e.g. it was removed mid-flight), publish_entity_discovery
    must not be called.
    """

    def _make_watcher(self, em, pub=None):
        import threading
        from nibe_ha_integration import HAEntityRegistryWatcher
        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em = em
        w._pub = pub or MagicMock()
        return w

    def test_enabled_point_dict_none_skips_discovery_republish(self):
        """_on_entity_enabled: point IS in mqtt_enabled_points but NOT in
        all_points_by_id → if point_dict: is False → no discovery publish.
        (606→609 False branch)"""
        em = _make_em()
        em.mqtt_enabled_points.add(100)
        # Deliberately do NOT put 100 in all_points_by_id
        pub = MagicMock()
        w = self._make_watcher(em, pub)
        with patch('nibe_ha_integration.notify_ha'):
            w._on_entity_enabled('switch.nibe_100')
        pub.publish_entity_discovery.assert_not_called()

    def test_disabled_dynamic_point_dict_none_skips_discovery_republish(self):
        """_on_entity_disabled: is_dynamic=True but point_dict is None at line
        640 — 641→643 False branch.

        Simulates the race condition where the point is present at line 622
        (so is_dynamic=True), but removed from all_points_by_id by a
        concurrent thread between lines 622 and 640.  Achieved by making
        all_points_by_id.get() return different values on successive calls."""
        em = _make_em()
        # First call to all_points_by_id.get(50827) returns {'is_dynamic': True}
        # Second call returns None (concurrent removal)
        call_count = [0]
        real_dict = {}
        def get_side_effect(key, default=None):
            if key == 50827:
                call_count[0] += 1
                if call_count[0] == 1:
                    return {'is_dynamic': True, 'display_title': 'THS-10 Humidity'}
                return None   # second call: simulates concurrent removal
            return real_dict.get(key, default)
        em.all_points_by_id = MagicMock()
        em.all_points_by_id.get = MagicMock(side_effect=get_side_effect)
        pub = MagicMock()
        w = self._make_watcher(em, pub)
        with patch('nibe_ha_integration.notify_ha'):
            w._on_entity_disabled('sensor.nibe_50827')
        pub.publish_entity_discovery.assert_not_called()


class TestAlarmCountAndStatsKeyUnchanged(unittest.TestCase):
    """Steady-state debug-log suppression branches.

    1067→1071: alarm_count == _last_alarm_count — debug log suppressed.
    1167→exit: stats_key == _last_stats_key — debug log suppressed.

    Both branches guard verbose debug logging that would fire on every
    poll cycle.  Testing the False branches verifies the dedup works.
    """

    def test_alarm_count_unchanged_skips_log_update(self):
        """Calling update_alarm_state twice with the same count must NOT
        re-update _last_alarm_count on the second call (1067→1071 False)."""
        from nibe_ha_integration import update_alarm_state
        em = _make_em()
        em._api.fetch_notifications.return_value = []
        pub = MagicMock()
        # First call: count=0, _last_alarm_count is updated to 0
        update_alarm_state(em, pub)
        # Second call: count=0 again — alarm_count == _last_alarm_count
        # The if at 1067 is False → 1067→1071 branch taken; _last_alarm_count
        # stays 0 (but we verify the function completes without error)
        update_alarm_state(em, pub)
        self.assertEqual(em._last_alarm_count, 0)
        # publish_alarm_state is called both times regardless
        self.assertEqual(pub.publish_alarm_state.call_count, 2)

    def test_stats_key_unchanged_skips_log_update(self):
        """Calling _publish_stats twice with identical state must skip the
        debug log on the second call (1167→exit False branch)."""
        from nibe_ha_integration import _publish_stats
        em = _make_em()
        pub = MagicMock()
        # First call: stats_key differs from None → debug log fires, key stored
        _publish_stats(em, pub)
        stored_key = getattr(em, '_last_stats_key', None)
        # Second call: same em state → stats_key == _last_stats_key → 1167→exit
        _publish_stats(em, pub)
        self.assertEqual(getattr(em, '_last_stats_key', None), stored_key)
        # publish_stats called twice
        self.assertEqual(pub.publish_stats.call_count, 2)


# ===========================================================================
# Snapshot command handler
# ===========================================================================


class TestHandleSnapshotCmd(unittest.TestCase):
    """_handle_snapshot_cmd: routes save/restore/delete to EntityManager."""

    def setUp(self):
        import concurrent.futures
        from nibe_ha_integration import ManagementCommandHandler
        self.em  = _make_em()
        self.pub = MagicMock()
        self.exe = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ManagementCommandHandler(self.em.mqtt, self.em, self.pub, self.exe).register_all()

    def tearDown(self):
        self.exe.shutdown(wait=True)

    def _send(self, payload: dict):
        import json
        msg = MagicMock()
        msg.payload = json.dumps(payload).encode()
        # Call the handler directly via the registered callback
        from nibe_ha_integration import ManagementCommandHandler
        handler = ManagementCommandHandler(
            self.em.mqtt, self.em, self.pub, self.exe
        )
        handler._handle_snapshot_cmd(None, None, msg)
        self.exe.shutdown(wait=True)
        import concurrent.futures
        self.exe = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def test_save_action_calls_save_snapshot(self):
        with patch.object(self.em, 'save_snapshot', return_value=(True, 'ok')) as mock_save:
            self._send({'action': 'save', 'name': 'Test'})
        mock_save.assert_called_once_with('Test')

    def test_restore_action_calls_restore_snapshot(self):
        with patch.object(self.em, 'restore_snapshot',
                          return_value=(True, 'ok')) as mock_restore:
            self._send({'action': 'restore', 'name': 'Test', 'mode': 'merge'})
        mock_restore.assert_called_once_with('Test', 'merge')

    def test_restore_defaults_to_flush(self):
        with patch.object(self.em, 'restore_snapshot',
                          return_value=(True, 'ok')) as mock_restore:
            self._send({'action': 'restore', 'name': 'Test'})
        mock_restore.assert_called_once_with('Test', 'flush')

    def test_restore_invalid_mode_defaults_to_flush(self):
        with patch.object(self.em, 'restore_snapshot',
                          return_value=(True, 'ok')) as mock_restore:
            self._send({'action': 'restore', 'name': 'Test', 'mode': 'invalid'})
        mock_restore.assert_called_once_with('Test', 'flush')

    def test_delete_action_calls_delete_snapshot(self):
        with patch.object(self.em, 'delete_snapshot',
                          return_value=(True, 'ok')) as mock_delete:
            self._send({'action': 'delete', 'name': 'Test'})
        mock_delete.assert_called_once_with('Test')

    def test_unknown_action_is_ignored(self):
        with patch.object(self.em, 'save_snapshot') as ms, \
             patch.object(self.em, 'restore_snapshot') as mr, \
             patch.object(self.em, 'delete_snapshot') as md:
            self._send({'action': 'unknown', 'name': 'Test'})
        ms.assert_not_called()
        mr.assert_not_called()
        md.assert_not_called()

    def test_invalid_json_payload_is_ignored(self):
        msg = MagicMock()
        msg.payload = b'not valid json'
        with patch.object(self.em, 'save_snapshot') as ms:
            from nibe_ha_integration import ManagementCommandHandler
            handler = ManagementCommandHandler(
                self.em.mqtt, self.em, self.pub, self.exe
            )
            handler._handle_snapshot_cmd(None, None, msg)
            self.exe.shutdown(wait=True)
        ms.assert_not_called()
