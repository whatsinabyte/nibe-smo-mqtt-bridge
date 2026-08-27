"""
test_ha_integration.py
======================
Nibe_ha_integration tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import re
import signal
import subprocess
import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

from conftest import (
    _MENU_YAML,
    _make_em,
    _nibe_point_id,
)
from hypothesis import example, given
from hypothesis import strategies as st


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
        self.assertLess(
            self.cls._PING_TIMEOUT_S,
            self.cls._PING_INTERVAL_S,
            f"_PING_TIMEOUT_S={self.cls._PING_TIMEOUT_S} must be < "
            f"_PING_INTERVAL_S={self.cls._PING_INTERVAL_S}",
        )

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
            mqtt_client=mqtt,
            device_info={},
            device_id="test",
            device_name="Test",
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
        retain = call.kwargs.get("retain", call.args[2] if len(call.args) > 2 else False)
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
        pub, _mqtt = self._pub()
        pub._pub_state(topic, payload)  # must not raise

    @given(st.text(min_size=1, max_size=100), st.text(max_size=200))
    def test_never_raises_on_failure_rc(self, topic, payload):
        """Non-zero rc must log a warning but never raise."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher

        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=4)
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt,
            device_info={},
            device_id="test",
            device_name="Test",
        )
        pub._pub_state(topic, payload)  # must not raise


# ---------------------------------------------------------------------------
# HAEntityRegistryWatcher._sub properties (nibe_ha_integration.py)
# ---------------------------------------------------------------------------


class TestSubProperties(unittest.TestCase):
    """Hypothesis properties for ManagementCommandHandler._sub."""

    def _handler(self):
        from nibe_ha_integration import ManagementCommandHandler

        em = MagicMock()
        mqtt = MagicMock()
        pub = MagicMock()
        rw = MagicMock()
        h = ManagementCommandHandler(em, mqtt, pub, rw)
        h._mqtt = mqtt
        h._em = em
        return h, mqtt, em

    @given(st.text(min_size=1, max_size=100))
    def test_calls_mqtt_subscribe(self, topic):
        h, mqtt, _em = self._handler()
        h._sub(topic, MagicMock())
        mqtt.subscribe.assert_called_once_with(topic, qos=1)

    @given(st.text(min_size=1, max_size=100))
    def test_calls_message_callback_add(self, topic):
        h, mqtt, _em = self._handler()
        handler = MagicMock()
        h._sub(topic, handler)
        mqtt.message_callback_add.assert_called_once_with(topic, handler)

    @given(st.text(min_size=1, max_size=100))
    def test_calls_register_mgmt_subscription(self, topic):
        h, _mqtt, em = self._handler()
        handler = MagicMock()
        h._sub(topic, handler)
        em.register_mgmt_subscription.assert_called_once_with(topic, handler, 1)

    @given(st.text(min_size=1, max_size=100), st.integers(min_value=0, max_value=2))
    def test_qos_passed_correctly(self, topic, qos):
        h, mqtt, em = self._handler()
        h._sub(topic, MagicMock(), qos=qos)
        mqtt.subscribe.assert_called_once_with(topic, qos=qos)
        em.register_mgmt_subscription.assert_called_once_with(topic, unittest.mock.ANY, qos)

    @given(st.text(min_size=1, max_size=100))
    def test_never_raises(self, topic):
        h, _mqtt, _em = self._handler()
        h._sub(topic, MagicMock())  # must not raise


class TestSubmitLogsUnhandledException(unittest.TestCase):
    """_submit()'s wrapped function must log the handler's exception —
    otherwise a bug in a management command handler fails invisibly since
    nothing awaits the executor Future."""

    def test_handler_exception_is_logged_verbatim(self):
        import concurrent.futures

        from nibe_ha_integration import ManagementCommandHandler

        em = _make_em()
        pub = MagicMock()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as exe:
            handler = ManagementCommandHandler(em.mqtt, em, pub, exe)

            def _boom():
                raise RuntimeError("handler bug")

            with self.assertLogs("nibe.commands", level="ERROR") as cm:
                handler._submit(_boom)
                exe.shutdown(wait=True)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Unhandled exception in management command handler")
                for msg in cm.output
            )
        )


class TestDefaultTestExecutorConstruction(unittest.TestCase):
    """When no test_executor is passed in, ManagementCommandHandler must
    build its own dedicated single-worker ThreadPoolExecutor with a
    recognisable thread name — not share mgmt_executor's pool (a 25-30
    minute test run could then starve or be starved by other management
    commands) and not spawn an unbounded number of worker threads."""

    def test_default_test_executor_has_one_worker_and_named_threads(self):
        import threading

        from nibe_ha_integration import ManagementCommandHandler

        em = _make_em()
        pub = MagicMock()
        exe = MagicMock()
        handler = ManagementCommandHandler(em.mqtt, em, pub, exe, test_executor=None)
        try:
            seen_names = []
            done = threading.Event()

            def _record_name():
                seen_names.append(threading.current_thread().name)
                done.set()

            handler._test_executor.submit(_record_name)
            done.wait(timeout=5)
            self.assertTrue(seen_names, "worker never ran")
            # startswith, not assertIn: ThreadPoolExecutor names threads
            # "<prefix>_<n>", so a real prefix always starts the name —
            # assertIn would also pass for a mutated 'XXnibe_test_runnerXX'
            # prefix, since the real text is still a substring of that.
            self.assertTrue(seen_names[0].startswith("nibe_test_runner"))
            # A second concurrent submission must NOT get its own thread —
            # max_workers=1 means it queues behind the first.
            self.assertEqual(handler._test_executor._max_workers, 1)
        finally:
            handler._test_executor.shutdown(wait=True)


class TestRegisterAllRecordsEveryTopic(unittest.TestCase):
    """register_all() must route every management topic — including
    BrowserTopic.SNAPSHOTS_CMD — through _sub(), not a raw
    mqtt.subscribe()/message_callback_add() call, so every one of them gets
    recorded via entity_manager.register_mgmt_subscription() and can be
    replayed by resubscribe_all() after a Mosquitto restart.

    Regression: SNAPSHOTS_CMD was previously wired up with a direct
    self._em.mqtt.subscribe()/message_callback_add() call, bypassing _sub()
    entirely. It worked at startup but silently stopped receiving snapshot
    save/restore/delete commands after any broker restart, since
    resubscribe_all() had no record of it to replay — with no error and no
    log line, since the broker legitimately has no subscriber for a topic
    it was never told to keep.
    """

    def test_snapshots_cmd_is_recorded_for_resubscription(self):
        from nibe_ha_integration import ManagementCommandHandler
        from nibe_mqtt_publisher import BrowserTopic

        em = _make_em()
        pub = MagicMock()
        exe = MagicMock()
        with patch.object(em, "register_mgmt_subscription") as mock_register:
            handler = ManagementCommandHandler(em.mqtt, em, pub, exe)
            handler.register_all()

        registered_topics = [call.args[0] for call in mock_register.call_args_list]
        self.assertIn(BrowserTopic.SNAPSHOTS_CMD, registered_topics)
        snapshots_call = next(
            call
            for call in mock_register.call_args_list
            if call.args[0] == BrowserTopic.SNAPSHOTS_CMD
        )
        self.assertEqual(snapshots_call.args[1], handler._handle_snapshot_cmd)


# ---------------------------------------------------------------------------
# DynamicPointMap expected_active_dynamic_points properties
# ---------------------------------------------------------------------------


class TestPublishApiReachabilityProperties(unittest.TestCase):
    """Hypothesis properties for publish_api_reachability."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher

        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt,
            device_info={},
            device_id="test",
            device_name="Test",
        )
        return pub, mqtt

    def _get_api_state(self, mqtt):
        from nibe_mqtt_publisher import MgmtTopic

        calls = [c for c in mqtt.publish.call_args_list if c.args[0] == MgmtTopic.API_OK_STATE]
        self.assertTrue(calls, "No API_OK_STATE publish found")
        return calls[-1].args[1]

    def _get_fetch_dur(self, mqtt):
        from nibe_mqtt_publisher import MgmtTopic

        calls = [c for c in mqtt.publish.call_args_list if c.args[0] == MgmtTopic.FETCH_DUR_STATE]
        self.assertTrue(calls, "No FETCH_DUR_STATE publish found")
        return calls[-1].args[1]

    @given(st.integers(min_value=0, max_value=20), st.integers(min_value=1, max_value=20))
    def test_api_state_is_always_on_or_off(self, failures, threshold):
        pub, mqtt = self._pub()
        pub.publish_api_reachability(failures, threshold, 0.0, 0.1)
        state = self._get_api_state(mqtt)
        self.assertIn(state, ("ON", "OFF"))

    @given(st.integers(min_value=0, max_value=20), st.integers(min_value=1, max_value=20))
    def test_api_state_on_when_failures_below_threshold(self, failures, threshold):
        pub, mqtt = self._pub()
        pub.publish_api_reachability(failures, threshold, 0.0, 0.1)
        state = self._get_api_state(mqtt)
        if failures < threshold:
            self.assertEqual(state, "ON")
        else:
            self.assertEqual(state, "OFF")

    @given(st.floats(min_value=0.0, max_value=9999.9, allow_nan=False, allow_infinity=False))
    def test_fetch_duration_always_2dp(self, duration):
        """Fetch duration must always be formatted to exactly 2 decimal places."""
        pub, mqtt = self._pub()
        pub.publish_api_reachability(0, 3, 0.0, duration)
        state = self._get_fetch_dur(mqtt)
        self.assertRegex(state, r"^\d+\.\d{2}$")

    @given(st.floats(min_value=0.0, max_value=9999.9, allow_nan=False, allow_infinity=False))
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
        diffs = [ids[i + 1] - ids[i] for i in range(len(ids) - 1)]
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

    def test_first_id_is_exactly_one(self):
        """_msg_id starts at 0 so the first _next_id() call returns 1 —
        pins the exact starting value, not just monotonicity/uniqueness."""
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = HAEntityRegistryWatcher(MagicMock(), MagicMock())
        self.assertEqual(w._next_id(), 1)


class TestRegistryWatcherInitConstructsRealPrimitives(unittest.TestCase):
    """__init__ must construct real threading primitives — a None/empty
    default instead of a real Lock/Event would raise the first time
    another method actually uses it (with self._ws_lock:, self._stop_event
    .set()/.is_set()), while self._thread/self._current_ws being None
    (not just falsy) is relied on by `is None` checks elsewhere."""

    def test_ws_lock_is_a_real_lock_usable_as_context_manager(self):
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = HAEntityRegistryWatcher(MagicMock(), MagicMock())
        with w._ws_lock:
            pass  # must not raise

    def test_stop_event_is_a_real_event(self):
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = HAEntityRegistryWatcher(MagicMock(), MagicMock())
        self.assertFalse(w._stop_event.is_set())
        w._stop_event.set()
        self.assertTrue(w._stop_event.is_set())

    def test_thread_and_current_ws_start_as_none(self):
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = HAEntityRegistryWatcher(MagicMock(), MagicMock())
        self.assertIsNone(w._thread)
        self.assertIsNone(w._current_ws)


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
        result = self.fn("/nonexistent/menu_structure.yaml")
        self.assertEqual(result, frozenset())

    def test_missing_file_never_raises(self):
        self.fn("/nonexistent/path.yaml")  # must not raise

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
        from nibe_lovelace import _collect_menu_points, build_menu_points

        with open(_MENU_YAML, encoding="utf-8") as f:
            data = _yaml.safe_load(f)
        collected = _collect_menu_points(data.get("menus", []))
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
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(": invalid: yaml: {{{")
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

        with patch.dict("os.environ", {}, clear=True):
            notify_ha(MagicMock(), title, message, notif_id)

    @given(st.text(max_size=50))
    def test_dismiss_ha_never_raises_without_token(self, notif_id):
        """Without SUPERVISOR_TOKEN dismiss_ha must never raise for any input."""
        from nibe_ha_integration import dismiss_ha

        with patch.dict("os.environ", {}, clear=True):
            dismiss_ha(MagicMock(), notif_id)

    @given(st.text(max_size=80), st.text(max_size=500), st.text(max_size=50))
    def test_notify_ha_without_token_never_calls_urlopen(self, title, message, notif_id):
        """Without token, no HTTP call should be made."""
        from nibe_ha_integration import notify_ha

        with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen") as mock_open:
            notify_ha(MagicMock(), title, message, notif_id)
        mock_open.assert_not_called()

    @given(st.text(max_size=50))
    def test_dismiss_ha_without_token_never_calls_urlopen(self, notif_id):
        """Without token, no HTTP call should be made."""
        from nibe_ha_integration import dismiss_ha

        with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen") as mock_open:
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

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "test_token"}),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            notify_ha(MagicMock(), title, message, notif_id)

        if captured:
            self.assertEqual(captured[0]["notification_id"], notif_id)
            self.assertEqual(captured[0]["title"], title)
            self.assertEqual(captured[0]["message"], message)

    def test_notify_ha_request_url_headers_method(self):
        """The Request built by notify_ha must target the real
        persistent_notification/create endpoint with a Bearer auth header
        built from the real token, JSON content-type, and method POST."""
        from nibe_ha_integration import notify_ha

        captured_req = []

        def fake_urlopen(req, **_kw):
            captured_req.append(req)
            return MagicMock()

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "my-tok"}),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            notify_ha(MagicMock(), "Title", "Message", "notif_1")
        req = captured_req[0]
        self.assertEqual(
            req.full_url,
            "http://supervisor/core/api/services/persistent_notification/create",
        )
        self.assertEqual(req.get_header("Authorization"), "Bearer my-tok")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_method(), "POST")

    def test_notify_ha_without_token_logs_warning_verbatim(self):
        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertLogs("nibe.mqtt", level="WARNING") as cm,
        ):
            notify_ha(MagicMock(), "My Title", "My Message", "notif_1")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "HA notification (no supervisor token): [notif_1] My Title"
                )
                for msg in cm.output
            )
        )

    def test_notify_ha_success_logs_warning_verbatim(self):
        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", return_value=MagicMock()),
            self.assertLogs("nibe.mqtt", level="WARNING") as cm,
        ):
            notify_ha(MagicMock(), "My Title", "My Message", "notif_1")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("HA notification sent: [notif_1] My Title")
                for msg in cm.output
            )
        )

    def test_notify_ha_failure_logs_error_verbatim(self):
        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
            self.assertLogs("nibe.mqtt", level="ERROR") as cm,
        ):
            notify_ha(MagicMock(), "My Title", "My Message", "notif_1")  # must not raise
        self.assertTrue(
            any(
                msg.splitlines()[0] == "ERROR:nibe.mqtt:Failed to send HA notification: refused"
                for msg in cm.output
            )
        )

    def test_dismiss_ha_request_url_headers_payload_method(self):
        """dismiss_ha must target the real persistent_notification/dismiss
        endpoint, with the real notification_id in the JSON payload, a
        Bearer auth header from the real token, and method POST — currently
        completely unverified beyond 'no call when token absent'."""
        import json as _json

        from nibe_ha_integration import dismiss_ha

        captured_req = []

        def fake_urlopen(req, **_kw):
            captured_req.append(req)
            return MagicMock()

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "my-tok"}),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            dismiss_ha(MagicMock(), "notif_to_dismiss")
        req = captured_req[0]
        self.assertEqual(
            req.full_url,
            "http://supervisor/core/api/services/persistent_notification/dismiss",
        )
        self.assertEqual(req.get_header("Authorization"), "Bearer my-tok")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(_json.loads(req.data), {"notification_id": "notif_to_dismiss"})

    def test_dismiss_ha_without_token_logs_info_verbatim(self):
        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertLogs("nibe.mqtt", level="INFO") as cm,
        ):
            dismiss_ha(MagicMock(), "notif_1")
        self.assertTrue(
            any(
                msg.splitlines()[0]
                == "INFO:nibe.mqtt:HA notification dismiss (no supervisor token): [notif_1]"
                for msg in cm.output
            )
        )

    def test_dismiss_ha_success_logs_debug_verbatim(self):
        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", return_value=MagicMock()),
            self.assertLogs("nibe.mqtt", level="DEBUG") as cm,
        ):
            dismiss_ha(MagicMock(), "notif_1")
        self.assertTrue(
            any(
                msg.splitlines()[0] == "DEBUG:nibe.mqtt:HA notification dismissed: [notif_1]"
                for msg in cm.output
            )
        )

    def test_dismiss_ha_failure_logs_error_verbatim(self):
        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
            self.assertLogs("nibe.mqtt", level="ERROR") as cm,
        ):
            dismiss_ha(MagicMock(), "notif_1")  # must not raise
        self.assertTrue(
            any(
                msg.splitlines()[0] == "ERROR:nibe.mqtt:Failed to dismiss HA notification: refused"
                for msg in cm.output
            )
        )


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
        w = self._make_watcher({f"nibe_{pid}": entity_id})
        self.assertEqual(w.entity_id_for(pid), entity_id)

    @given(_nibe_point_id, st.text(min_size=1, max_size=50))
    def test_different_pid_returns_none(self, pid, entity_id):
        """Looking up a different pid than registered returns None."""
        other_pid = pid + 1
        w = self._make_watcher({f"nibe_{pid}": entity_id})
        self.assertIsNone(w.entity_id_for(other_pid))

    @given(
        st.dictionaries(
            _nibe_point_id,
            st.text(min_size=1, max_size=50),
            max_size=20,
        )
    )
    def test_result_consistent_with_unique_id_map(self, registry):
        """entity_id_for result always consistent with _unique_id_map lookup."""
        w = self._make_watcher({f"nibe_{pid}": eid for pid, eid in registry.items()})
        for pid in registry:
            self.assertEqual(
                w.entity_id_for(pid),
                w._unique_id_map.get(f"nibe_{pid}"),
            )

    @given(_nibe_point_id, st.text(min_size=1, max_size=50))
    def test_entity_id_for_uses_nibe_prefix_key(self, pid, entity_id):
        """The registry key must be nibe_{pid} — not just str(pid)."""
        w = self._make_watcher()
        # Register WITHOUT nibe_ prefix — should NOT be found
        w._unique_id_map[str(pid)] = entity_id
        self.assertIsNone(w.entity_id_for(pid))

    @given(
        st.dictionaries(
            _nibe_point_id,
            st.text(min_size=1, max_size=50),
            max_size=20,
        )
    )
    def test_all_registered_pids_are_found(self, registry):
        """Every pid that was registered must be findable."""
        w = self._make_watcher({f"nibe_{pid}": eid for pid, eid in registry.items()})
        for pid, eid in registry.items():
            self.assertEqual(w.entity_id_for(pid), eid)


class TestManagementHandlers(unittest.TestCase):
    """Tests for the MQTT management command handlers in nibe_ha_integration."""

    def setUp(self):
        import concurrent.futures

        from nibe_ha_integration import ManagementCommandHandler

        self.em = _make_em()
        self.mqtt = MagicMock()
        self.publisher = MagicMock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.test_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ManagementCommandHandler(
            self.mqtt, self.em, self.publisher, self.executor, self.test_executor
        ).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=True)
        self.test_executor.shutdown(wait=True)

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
        self.test_executor.shutdown(wait=True)
        # Recreate executors so tearDown and subsequent _run calls work cleanly
        import concurrent.futures

        from nibe_ha_integration import ManagementCommandHandler

        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.test_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Re-register handlers against the new executors
        ManagementCommandHandler(
            self.mqtt, self.em, self.publisher, self.executor, self.test_executor
        ).register_all()

    # ── aid mode handler ──────────────────────────────────────────────────────

    def test_aid_mode_on_payloads(self):
        """ON, 1, on, true, True all map to 'on'."""
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "ON")
        self.em._api.write_device_mode.assert_called_with("aidmode", "on")

    def test_aid_mode_lowercase_on_payload(self):
        """Lowercase 'on' specifically — the docstring above claims this
        is covered but only 'ON' was ever actually exercised."""
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "on")
        self.em._api.write_device_mode.assert_called_with("aidmode", "on")

    def test_aid_mode_on_payload_numeric(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "1")
        self.em._api.write_device_mode.assert_called_with("aidmode", "on")

    def test_aid_mode_off_payload(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "OFF")
        self.em._api.write_device_mode.assert_called_with("aidmode", "off")

    def test_aid_mode_lowercase_true_payload(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "true")
        self.em._api.write_device_mode.assert_called_with("aidmode", "on")

    def test_aid_mode_titlecase_true_payload(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "True")
        self.em._api.write_device_mode.assert_called_with("aidmode", "on")

    def test_aid_mode_uppercase_true_payload_maps_to_off(self):
        """'TRUE' (all caps) is NOT one of the five recognised on-payloads
        — pins the exact accepted set rather than any case of 'true'."""
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "TRUE")
        self.em._api.write_device_mode.assert_called_with("aidmode", "off")

    def test_aid_mode_publishes_state_on_success(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "ON")
        from nibe_mqtt_publisher import MgmtTopic

        topics = [c.args[0] for c in self.mqtt.publish.call_args_list]
        self.assertIn(MgmtTopic.AID_STATE, topics)

    def test_aid_mode_publishes_exact_state_value_retained(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "ON")
        from nibe_mqtt_publisher import MgmtTopic

        call = next(c for c in self.mqtt.publish.call_args_list if c.args[0] == MgmtTopic.AID_STATE)
        self.assertEqual(call.args[1], "ON")
        self.assertTrue(call.kwargs.get("retain"))

    def test_aid_mode_off_publishes_off_state(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("AID_SET", "OFF")
        from nibe_mqtt_publisher import MgmtTopic

        call = next(c for c in self.mqtt.publish.call_args_list if c.args[0] == MgmtTopic.AID_STATE)
        self.assertEqual(call.args[1], "OFF")

    def test_aid_mode_does_not_publish_state_on_failure(self):
        self.em._api.write_device_mode = MagicMock(return_value=False)
        self._run("AID_SET", "ON")
        from nibe_mqtt_publisher import MgmtTopic

        topics = [c.args[0] for c in self.mqtt.publish.call_args_list]
        self.assertNotIn(MgmtTopic.AID_STATE, topics)

    def test_aid_mode_success_bumps_write_seq(self):
        """Regression: _publish_device_modes (nibe_ha_integration.py) uses
        device_modes_write_seq to detect a write landing while its own
        fetch_device_info() call is in flight — without bumping it here,
        that race-detection is a no-op and a concurrent write's dirty=True
        can be silently clobbered by a stale in-flight fetch's result."""
        self.em._api.write_device_mode = MagicMock(return_value=True)
        before = self.em.device_modes_write_seq
        self._run("AID_SET", "ON")
        self.assertEqual(self.em.device_modes_write_seq, before + 1)

    def test_aid_mode_write_seq_increments_not_resets_on_repeated_writes(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        handler = self._get_handler("AID_SET")
        handler(None, None, self._msg("ON"))
        handler(None, None, self._msg("OFF"))
        self.executor.shutdown(wait=True)
        self.assertEqual(self.em.device_modes_write_seq, 2)

    def test_aid_mode_success_marks_device_modes_dirty(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self.em.device_modes_dirty = False
        self._run("AID_SET", "ON")
        self.assertIs(self.em.device_modes_dirty, True)

    def test_aid_mode_failure_does_not_bump_write_seq(self):
        self.em._api.write_device_mode = MagicMock(return_value=False)
        before = self.em.device_modes_write_seq
        self._run("AID_SET", "ON")
        self.assertEqual(self.em.device_modes_write_seq, before)

    # ── smart mode handler ────────────────────────────────────────────────────

    def test_smart_mode_normal(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("SMART_SET", "normal")
        self.em._api.write_device_mode.assert_called_with("smartmode", "normal")

    def test_smart_mode_away(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("SMART_SET", "away")
        self.em._api.write_device_mode.assert_called_with("smartmode", "away")

    def test_smart_mode_invalid_value_ignored(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        with self.assertLogs("nibe.commands", level="ERROR") as cm:
            self._run("SMART_SET", "holiday")
        self.em._api.write_device_mode.assert_not_called()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Invalid smart mode value: 'holiday' — expected 'normal' or 'away'"
                )
                for msg in cm.output
            )
        )

    def test_smart_mode_does_not_publish_state_on_failure(self):
        self.em._api.write_device_mode = MagicMock(return_value=False)
        self._run("SMART_SET", "away")
        from nibe_mqtt_publisher import MgmtTopic

        topics = [c.args[0] for c in self.mqtt.publish.call_args_list]
        self.assertNotIn(MgmtTopic.SMART_STATE, topics)

    def test_smart_mode_publishes_exact_value_retained(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self._run("SMART_SET", "away")
        from nibe_mqtt_publisher import MgmtTopic

        call = next(
            c for c in self.mqtt.publish.call_args_list if c.args[0] == MgmtTopic.SMART_STATE
        )
        self.assertEqual(call.args[1], "away")
        self.assertTrue(call.kwargs.get("retain"))

    def test_smart_mode_success_bumps_write_seq(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        before = self.em.device_modes_write_seq
        self._run("SMART_SET", "away")
        self.assertEqual(self.em.device_modes_write_seq, before + 1)

    def test_smart_mode_write_seq_increments_not_resets_on_repeated_writes(self):
        """A second successful write must increment write_seq again (2), not
        reset it back to a fixed value — `= 1` in place of `+= 1` would
        pass a single-call before/after check but corrupt every write after
        the first."""
        self.em._api.write_device_mode = MagicMock(return_value=True)
        handler = self._get_handler("SMART_SET")
        handler(None, None, self._msg("away"))
        handler(None, None, self._msg("normal"))
        self.executor.shutdown(wait=True)
        self.assertEqual(self.em.device_modes_write_seq, 2)

    def test_smart_mode_success_marks_device_modes_dirty(self):
        self.em._api.write_device_mode = MagicMock(return_value=True)
        self.em.device_modes_dirty = False
        self._run("SMART_SET", "away")
        self.assertIs(self.em.device_modes_dirty, True)

    def test_smart_mode_failure_does_not_bump_write_seq(self):
        self.em._api.write_device_mode = MagicMock(return_value=False)
        before = self.em.device_modes_write_seq
        self._run("SMART_SET", "away")
        self.assertEqual(self.em.device_modes_write_seq, before)

    # ── reset alarms handler ──────────────────────────────────────────────────

    def test_reset_alarms_calls_reset_notifications(self):
        self.em._api.reset_notifications = MagicMock(return_value=True)
        self._run("ALARM_RESET_PRESS", "")
        self.em._api.reset_notifications.assert_called_once()

    def test_reset_alarms_publishes_zero_alarm_state(self):
        self.em._api.reset_notifications = MagicMock(return_value=True)
        self._run("ALARM_RESET_PRESS", "")
        from nibe_mqtt_publisher import MgmtTopic

        publish_calls = {c.args[0]: c.args[1] for c in self.mqtt.publish.call_args_list}
        self.assertEqual(publish_calls.get(MgmtTopic.ALARM_STATE), "0")
        state_call = next(
            c for c in self.mqtt.publish.call_args_list if c.args[0] == MgmtTopic.ALARM_STATE
        )
        self.assertTrue(state_call.kwargs.get("retain"))
        attrs_call = next(
            c for c in self.mqtt.publish.call_args_list if c.args[0] == MgmtTopic.ALARM_ATTRS
        )
        import json as _json

        payload = _json.loads(attrs_call.args[1])
        self.assertEqual(payload["alarms"], [])
        self.assertIn("last_updated", payload)
        self.assertTrue(attrs_call.kwargs.get("retain"))

    def test_reset_alarms_no_publish_on_failure(self):
        self.em._api.reset_notifications = MagicMock(return_value=False)
        self._run("ALARM_RESET_PRESS", "")
        from nibe_mqtt_publisher import MgmtTopic

        topics = [c.args[0] for c in self.mqtt.publish.call_args_list]
        self.assertNotIn(MgmtTopic.ALARM_STATE, topics)

    def test_handler_exception_is_logged_not_silently_swallowed(self):
        """A raised exception inside a handler's executor-submitted closure
        must be logged, not silently swallowed — a bare executor.submit()
        with nothing awaiting the Future would otherwise lose the error
        entirely, with no log line and no way to diagnose the failure."""
        self.em._api.reset_notifications = MagicMock(side_effect=RuntimeError("boom"))
        with patch("nibe_ha_integration.log_commands.exception") as mock_log:
            self._run("ALARM_RESET_PRESS", "")  # must not raise
        mock_log.assert_called_once()

    # ── force poll handler ────────────────────────────────────────────────────

    def test_force_poll_calls_update_all_states(self):
        with (
            patch.object(self.em, "update_all_states") as mock_update,
            self.assertLogs("nibe.startup", level="INFO") as cm,
        ):
            self._run("FORCE_POLL_PRESS", "")
        mock_update.assert_called_once_with(force=True)
        self.assertTrue(
            any(msg.splitlines()[0].endswith("Force poll triggered from HA") for msg in cm.output)
        )

    def test_force_poll_calls_update_stats_and_health_with_em_and_pub(self):
        with patch("nibe_ha_integration.update_stats_and_health") as mock_stats:
            self._run("FORCE_POLL_PRESS", "")
        mock_stats.assert_called_once_with(self.em, self.publisher)

    def test_force_poll_calls_publish_device_modes_with_em_and_pub(self):
        with patch("nibe_ha_integration._publish_device_modes") as mock_modes:
            self._run("FORCE_POLL_PRESS", "")
        mock_modes.assert_called_once_with(self.em, self.publisher)

    # ── enable / disable handlers ─────────────────────────────────────────────

    def test_enable_valid_point_id_calls_enable_entity(self):
        with patch.object(self.em, "enable_entity", return_value=True) as mock_en:
            self._run("ENABLE_SET", "1234")
        mock_en.assert_called_once_with(1234)

    def test_enable_invalid_payload_does_not_raise(self):
        with (
            patch.object(self.em, "enable_entity") as mock_en,
            self.assertLogs("nibe.commands", level="WARNING") as cm,
        ):
            self._run("ENABLE_SET", "notanumber")
        mock_en.assert_not_called()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("handle_enable: invalid point id 'notanumber'")
                for msg in cm.output
            )
        )

    def test_enable_returning_false_does_not_publish_stats(self):
        with (
            patch.object(self.em, "enable_entity", return_value=False),
            patch("nibe_ha_integration._publish_stats") as mock_stats,
        ):
            self._run("ENABLE_SET", "1234")
        mock_stats.assert_not_called()

    def test_enable_returning_true_publishes_stats_with_em_and_pub(self):
        with (
            patch.object(self.em, "enable_entity", return_value=True),
            patch("nibe_ha_integration._publish_stats") as mock_stats,
        ):
            self._run("ENABLE_SET", "1234")
        mock_stats.assert_called_once_with(self.em, self.publisher)

    def test_disable_valid_point_id_calls_disable_entity(self):
        with patch.object(self.em, "disable_entity", return_value=True) as mock_dis:
            self._run("DISABLE_SET", "5678")
        mock_dis.assert_called_once_with(5678)

    def test_disable_invalid_payload_does_not_raise(self):
        with (
            patch.object(self.em, "disable_entity") as mock_dis,
            self.assertLogs("nibe.commands", level="WARNING") as cm,
        ):
            self._run("DISABLE_SET", "bad")
        mock_dis.assert_not_called()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("handle_disable: invalid point id 'bad'")
                for msg in cm.output
            )
        )

    def test_disable_returning_false_does_not_publish_stats(self):
        with (
            patch.object(self.em, "disable_entity", return_value=False),
            patch("nibe_ha_integration._publish_stats") as mock_stats,
        ):
            self._run("DISABLE_SET", "5678")
        mock_stats.assert_not_called()

    def test_disable_returning_true_publishes_stats_with_em_and_pub(self):
        with (
            patch.object(self.em, "disable_entity", return_value=True),
            patch("nibe_ha_integration._publish_stats") as mock_stats,
        ):
            self._run("DISABLE_SET", "5678")
        mock_stats.assert_called_once_with(self.em, self.publisher)

    # ── changelog reset handler ───────────────────────────────────────────────

    def test_changelog_reset_calls_mark_changelog_read(self):
        """Regression: _handle_changelog_reset now dispatches through
        _submit() (like every other handler) so an exception in
        mark_changelog_read() gets this project's own log_commands.exception
        safety net instead of silently vanishing into paho's own message-
        dispatch exception handling — so this must wait for the executor via
        _run(), not call the handler synchronously and assert immediately."""
        with (
            patch.object(self.em, "mark_changelog_read") as mock_read,
            self.assertLogs("nibe.history", level="INFO") as cm,
        ):
            self._run("CHANGELOG_READ_PRESS", "")
        mock_read.assert_called_once()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Changelog reset requested by user")
                for msg in cm.output
            )
        )

    # ── flush dynamic map handler ───────────────────────────────────────────

    def test_flush_dynamic_map_calls_flush_with_current_points(self):
        self.em.all_points_by_id = {100: {"entity_type": "switch"}}
        with (
            patch.object(self.em.dynamic_point_map, "flush") as mock_flush,
            self.assertLogs("nibe.commands", level="INFO") as cm,
        ):
            self._run("FLUSH_MAP_PRESS", "")
        mock_flush.assert_called_once_with(
            self.em.all_points_by_id,
            {100: "switch"},
        )
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Flush Dynamic Map triggered from HA (debug action)")
                for msg in cm.output
            )
        )

    def test_flush_dynamic_map_persists_after_flush(self):
        """The flush must be persisted to disk immediately — otherwise a
        restart before the next natural save would silently undo the flush."""
        with (
            patch.object(self.em.dynamic_point_map, "flush"),
            patch.object(self.em, "_persist_dynamic_map") as mock_persist,
            self.assertLogs("nibe.commands", level="INFO") as cm,
        ):
            self._run("FLUSH_MAP_PRESS", "")
        mock_persist.assert_called_once()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Dynamic map flushed — all entries reset to unprocessed"
                )
                for msg in cm.output
            )
        )

    def test_flush_dynamic_map_entity_types_default_to_empty_string(self):
        """A point missing the entity_type key must not crash the flush —
        defaults to '' rather than KeyError."""
        self.em.all_points_by_id = {200: {}}  # no entity_type key
        with patch.object(self.em.dynamic_point_map, "flush") as mock_flush:
            self._run("FLUSH_MAP_PRESS", "")
        mock_flush.assert_called_once_with(
            self.em.all_points_by_id,
            {200: ""},
        )

    def test_flush_dynamic_map_serializes_against_em_lock(self):
        """dynamic_point_map._table is also mutated (under _em_lock) by
        _run_learning_detection (write executor thread) and
        _publish_dynamic_changes (poll thread) — the flush handler runs on
        mgmt_executor's thread and must serialize against those the same
        way, or a flush landing mid-mutation on either of those threads
        corrupts the table. Regression test proving real mutual exclusion:
        holds em._em_lock on the main thread and confirms the flush handler
        genuinely blocks on it via a separate executor thread, rather than
        sailing through — not just that the final state looks right.

        _handle_flush_dynamic_map itself only submits the real work (_do,
        an inner closure) to self.executor and returns immediately — so the
        future for calling the handler directly would report done() the
        instant it finishes queuing, regardless of whether the queued _do()
        has even started. Capturing the real submit()'s return value gets
        the *inner* future for _do itself, which is the one that actually
        acquires _em_lock and is what this test needs to observe.
        """
        import time as _time

        original_submit = self.executor.submit
        captured_futures = []

        def _capturing_submit(fn, *a, **kw):
            fut = original_submit(fn, *a, **kw)
            captured_futures.append(fut)
            return fut

        with (
            patch.object(self.em.dynamic_point_map, "flush"),
            patch.object(self.em, "_persist_dynamic_map"),
        ):
            handler = self._get_handler("FLUSH_MAP_PRESS")
            with patch.object(self.executor, "submit", side_effect=_capturing_submit):
                with self.em._em_lock:
                    handler(None, None, self._msg(""))
                    inner_future = captured_futures[0]
                    # Give the queued _do() ample opportunity to run if it
                    # were NOT actually blocked on _em_lock — everything it
                    # touches besides the lock is mocked, so a real race
                    # would complete almost instantly.
                    _time.sleep(0.3)
                    self.assertFalse(
                        inner_future.done(),
                        "Flush Dynamic Map's queued work completed while "
                        "_em_lock was held by another thread — the "
                        "flush/persist calls are not actually serialized "
                        "by the lock",
                    )
                # Lock released — the queued work must now complete promptly.
                inner_future.result(timeout=5)

    # ── run test suite handler ────────────────────────────────────────────────

    def _run_tests_call_args(self):
        """Helper: return all publish calls on em.mqtt for run_tests topics."""
        return [(c.args[0], c.args[1]) for c in self.em.mqtt.publish.call_args_list]

    def test_run_tests_publishes_running_state_immediately(self):
        """Pressing the button must immediately publish 'running' before subprocess completes."""
        with patch("subprocess.Popen") as mock_run, patch("nibe_ha_integration.dismiss_ha"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1543 passed in 15.0s", stderr=""
            )
            mock_run.return_value.communicate.return_value = ("1543 passed in 15.0s", "")
            self._run("RUN_TESTS_PRESS", "")
        from nibe_mqtt_publisher import MgmtTopic

        states = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn("running", states)

    def test_run_tests_publishes_passed_on_success(self):
        """Exit code 0 → state topic must contain 'passed'."""
        with patch("subprocess.Popen") as mock_run, patch("nibe_ha_integration.dismiss_ha"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1543 passed in 15.0s", stderr=""
            )
            mock_run.return_value.communicate.return_value = ("1543 passed in 15.0s", "")
            self._run("RUN_TESTS_PRESS", "")
        from nibe_mqtt_publisher import MgmtTopic

        states = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn("passed", states)

    def test_run_tests_publishes_failed_on_failure(self):
        """Non-zero exit code → state topic must contain 'failed'."""
        with patch("subprocess.Popen") as mock_run, patch("nibe_ha_integration.notify_ha"):
            mock_run.return_value = MagicMock(returncode=1, stdout="1 failed in 15.0s", stderr="")
            mock_run.return_value.communicate.return_value = ("1 failed in 15.0s", "")
            self._run("RUN_TESTS_PRESS", "")
        from nibe_mqtt_publisher import MgmtTopic

        states = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn("failed", states)

    def test_run_tests_pass_does_not_send_notification(self):
        """On pass, no HA notification — result is on the sensor attributes tab."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("nibe_ha_integration.notify_ha") as mock_notify,
            patch("nibe_ha_integration.dismiss_ha") as mock_dismiss,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1543 passed in 15.0s", stderr=""
            )
            mock_run.return_value.communicate.return_value = ("1543 passed in 15.0s", "")
            self._run("RUN_TESTS_PRESS", "")
        mock_notify.assert_not_called()
        mock_dismiss.assert_called_once()

    def test_run_tests_pass_dismisses_previous_failure_notification(self):
        """On pass, any previous failure notification must be dismissed."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("nibe_ha_integration.dismiss_ha") as mock_dismiss,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1543 passed in 15.0s", stderr=""
            )
            mock_run.return_value.communicate.return_value = ("1543 passed in 15.0s", "")
            self._run("RUN_TESTS_PRESS", "")
        mock_dismiss.assert_called_once()

    def test_run_tests_notification_title_shows_failed(self):
        """Notification title must include 'FAILED' on failure."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("nibe_ha_integration.notify_ha") as mock_notify,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="1 failed", stderr="")
            mock_run.return_value.communicate.return_value = ("1 failed", "")
            self._run("RUN_TESTS_PRESS", "")
        _, kwargs = mock_notify.call_args
        self.assertEqual(kwargs.get("title", ""), "Nibe Test Suite — ❌ FAILED")

    def test_run_tests_subprocess_timeout_handled_gracefully(self):
        """subprocess.TimeoutExpired must not propagate — state becomes 'timed_out'."""
        from nibe_mqtt_publisher import MgmtTopic

        mock_proc = MagicMock(pid=12345)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 3600),  # the 4-hour-limit wait
            ("", ""),  # the post-kill() drain
        ]
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("os.getpgid", return_value=99999) as mock_getpgid,
            patch("os.killpg") as mock_killpg,
        ):
            self._run("RUN_TESTS_PRESS", "")
        states = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn("timed_out", states)
        # The whole process group is killed, not just the top-level PID —
        # proc.kill() alone would leave orphaned pytest-xdist workers
        # running, still holding the output pipes open.
        mock_getpgid.assert_called_once_with(12345)
        mock_killpg.assert_called_once_with(99999, signal.SIGKILL)

    def test_run_tests_timeout_notification_title(self):
        """TimeoutExpired must produce a '⏱ TIMED OUT' notification, not '❌ FAILED'."""
        mock_proc = MagicMock(pid=12345)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 3600),
            ("", ""),
        ]
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("nibe_ha_integration.notify_ha") as mock_notify,
            patch("builtins.open", MagicMock()),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
        ):
            self._run("RUN_TESTS_PRESS", "")
        if mock_notify.called:
            kwargs = mock_notify.call_args.kwargs
            self.assertEqual(kwargs.get("title", ""), "Nibe Test Suite — ⏱ TIMED OUT")

    def test_run_tests_timeout_no_output_captured_summary(self):
        """When the killed process's drain produces no output at all, the
        published summary must say so explicitly, verbatim — this is the
        exact text surfaced on the sensor's attributes tab."""
        import json as _json

        from nibe_mqtt_publisher import MgmtTopic

        mock_proc = MagicMock(pid=12345)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 3600),
            ("", ""),
        ]
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("builtins.open", MagicMock()),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
        ):
            self._run("RUN_TESTS_PRESS", "")
        attrs_calls = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_ATTRS]
        final = _json.loads(attrs_calls[-1])
        self.assertEqual(
            final["summary"],
            "No output was captured before the process was killed — "
            "check the add-on log for the exact kill time.",
        )

    def test_run_tests_timeout_with_long_captured_output_truncates_to_2000(self):
        """The composed 'output' string embeds partial_output[-2000:] — a
        single-line drained output longer than that becomes the published
        summary verbatim (via the counts_line fallback), so an off-by-one
        slice bound here is observable through the exact published text."""
        import json as _json

        from nibe_mqtt_publisher import MgmtTopic

        long_output = "y" * 2500 + "TAIL_MARKER"
        mock_proc = MagicMock(pid=12345)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 3600),
            (long_output, ""),
        ]
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("builtins.open", MagicMock()),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
        ):
            self._run("RUN_TESTS_PRESS", "")
        attrs_calls = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_ATTRS]
        final = _json.loads(attrs_calls[-1])
        self.assertEqual(final["summary"], long_output[-2000:])

    def test_run_tests_timeout_with_captured_output_summary(self):
        """When the killed process's drain does produce output, the
        published summary must be exactly that drained text (the tail line
        of the composed message), not an empty placeholder."""
        import json as _json

        from nibe_mqtt_publisher import MgmtTopic

        mock_proc = MagicMock(pid=12345)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 3600),
            ("partial pytest output here", ""),
        ]
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("builtins.open", MagicMock()),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
        ):
            self._run("RUN_TESTS_PRESS", "")
        attrs_calls = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_ATTRS]
        final = _json.loads(attrs_calls[-1])
        self.assertEqual(final["summary"], "partial pytest output here")

    def test_run_tests_timeout_diagnostic_error_log_has_exact_text_and_real_args(self):
        """The kill-time diagnostic log_commands.error() call — text, real
        pid, real byte count, and the real drained output — is otherwise
        untested (it only reaches the add-on log, not the published
        summary/notification checked by the sibling tests above)."""
        mock_proc = MagicMock(pid=12345)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 3600),
            ("partial pytest output here", ""),
        ]
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("builtins.open", MagicMock()),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
            patch("nibe_test_runner.log_commands") as mock_log,
        ):
            self._run("RUN_TESTS_PRESS", "")
        error_call = next(
            c
            for c in mock_log.error.call_args_list
            if c.args[0].startswith("Test suite subprocess did not finish")
        )
        self.assertEqual(
            error_call.args[0],
            "Test suite subprocess did not finish within the 14400s hard "
            "limit — killed process group (pid %d) after %.1fs elapsed. "
            "Captured output (%d bytes):\n%s",
        )
        self.assertEqual(error_call.args[1], 12345)
        self.assertEqual(error_call.args[3], len("partial pytest output here"))
        self.assertEqual(error_call.args[4], "partial pytest output here")

    def test_run_tests_timeout_diagnostic_error_log_truncates_to_last_4000_bytes(self):
        """The diagnostic log's captured-output tail must be exactly the
        last 4000 bytes of the drained output — an off-by-one slice bound
        only shows up with output longer than that limit."""
        long_output = "x" * 5000 + "TAIL_MARKER"
        mock_proc = MagicMock(pid=12345)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 3600),
            (long_output, ""),
        ]
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("builtins.open", MagicMock()),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
            patch("nibe_test_runner.log_commands") as mock_log,
        ):
            self._run("RUN_TESTS_PRESS", "")
        error_call = next(
            c
            for c in mock_log.error.call_args_list
            if c.args[0].startswith("Test suite subprocess did not finish")
        )
        self.assertEqual(error_call.args[4], long_output[-4000:])
        self.assertEqual(len(error_call.args[4]), 4000)

    def test_run_tests_timeout_diagnostic_error_log_no_output_fallback_text(self):
        mock_proc = MagicMock(pid=12345)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 3600),
            ("", ""),
        ]
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("builtins.open", MagicMock()),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
            patch("nibe_test_runner.log_commands") as mock_log,
        ):
            self._run("RUN_TESTS_PRESS", "")
        error_call = next(
            c
            for c in mock_log.error.call_args_list
            if c.args[0].startswith("Test suite subprocess did not finish")
        )
        self.assertEqual(error_call.args[4], "(nothing captured)")

    def test_run_tests_launch_error_state_is_error(self):
        """An unexpected exception launching the subprocess must set state='error',
        not 'failed' or 'timed_out'."""
        from nibe_mqtt_publisher import MgmtTopic

        with patch("subprocess.Popen", side_effect=OSError("no such file: python3")):
            self._run("RUN_TESTS_PRESS", "")
        states = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn("error", states)

    def test_run_tests_launch_error_notification_title(self):
        """A launch error must produce a '⚠ LAUNCH ERROR' notification title."""
        with (
            patch("subprocess.Popen", side_effect=OSError("no such file: python3")),
            patch("nibe_ha_integration.notify_ha") as mock_notify,
            patch("builtins.open", MagicMock()),
        ):
            self._run("RUN_TESTS_PRESS", "")
        if mock_notify.called:
            kwargs = mock_notify.call_args.kwargs
            self.assertEqual(kwargs.get("title", ""), "Nibe Test Suite — ⚠ LAUNCH ERROR")

    def test_run_tests_launch_error_notification_body_is_the_real_output(self):
        """The launch-error notification body must be the real output text
        — not None. counts_line (the message's first line) is *also*
        derived from `output` here (a single-line string has no other
        line to extract), so a loose assertIn on the whole message can't
        distinguish body=output from body=None: the exception text leaks
        in via counts_line regardless. Splitting out the body section
        specifically (between the double-newlines) closes that gap."""
        with (
            patch("subprocess.Popen", side_effect=OSError("no such file: python3")),
            patch("nibe_ha_integration.notify_ha") as mock_notify,
            patch("builtins.open", MagicMock()),
        ):
            self._run("RUN_TESTS_PRESS", "")
        self.assertTrue(mock_notify.called)
        kwargs = mock_notify.call_args.kwargs
        message = kwargs.get("message", "")
        # message = f'{timestamp} — {counts_line} — {elapsed_str}\n\n{body}\n\n{report_link}'
        body_section = message.split("\n\n")[1]
        self.assertIn("no such file: python3", body_section)

    def test_run_tests_uses_nightly_hypothesis_profile(self):
        """The subprocess must be launched with HYPOTHESIS_PROFILE=nightly."""
        with patch("subprocess.Popen") as mock_run, patch("nibe_ha_integration.dismiss_ha"):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        env = mock_run.call_args.kwargs.get("env", {})
        self.assertEqual(env.get("HYPOTHESIS_PROFILE"), "nightly")

    def test_run_tests_generates_html_report(self):
        """pytest must be invoked with --html pointing to /homeassistant/www/ and
        Report is written to /homeassistant/www/nibe_test_report.html (assets in
        /homeassistant/www/assets/ — pytest-html 4.x multi-file output)."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        args = mock_run.call_args.args[0]
        html_args = [a for a in args if a.startswith("--html=")]
        self.assertEqual(len(html_args), 1)
        self.assertEqual(html_args[0], "--html=/homeassistant/www/nibe_test_report.html")
        self.assertNotIn("--self-contained-html", args)

    def test_run_tests_logs_warning_when_report_missing(self):
        """If the HTML report is absent (e.g. pytest-html not installed in the
        Docker image), a clear WARNING must be emitted rather than silently
        swallowing the FileNotFoundError with a bare except."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", side_effect=FileNotFoundError),
            patch("nibe_ha_integration.dismiss_ha"),
            patch("nibe_test_runner.log_commands") as mock_log,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        warning_msgs = [str(c) for c in mock_log.warning.call_args_list]
        self.assertTrue(
            any("pytest-html" in m or "not found" in m.lower() for m in warning_msgs),
            "Expected a warning about the missing HTML report",
        )

    def test_run_tests_report_missing_warning_has_exact_text_and_real_path(self):
        """The sibling test above uses a loose `in` check; assert the
        mocked call's exact args instead, since that's the only way to
        distinguish the real text/path from a dropped arg or XX-wrapped
        mutant."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", side_effect=FileNotFoundError),
            patch("nibe_ha_integration.dismiss_ha"),
            patch("nibe_test_runner.log_commands") as mock_log,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        mock_log.warning.assert_called_once_with(
            "Test suite HTML report not found at %s — "
            "pytest-html may not be installed in the Docker image. "
            "Check requirements-test.txt and rebuild the add-on.",
            "/homeassistant/www/nibe_test_report.html",
        )

    def test_run_tests_launch_exception_logged_with_exact_text(self):
        with (
            patch("subprocess.Popen", side_effect=OSError("boom")),
            patch("nibe_ha_integration.dismiss_ha"),
            patch("nibe_test_runner.log_commands") as mock_log,
        ):
            self._run("RUN_TESTS_PRESS", "")
        mock_log.exception.assert_called_once_with("Failed to launch test suite subprocess")

    def test_run_tests_failure_notification_contains_report_link(self):
        """Failure notification must include a link to the HTML report so
        the user can open it directly from the HA notification bell."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.notify_ha") as mock_notify,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="FAILED test_x", stderr="")
            mock_run.return_value.communicate.return_value = ("FAILED test_x", "")
            self._run("RUN_TESTS_PRESS", "")
        self.assertTrue(mock_notify.called)
        message = mock_notify.call_args.kwargs.get("message", "")
        self.assertIn("nibe_test_report.html", message)

    def test_run_tests_initial_running_state_attrs_has_status_and_started_keys(self):
        """The very first RUN_TESTS_ATTRS publish (before the subprocess
        even starts) must be {'status': 'running', 'started': <timestamp>}
        — a wrong key here breaks the HA sensor attribute the frontend
        polls to show 'test run in progress since HH:MM'."""
        import json as _json

        from nibe_mqtt_publisher import MgmtTopic

        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        attrs_calls = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_ATTRS]
        first = _json.loads(attrs_calls[0])
        self.assertEqual(first["status"], "running")
        self.assertIn("started", first)

    def test_run_tests_publishes_attrs_with_summary(self):
        """The final attrs publish must contain a non-empty summary."""
        import json as _json

        from nibe_mqtt_publisher import MgmtTopic

        with patch("subprocess.Popen") as mock_run, patch("nibe_ha_integration.dismiss_ha"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1543 passed in 15.0s", stderr=""
            )
            mock_run.return_value.communicate.return_value = ("1543 passed in 15.0s", "")
            self._run("RUN_TESTS_PRESS", "")
        attrs_calls = [p for t, p in self._run_tests_call_args() if t == MgmtTopic.RUN_TESTS_ATTRS]
        self.assertGreaterEqual(len(attrs_calls), 2)
        final = _json.loads(attrs_calls[-1])
        self.assertIn("summary", final)
        self.assertIn("1543 passed", final["summary"])

    def test_run_tests_subprocess_args_list_exact(self):
        """The full pytest invocation argv (flags, order, and exact values)
        must match exactly — each flag here (-m, pytest, --tb=short,
        --no-header, -q) controls real subprocess behavior (module lookup,
        traceback verbosity, header suppression, output verbosity)."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        args = mock_run.call_args.args[0]
        python_exe = args[0]
        self.assertEqual(
            args,
            [
                python_exe,
                "-m",
                "pytest",
                args[3],
                "--html=/homeassistant/www/nibe_test_report.html",
                "--tb=short",
                "--no-header",
                "-q",
                "--timeout=600",
                "-n",
                "auto",
            ],
        )

    def test_run_tests_communicate_timeout_is_14400(self):
        """communicate() must be called with the 4-hour hard limit — a
        wrong value here changes when a hung test run actually gets
        killed."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        kwargs = mock_run.return_value.communicate.call_args.kwargs
        self.assertEqual(kwargs.get("timeout"), 14400)

    def test_run_tests_html_report_opened_with_utf8_encoding(self):
        """The generated HTML report must be read back with an explicit
        utf-8 encoding rather than relying on the platform default, since
        pytest-html always writes utf-8 regardless of container locale."""
        mock_open = MagicMock()
        mock_open.return_value.__enter__.return_value.read.return_value = (
            '<meta charset="utf-8"/><style>.container{min-width:800px}</style>'
        )
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", mock_open),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        read_call = next(
            c
            for c in mock_open.call_args_list
            if c.args and str(c.args[0]).endswith("nibe_test_report.html")
        )
        self.assertEqual(read_call.kwargs.get("encoding"), "utf-8")

    def test_run_tests_html_report_postprocessing_exact(self):
        """The HTML report is rewritten in place: opened for writing at the
        same report_path with utf-8 encoding, and the written content has
        the viewport meta tag injected right after the charset meta tag and
        'min-width: 800px' narrowed to 'min-width: 320px' — both edits are
        what make pytest-html's desktop-oriented report usable on a phone."""
        mock_open = MagicMock()
        mock_open.return_value.__enter__.return_value.read.return_value = (
            '<html><head><meta charset="utf-8"/></head>'
            "<style>.container{min-width: 800px}</style></html>"
        )
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", mock_open),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        write_call = next(
            c for c in mock_open.call_args_list if len(c.args) >= 2 and c.args[1] == "w"
        )
        self.assertEqual(write_call.args[0], "/homeassistant/www/nibe_test_report.html")
        self.assertEqual(write_call.kwargs.get("encoding"), "utf-8")
        written = mock_open.return_value.__enter__.return_value.write.call_args.args[0]
        self.assertIn(
            '<meta charset="utf-8"/>\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1"/>',
            written,
        )
        self.assertIn("min-width: 320px", written)
        self.assertNotIn("min-width: 800px", written)
        # assertIn alone can't distinguish the real replacement text from an
        # XX-wrapped mutant ('min-width: 320px' is still a substring of
        # 'XXmin-width: 320pxXX') — an exact-equality check on the isolated
        # style-block content closes that gap.
        style_content = written.split(".container{")[1].split("}")[0]
        self.assertEqual(style_content, "min-width: 320px")

    def test_run_tests_subprocess_uses_per_test_timeout_600(self):
        """pytest must be invoked with --timeout=600 so that long-running
        nightly Hypothesis stateful tests (stateful_step_count=50) are not
        killed by pytest.ini's default timeout=300."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        args = mock_run.call_args.args[0]
        self.assertIn("--timeout=600", args)

    def test_run_tests_subprocess_uses_xdist_auto(self):
        """pytest must be invoked with -n auto so xdist distributes tests
        across all available CPU cores (~4 on the ODROID-M1)."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        args = mock_run.call_args.args[0]
        self.assertIn("-n", args)
        n_idx = args.index("-n")
        self.assertEqual(args[n_idx + 1], "auto")

    def test_run_tests_subprocess_starts_new_session(self):
        """The pytest subprocess must launch with start_new_session=True so
        it becomes the leader of its own process group, separate from the
        add-on's own group. Without this, abort_test_suite()'s
        os.killpg(os.getpgid(proc.pid), ...) would target the wrong group
        (or fail outright) — start_new_session is what makes killing the
        whole pytest -n auto process tree (including xdist workers)
        possible at all."""
        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        self.assertTrue(mock_run.call_args.kwargs.get("start_new_session"))

    def test_run_tests_subprocess_kwargs_pipe_and_text_and_cwd(self):
        """stdout/stderr must be captured via subprocess.PIPE (not None,
        which would let the child inherit this process's own stdout/stderr
        instead of being captured for communicate()), text=True (else
        communicate() returns bytes, and the string concatenation/regex
        parsing later in this function would crash), and cwd must be a
        real, non-None directory (pytest.ini lives there and configures
        testpaths/pythonpath relative to it)."""
        import os as _os
        import subprocess as _subprocess

        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs.get("stdout"), _subprocess.PIPE)
        self.assertEqual(kwargs.get("stderr"), _subprocess.PIPE)
        self.assertIs(kwargs.get("text"), True)
        self.assertIsNotNone(kwargs.get("cwd"))
        self.assertTrue(_os.path.isabs(kwargs.get("cwd")))

    def test_run_tests_test_path_arg_is_real_absolute_tests_dir(self):
        """The test_path positional arg passed to pytest must be a real,
        absolute path to a 'tests' directory. In this dev/CI environment
        the hardcoded '/tests' container path never exists, so the
        fallback branch (os.path.join(addon_dir, 'tests')) always runs —
        an inverted isdir() check, or a fallback that silently drops
        addon_dir, would point pytest at a nonexistent or wrong-relative
        location, and it wouldn't collect (or would silently collect
        zero) tests."""
        import os as _os

        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        args = mock_run.call_args.args[0]
        test_path = args[3]
        self.assertTrue(_os.path.isabs(test_path))
        self.assertTrue(_os.path.isdir(test_path))
        self.assertTrue(test_path.endswith("tests"))

    def test_run_tests_pytest_ini_lookup_uses_absolute_addon_dir_path(self):
        """pytest_ini = os.path.join(addon_dir, 'pytest.ini') must resolve
        to an absolute path — dropping addon_dir would make the
        os.path.exists() check resolve relative to whatever the current
        working directory happens to be at runtime (unpredictable in a
        container), silently picking the wrong run_dir when it doesn't
        coincidentally match."""
        import os as _os

        real_exists = _os.path.exists
        seen_paths = []

        def spy_exists(path):
            seen_paths.append(path)
            return real_exists(path)

        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
            patch("nibe_test_runner.os.path.exists", side_effect=spy_exists),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        ini_checks = [p for p in seen_paths if p.endswith("pytest.ini")]
        self.assertTrue(ini_checks, "Expected an os.path.exists() check for pytest.ini")
        self.assertTrue(_os.path.isabs(ini_checks[0]))

    def test_run_tests_pythonpath_env_is_absolute_app_dir(self):
        """The subprocess's PYTHONPATH must be an absolute path to app/ —
        dropping addon_dir would leave a bare relative 'app', which only
        resolves correctly if the subprocess's cwd happens to already be
        the addon root (not guaranteed)."""
        import os as _os

        with (
            patch("subprocess.Popen") as mock_run,
            patch("builtins.open", MagicMock()),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value.communicate.return_value = ("", "")
            self._run("RUN_TESTS_PRESS", "")
        env = mock_run.call_args.kwargs.get("env", {})
        pythonpath = env.get("PYTHONPATH", "")
        self.assertTrue(_os.path.isabs(pythonpath))
        self.assertTrue(pythonpath.endswith("app"))

    def test_run_tests_concurrent_trigger_ignored(self):
        """A second button press while a run is in flight must be silently
        dropped — subprocess.run must only be called once.

        Simulated by pre-setting _test_running on the handler before the
        second press, which is exactly the state during an in-flight run.
        """
        import concurrent.futures

        from nibe_ha_integration import ManagementCommandHandler

        em = _make_em()
        pub = MagicMock()
        exe = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            handler = ManagementCommandHandler(em.mqtt, em, pub, exe)
            handler.register_all()

            with patch("subprocess.Popen") as mock_run, patch("builtins.open", MagicMock()):
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="2652 passed in 26m 0s", stderr=""
                )
                mock_run.return_value.communicate.return_value = ("2652 passed in 26m 0s", "")

                # Simulate an in-flight run by pre-setting the flag
                handler._test_running.set()

                # Trigger — should be ignored
                msg = MagicMock()
                msg.payload = b""
                with self.assertLogs("nibe.commands", level="INFO") as cm:
                    handler._handle_run_tests(None, None, msg)

            mock_run.assert_not_called()
            self.assertTrue(
                any(
                    msg_.splitlines()[0].endswith(
                        "Test suite already running — ignoring duplicate trigger"
                    )
                    for msg_ in cm.output
                )
            )
        finally:
            exe.shutdown(wait=False)

    def test_run_tests_first_trigger_logs_start_message(self):
        import concurrent.futures

        from nibe_ha_integration import ManagementCommandHandler

        em = _make_em()
        pub = MagicMock()
        exe = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            handler = ManagementCommandHandler(em.mqtt, em, pub, exe)
            handler.register_all()
            with (
                patch("subprocess.Popen") as mock_run,
                patch("builtins.open", MagicMock()),
                self.assertLogs("nibe.commands", level="INFO") as cm,
            ):
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                mock_run.return_value.communicate.return_value = ("", "")
                msg = MagicMock()
                msg.payload = b""
                handler._handle_run_tests(None, None, msg)
                handler._test_executor.shutdown(wait=True)
            self.assertTrue(
                any(
                    msg_.splitlines()[0].endswith("Run Test Suite triggered from HA (debug action)")
                    for msg_ in cm.output
                )
            )
        finally:
            exe.shutdown(wait=False)

    def test_run_tests_submits_the_handlers_own_test_running_event(self):
        """run_test_suite must be given this handler's own _test_running
        Event (not None/a different one) — that's the flag it clears in
        its `finally` block, and the same flag the duplicate-trigger guard
        above reads; passing the wrong object would leave _test_running
        stuck set forever after the run finishes."""
        import concurrent.futures

        from nibe_ha_integration import ManagementCommandHandler

        em = _make_em()
        pub = MagicMock()
        exe = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            handler = ManagementCommandHandler(em.mqtt, em, pub, exe)
            handler.register_all()
            from nibe_ha_integration import _get_ha_base_url, dismiss_ha, notify_ha

            with patch("nibe_ha_integration.run_test_suite") as mock_run_suite:
                msg = MagicMock()
                msg.payload = b""
                handler._handle_run_tests(None, None, msg)
                handler._test_executor.shutdown(wait=True)
            mock_run_suite.assert_called_once_with(
                em.mqtt,
                notify_ha,
                dismiss_ha,
                _get_ha_base_url,
                handler._test_running,
            )
        finally:
            exe.shutdown(wait=False)

    def test_run_tests_not_starved_by_saturated_mgmt_executor(self):
        """run_test_suite must run on its own dedicated executor, not
        mgmt_executor, so it can't be queued behind other blocking
        management-command handlers.

        Regression test: previously ManagementCommandHandler submitted
        run_test_suite to the same fixed-size mgmt_executor used by every
        other command handler. If both of mgmt_executor's workers were
        already occupied by long-running handlers, the test-suite job would
        sit queued indefinitely and never publish its 'running' MQTT state
        — reproduced here by fully saturating a 2-worker mgmt_executor with
        blocking handlers before triggering Run Test Suite on a *separate*
        test_executor, and asserting the test run still starts immediately.
        """
        import concurrent.futures
        import threading

        from nibe_ha_integration import ManagementCommandHandler

        em = _make_em()
        pub = MagicMock()
        mgmt_exe = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        test_exe = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            handler = ManagementCommandHandler(em.mqtt, em, pub, mgmt_exe, test_exe)
            handler.register_all()

            # Saturate both mgmt_executor workers with blocking jobs that
            # only release once the test below is done asserting.
            release = threading.Event()
            block_started = threading.Barrier(3, timeout=5)

            def _block():
                block_started.wait()
                release.wait(timeout=5)

            mgmt_exe.submit(_block)
            mgmt_exe.submit(_block)

            with (
                patch("subprocess.Popen") as mock_run,
                patch("builtins.open", MagicMock()),
                patch("nibe_ha_integration.dismiss_ha"),
            ):
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="2652 passed in 26m 0s", stderr=""
                )
                mock_run.return_value.communicate.return_value = ("2652 passed in 26m 0s", "")

                msg = MagicMock()
                msg.payload = b""
                handler._handle_run_tests(None, None, msg)

                # Wait for the two blocking mgmt jobs to actually be running,
                # proving mgmt_executor really is saturated at this point.
                block_started.wait(timeout=5)

                # The test run went to test_exe, not the saturated
                # mgmt_exe, so it must complete promptly regardless.
                test_exe.shutdown(wait=True, cancel_futures=False)
                mock_run.assert_called_once()
        finally:
            release.set()
            mgmt_exe.shutdown(wait=False)
            test_exe.shutdown(wait=False)


class TestHandleTestConnection(unittest.TestCase):
    """Tests for ManagementCommandHandler._handle_test_connection — the
    "Test API Connection" debug button that runs the independent ping+curl
    diagnostic (nibe_connectivity_check.py).

    notify_ha/dismiss_ha are patched in every test that reaches them —
    ManagementCommandHandler wires the *real* functions in (same as
    _handle_run_tests), and this project has already shipped one real bug
    where an unmocked notify_ha/dismiss_ha in a test fired a genuine HA
    notification in production (the nightly-test-suite incident) — do not
    repeat that here.
    """

    def _make_handler(self, ca_cert_path=None):
        import concurrent.futures

        from nibe_ha_integration import ManagementCommandHandler

        em = _make_em()
        em._api.base_url = "https://192.0.2.1:8443/api/v1/devices/0"
        em._api.auth = "Basic dGVzdA=="
        mqtt = MagicMock()
        publisher = MagicMock()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        handler = ManagementCommandHandler(
            mqtt,
            em,
            publisher,
            executor,
            ca_cert_path=ca_cert_path,
        )
        handler.register_all()
        return handler, em, mqtt, executor

    def _get_handler_callback(self, mqtt):
        from nibe_mqtt_publisher import MgmtTopic

        for call in mqtt.message_callback_add.call_args_list:
            if call.args[0] == MgmtTopic.TEST_CONNECTION_PRESS:
                return call.args[1]
        raise KeyError("No handler registered for TEST_CONNECTION_PRESS")

    def _trigger(self, handler, mqtt, executor):
        callback = self._get_handler_callback(mqtt)
        msg = MagicMock()
        msg.payload = b""
        callback(None, None, msg)
        executor.shutdown(wait=True)

    def test_publishes_running_state_immediately(self):
        from nibe_mqtt_publisher import MgmtTopic

        handler, em, mqtt, executor = self._make_handler()
        with (
            patch(
                "nibe_ha_integration.run_connectivity_check",
                return_value={
                    "ok": True,
                    "summary": "x",
                    "ping": {"ok": True, "summary": "p"},
                    "curl": {"ok": True, "summary": "c"},
                },
            ),
            patch("nibe_ha_integration.dismiss_ha"),
            self.assertLogs("nibe.commands", level="INFO") as cm,
        ):
            self._trigger(handler, mqtt, executor)
        states = [
            c.args[1]
            for c in em.mqtt.publish.call_args_list
            if c.args[0] == MgmtTopic.TEST_CONNECTION_STATE
        ]
        self.assertIn("running", states)
        running_call = next(
            c
            for c in em.mqtt.publish.call_args_list
            if c.args[0] == MgmtTopic.TEST_CONNECTION_STATE and c.args[1] == "running"
        )
        self.assertTrue(running_call.kwargs.get("retain"))
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Test API Connection triggered from HA (debug action)")
                for msg in cm.output
            )
        )

    def test_publishes_reachable_state_on_success(self):
        from nibe_mqtt_publisher import MgmtTopic

        handler, em, mqtt, executor = self._make_handler()
        with (
            patch(
                "nibe_ha_integration.run_connectivity_check",
                return_value={
                    "ok": True,
                    "summary": "x",
                    "ping": {"ok": True, "summary": "p"},
                    "curl": {"ok": True, "summary": "c"},
                },
            ),
            patch("nibe_ha_integration.dismiss_ha") as mock_dismiss,
        ):
            self._trigger(handler, mqtt, executor)
        states = [
            c.args[1]
            for c in em.mqtt.publish.call_args_list
            if c.args[0] == MgmtTopic.TEST_CONNECTION_STATE
        ]
        self.assertIn("reachable", states)
        mock_dismiss.assert_called_once_with(em.mqtt, "nibe_connectivity_check")
        reachable_call = next(
            c
            for c in em.mqtt.publish.call_args_list
            if c.args[0] == MgmtTopic.TEST_CONNECTION_STATE and c.args[1] == "reachable"
        )
        self.assertTrue(reachable_call.kwargs.get("retain"))
        attrs_call = next(
            c
            for c in em.mqtt.publish.call_args_list
            if c.args[0] == MgmtTopic.TEST_CONNECTION_ATTRS
        )
        self.assertTrue(attrs_call.kwargs.get("retain"))
        import json as _json

        payload = _json.loads(attrs_call.args[1])
        self.assertEqual(payload["status"], "reachable")
        self.assertEqual(payload["summary"], "x")
        self.assertIn("timestamp", payload)

    def test_publishes_unreachable_state_and_notifies_on_failure(self):
        from nibe_mqtt_publisher import MgmtTopic

        handler, em, mqtt, executor = self._make_handler()
        with (
            patch(
                "nibe_ha_integration.run_connectivity_check",
                return_value={
                    "ok": False,
                    "summary": "Unreachable — network problem.",
                    "ping": {"ok": False, "summary": "no ping reply"},
                    "curl": {"ok": False, "summary": "connection refused"},
                },
            ),
            patch("nibe_ha_integration.notify_ha") as mock_notify,
        ):
            self._trigger(handler, mqtt, executor)
        states = [
            c.args[1]
            for c in em.mqtt.publish.call_args_list
            if c.args[0] == MgmtTopic.TEST_CONNECTION_STATE
        ]
        self.assertIn("unreachable", states)
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args.args[0], em.mqtt)
        self.assertEqual(mock_notify.call_args.kwargs["notification_id"], "nibe_connectivity_check")
        self.assertEqual(mock_notify.call_args.kwargs["title"], "Nibe Bridge: Connectivity Check")
        self.assertIn("Unreachable", mock_notify.call_args.kwargs["message"])

    def test_attrs_include_ping_and_curl_sub_results(self):
        import json as _json

        from nibe_mqtt_publisher import MgmtTopic

        handler, em, mqtt, executor = self._make_handler()
        with (
            patch(
                "nibe_ha_integration.run_connectivity_check",
                return_value={
                    "ok": True,
                    "summary": "x",
                    "ping": {"ok": True, "summary": "ping detail"},
                    "curl": {"ok": True, "summary": "curl detail"},
                },
            ),
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            self._trigger(handler, mqtt, executor)
        attrs_calls = [
            c
            for c in em.mqtt.publish.call_args_list
            if c.args[0] == MgmtTopic.TEST_CONNECTION_ATTRS
        ]
        self.assertTrue(attrs_calls)
        payload = _json.loads(attrs_calls[-1].args[1])
        self.assertEqual(payload["ping"]["summary"], "ping detail")
        self.assertEqual(payload["curl"]["summary"], "curl detail")

    def test_uses_real_host_parsed_from_base_url(self):
        handler, em, mqtt, executor = self._make_handler()
        with (
            patch(
                "nibe_ha_integration.run_connectivity_check",
                return_value={
                    "ok": True,
                    "summary": "x",
                    "ping": {"ok": True, "summary": "p"},
                    "curl": {"ok": True, "summary": "c"},
                },
            ) as mock_check,
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            self._trigger(handler, mqtt, executor)
        mock_check.assert_called_once_with(
            "192.0.2.1",
            em._api.base_url,
            None,
            em._api.auth,
        )

    def test_uses_configured_ca_cert_path(self):
        """The handler must forward the ca_cert_path it was constructed
        with — not always None — or a user with verified TLS configured
        would get a check that silently ignores their CA cert."""
        handler, em, mqtt, executor = self._make_handler(ca_cert_path="/ssl/nibe-ca.pem")
        with (
            patch(
                "nibe_ha_integration.run_connectivity_check",
                return_value={
                    "ok": True,
                    "summary": "x",
                    "ping": {"ok": True, "summary": "p"},
                    "curl": {"ok": True, "summary": "c"},
                },
            ) as mock_check,
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            self._trigger(handler, mqtt, executor)
        mock_check.assert_called_once_with(
            "192.0.2.1",
            em._api.base_url,
            "/ssl/nibe-ca.pem",
            em._api.auth,
        )

    def test_uses_real_auth_header(self):
        handler, em, mqtt, executor = self._make_handler()
        em._api.auth = "Basic c3BlY2lhbA=="
        with (
            patch(
                "nibe_ha_integration.run_connectivity_check",
                return_value={
                    "ok": True,
                    "summary": "x",
                    "ping": {"ok": True, "summary": "p"},
                    "curl": {"ok": True, "summary": "c"},
                },
            ) as mock_check,
            patch("nibe_ha_integration.dismiss_ha"),
        ):
            self._trigger(handler, mqtt, executor)
        self.assertEqual(mock_check.call_args.args[3], "Basic c3BlY2lhbA==")


class TestRegistryWatcherEventHandling(unittest.TestCase):
    def _make_watcher(self):
        import threading

        from nibe_ha_integration import HAEntityRegistryWatcher

        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._registry_map_lock = threading.Lock()
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
        w._unique_id_map["nibe_6983"] = "number.nibe_6983_power"
        self.assertEqual(w.entity_id_for(6983), "number.nibe_6983_power")

    def test_create_event_without_unique_id_does_not_crash(self):
        w = self._make_watcher()
        with patch.object(w, "refresh_registry"):
            w._handle_event({"data": {"action": "create", "entity_id": "number.nibe_6983_power"}})
            if w._refresh_timer is not None:
                w._refresh_timer.cancel()
        self.assertNotIn("nibe_6983", w._unique_id_map)

    def test_create_event_with_top_level_unique_id(self):
        w = self._make_watcher()
        w._handle_event(
            {
                "data": {
                    "action": "create",
                    "entity_id": "number.nibe_6983_power",
                    "unique_id": "nibe_6983",
                }
            }
        )
        self.assertEqual(w._unique_id_map.get("nibe_6983"), "number.nibe_6983_power")

    def test_create_event_with_nested_unique_id(self):
        w = self._make_watcher()
        w._handle_event(
            {
                "data": {
                    "action": "create",
                    "entity_id": "number.nibe_6983_power",
                    "config": {"unique_id": "nibe_6983"},
                }
            }
        )
        self.assertEqual(w._unique_id_map.get("nibe_6983"), "number.nibe_6983_power")

    def test_update_event_populates_map(self):
        w = self._make_watcher()
        w._handle_event(
            {
                "data": {
                    "action": "update",
                    "entity_id": "number.nibe_6983_power",
                    "unique_id": "nibe_6983",
                }
            }
        )
        self.assertEqual(w._unique_id_map.get("nibe_6983"), "number.nibe_6983_power")

    def test_remove_event_clears_entry(self):
        w = self._make_watcher()
        w._unique_id_map["nibe_6983"] = "number.nibe_6983_power"
        w._handle_event(
            {
                "data": {
                    "action": "remove",
                    "unique_id": "nibe_6983",
                    "entity_id": "number.nibe_6983_power",
                }
            }
        )
        self.assertNotIn("nibe_6983", w._unique_id_map)

    def test_unknown_action_does_not_crash(self):
        w = self._make_watcher()
        w._handle_event({"data": {"action": "something_new", "entity_id": "number.nibe_test"}})

    def test_remove_event_nested_config_unique_id_fallback(self):
        """'remove' events must also use the nested config.unique_id
        fallback when the top-level key is absent — every existing remove
        test uses the top-level key directly, never exercising this path
        (unlike 'create'/'update', which have their own dedicated
        nested-fallback tests)."""
        w = self._make_watcher()
        w._unique_id_map["nibe_nested"] = "sensor.nibe_nested"
        w._handle_event(
            {
                "data": {
                    "action": "remove",
                    "config": {"unique_id": "nibe_nested"},
                }
            }
        )
        self.assertNotIn("nibe_nested", w._unique_id_map)

    def test_remove_event_unknown_uid_does_not_raise(self):
        """A 'remove' event for a uid that was never in _unique_id_map
        (e.g. a non-Nibe entity, or a map that was already cleared) must
        not raise — pop() must use the safe two-arg form."""
        w = self._make_watcher()
        w._handle_event(
            {
                "data": {
                    "action": "remove",
                    "unique_id": "never_seen_before",
                }
            }
        )  # must not raise

    def test_event_with_no_data_key_does_not_crash(self):
        """An event dict entirely missing the 'data' key (not just an
        empty dict under it) must fall through gracefully — action ends up
        None, matching no branch — rather than crashing on data.get(...)
        against a None default."""
        w = self._make_watcher()
        w._handle_event({})  # no 'data' key at all — must not raise


# ===========================================================================
# 36. Unit overrides
# ===========================================================================


class TestWsAuthenticate(unittest.TestCase):
    """_ws_authenticate(): the three-step WebSocket auth handshake against
    the HA Supervisor — wait auth_required, send credentials, confirm
    auth_ok. Static method, no watcher instance needed."""

    def _import(self):
        from nibe_ha_integration import HAEntityRegistryWatcher

        return HAEntityRegistryWatcher._ws_authenticate

    def _ws(self, recv_sequence):
        ws = MagicMock()
        ws.recv.side_effect = [json.dumps(m) for m in recv_sequence]
        return ws

    def test_success_sends_real_access_token(self):
        ws_authenticate = self._import()
        ws = self._ws([{"type": "auth_required"}, {"type": "auth_ok"}])
        ws_authenticate(ws, "real-supervisor-token")
        sent = json.loads(ws.send.call_args.args[0])
        self.assertEqual(sent, {"type": "auth", "access_token": "real-supervisor-token"})

    def test_wrong_greeting_type_closes_and_raises_with_the_real_type(self):
        ws_authenticate = self._import()
        ws = self._ws([{"type": "auth_invalid"}])
        with self.assertRaises(RuntimeError) as cm:
            ws_authenticate(ws, "tok")
        ws.close.assert_called_once()
        self.assertEqual(str(cm.exception), "Unexpected WS greeting type: auth_invalid")

    def test_wrong_greeting_type_missing_key_reports_unknown(self):
        ws_authenticate = self._import()
        ws = self._ws([{}])  # no 'type' key at all
        with self.assertRaises(RuntimeError) as cm:
            ws_authenticate(ws, "tok")
        self.assertEqual(str(cm.exception), "Unexpected WS greeting type: unknown")

    def test_auth_rejected_closes_and_raises_with_the_real_type(self):
        ws_authenticate = self._import()
        ws = self._ws([{"type": "auth_required"}, {"type": "auth_invalid"}])
        with self.assertRaises(RuntimeError) as cm:
            ws_authenticate(ws, "tok")
        ws.close.assert_called_once()
        self.assertEqual(
            str(cm.exception),
            "WS auth failed (response type: auth_invalid)",
        )

    def test_auth_rejected_missing_type_reports_unknown(self):
        ws_authenticate = self._import()
        ws = self._ws([{"type": "auth_required"}, {}])  # no 'type' key
        with self.assertRaises(RuntimeError) as cm:
            ws_authenticate(ws, "tok")
        self.assertEqual(
            str(cm.exception),
            "WS auth failed (response type: unknown)",
        )


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
        w._registry_map_lock = threading.Lock()
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        return w

    def test_single_call_starts_one_timer(self):
        w = self._make_watcher()
        with (
            patch("threading.Timer") as mock_timer,
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            w._schedule_refresh_registry()
        mock_timer.assert_called_once_with(w._REFRESH_DEBOUNCE_S, w.refresh_registry)
        mock_timer.return_value.start.assert_called_once()
        self.assertEqual(
            mock_timer.return_value.name,
            "nibe_registry_refresh_debounce",
        )
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Scheduling registry refresh (debounce)")
                for msg in cm.output
            )
        )

    def test_burst_of_many_calls_coalesces_to_one_pending_timer(self):
        """The core regression case: N calls in a burst (simulating N
        entity_registry_updated create events fired for a large batch of
        newly enabled points) must cancel every prior timer, leaving
        exactly one live at the end — not N independent ones."""
        w = self._make_watcher()
        timers = [MagicMock() for _ in range(50)]
        with patch("threading.Timer", side_effect=timers) as mock_timer:
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
        exercising the real Timer/thread integration.

        The debounce window and join timeout below are deliberately far
        more generous than the ~65 cheap Timer cancel/create calls in the
        loop should ever need: a 0.05s window (a prior version of this
        test) genuinely raced the loop itself on a loaded machine — this
        test failed for real on the ODROID during its own nightly xdist
        run, where CPU contention from the rest of the suite running in
        parallel was enough to let the debounce timer fire mid-burst and
        call refresh_registry() more than once. 1.0s gives ~20x the
        margin, and the 5s join timeout comfortably covers the 1.0s
        debounce plus the same class of scheduling delay. If this ever
        flakes again, the fix is a wider margin here, not fewer events."""
        w = self._make_watcher()
        with (
            patch.object(w, "refresh_registry") as mock_refresh,
            patch.object(type(w), "_REFRESH_DEBOUNCE_S", 1.0),
        ):
            for i in range(65):
                w._handle_event({"data": {"action": "create", "entity_id": f"sensor.nibe_{i}"}})
            if w._refresh_timer is not None:
                w._refresh_timer.join(timeout=5)
        mock_refresh.assert_called_once()

    def test_update_event_missing_unique_id_also_debounces(self):
        """The 'update' branch has the identical missing-unique_id
        fallback and must use the same coalescing, not its own
        independent timer."""
        w = self._make_watcher()
        with patch("threading.Timer") as mock_timer:
            w._handle_event({"data": {"action": "update", "entity_id": "sensor.nibe_1"}})
            w._handle_event({"data": {"action": "update", "entity_id": "sensor.nibe_2"}})
        self.assertEqual(mock_timer.call_count, 2)
        mock_timer.return_value.cancel.assert_called_once()  # first one cancelled

    def test_timer_is_daemon_and_named(self):
        w = self._make_watcher()
        with patch("threading.Timer") as mock_timer:
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
        w._registry_map_lock = threading.Lock()
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

    def _em_with_point(self, point_id, is_dynamic=False, display_title="Test point"):
        em = _make_em()
        em.all_points_by_id[point_id] = {
            "display_title": display_title,
            "is_dynamic": is_dynamic,
        }
        return em

    # -- resolution failure --------------------------------------------------

    def test_enabled_unresolvable_entity_id_does_nothing(self):
        """If the entity_id can't be mapped back to a point_id at all,
        neither method should touch the bridge state or notify anyone."""
        em = _make_em()
        w = self._make_watcher(em)
        w._on_entity_enabled("switch.totally_unknown")
        self.assertEqual(em.mqtt.publish.call_count, 0)

    def test_disabled_unresolvable_entity_id_does_nothing(self):
        em = _make_em()
        w = self._make_watcher(em)
        w._on_entity_disabled("switch.totally_unknown")
        self.assertEqual(em.mqtt.publish.call_count, 0)

    # -- registry-map snapshot isolation (post-write race fix) ---------------

    def test_enabled_passes_a_snapshot_not_the_live_map(self):
        """Regression test: _on_entity_enabled must pass an independent copy
        of _unique_id_map to resolve_point_from_entity_id, not the live dict
        object — the live dict can be concurrently reassigned (on reconnect)
        or mutated (by a registry refresh on another thread) while resolution
        is in progress, which risked either lost updates or a "dictionary
        changed size during iteration" crash."""
        em = _make_em()
        w = self._make_watcher(em)
        w._unique_id_map = {"nibe_100": "switch.nibe_100"}
        with patch.object(em, "resolve_point_from_entity_id", return_value=None) as mock_resolve:
            w._on_entity_enabled("switch.nibe_100")
        mock_resolve.assert_called_once()
        passed_map = mock_resolve.call_args.kwargs["unique_id_map"]
        self.assertEqual(passed_map, w._unique_id_map)
        self.assertIsNot(passed_map, w._unique_id_map)

    def test_disabled_passes_a_snapshot_not_the_live_map(self):
        em = _make_em()
        w = self._make_watcher(em)
        w._unique_id_map = {"nibe_100": "switch.nibe_100"}
        with patch.object(em, "resolve_point_from_entity_id", return_value=None) as mock_resolve:
            w._on_entity_disabled("switch.nibe_100")
        mock_resolve.assert_called_once()
        passed_map = mock_resolve.call_args.kwargs["unique_id_map"]
        self.assertEqual(passed_map, w._unique_id_map)
        self.assertIsNot(passed_map, w._unique_id_map)

    # -- _on_entity_disabled: static point (the silent-mirror path) ----------

    def test_disabled_static_point_calls_disable_entity(self):
        em = self._em_with_point(100, is_dynamic=False)
        em.active_entities_by_id[100] = {"entity_type": "switch", "entity_id": "foo"}
        em.mqtt_enabled_points.add(100)
        w = self._make_watcher(em)
        with (
            patch.object(em, "disable_entity") as mock_disable,
            patch("nibe_ha_integration._publish_stats"),
        ):
            w._on_entity_disabled("switch.nibe_100")
        mock_disable.assert_called_once_with(100)

    def test_disabled_point_missing_is_dynamic_key_treated_as_static(self):
        """A point dict entirely missing the 'is_dynamic' key must default
        to False (mirrored/silent path) — not True (revert/notify path)."""
        em = _make_em()
        em.all_points_by_id[100] = {"display_title": "Test point"}  # no 'is_dynamic'
        w = self._make_watcher(em)
        with (
            patch.object(em, "disable_entity") as mock_disable,
            patch("nibe_ha_integration._publish_stats"),
            patch("nibe_ha_integration.notify_ha") as mock_notify,
        ):
            w._on_entity_disabled("switch.nibe_100")
        mock_disable.assert_called_once_with(100)
        mock_notify.assert_not_called()

    def test_disabled_logs_debug_with_real_entity_and_point_id(self):
        em = self._em_with_point(100, is_dynamic=False)
        w = self._make_watcher(em)
        with (
            patch.object(em, "disable_entity"),
            patch("nibe_ha_integration._publish_stats"),
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            w._on_entity_disabled("switch.nibe_100")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Entity switch.nibe_100 (point 100) disabled via HA — mirroring disable"
                )
                for msg in cm.output
            )
        )

    def test_disabled_static_point_logs_mirrored_info_verbatim(self):
        em = self._em_with_point(100, is_dynamic=False)
        w = self._make_watcher(em)
        with (
            patch.object(em, "disable_entity"),
            patch("nibe_ha_integration._publish_stats"),
            self.assertLogs("nibe.registry", level="INFO") as cm,
        ):
            w._on_entity_disabled("switch.nibe_100")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Mirrored HA-side disable for point 100 in bridge")
                for msg in cm.output
            )
        )

    def test_disabled_dynamic_point_logs_republished_info_verbatim(self):
        em = self._em_with_point(50827, is_dynamic=True)
        w = self._make_watcher(em)
        with (
            patch("nibe_ha_integration.notify_ha"),
            self.assertLogs("nibe.registry", level="INFO") as cm,
        ):
            w._on_entity_disabled("sensor.nibe_50827")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Republished discovery config for point 50827 to reverse HA-side disable"
                )
                for msg in cm.output
            )
        )

    def test_disabled_static_point_sends_no_notification(self):
        """The documented 'no confusing notification for an intentional
        disable' behavior — must not call notify_ha at all for a plain
        static-point disable."""
        em = self._em_with_point(100, is_dynamic=False)
        w = self._make_watcher(em)
        with (
            patch.object(em, "disable_entity"),
            patch("nibe_ha_integration._publish_stats"),
            patch("nibe_ha_integration.notify_ha") as mock_notify,
        ):
            w._on_entity_disabled("switch.nibe_100")
        mock_notify.assert_not_called()

    def test_disabled_static_point_publishes_stats(self):
        em = self._em_with_point(100, is_dynamic=False)
        w = self._make_watcher(em)
        with (
            patch.object(em, "disable_entity"),
            patch("nibe_ha_integration._publish_stats") as mock_stats,
        ):
            w._on_entity_disabled("switch.nibe_100")
        mock_stats.assert_called_once()

    # -- _on_entity_disabled: dynamic point (the revert-and-notify path) -----

    def test_disabled_dynamic_point_does_not_call_disable_entity(self):
        """A dynamic point's HA-side disable must be REVERTED, not mirrored
        — disable_entity must never be called for it."""
        em = self._em_with_point(50827, is_dynamic=True)
        w = self._make_watcher(em)
        with (
            patch.object(em, "disable_entity") as mock_disable,
            patch("nibe_ha_integration.notify_ha"),
        ):
            w._on_entity_disabled("sensor.nibe_50827")
        mock_disable.assert_not_called()

    def test_disabled_dynamic_point_republishes_discovery_config(self):
        em = self._em_with_point(50827, is_dynamic=True)
        pub = MagicMock()
        w = self._make_watcher(em, pub=pub)
        with patch("nibe_ha_integration.notify_ha"):
            w._on_entity_disabled("sensor.nibe_50827")
        pub.publish_entity_discovery.assert_called_once_with(
            em.all_points_by_id[50827],
            em.bulk_data,
        )

    def test_disabled_dynamic_point_sends_notification(self):
        """Unlike the static case, a dynamic point's disable attempt DOES
        notify the user — explaining why it reappeared."""
        em = self._em_with_point(50827, is_dynamic=True)
        w = self._make_watcher(em)
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            w._on_entity_disabled("sensor.nibe_50827")
        mock_notify.assert_called_once()

    # -- _on_entity_enabled ---------------------------------------------------

    def test_enabled_not_yet_in_mqtt_enabled_points_calls_enable_entity(self):
        em = self._em_with_point(100)
        w = self._make_watcher(em)
        with (
            patch.object(em, "enable_entity") as mock_enable,
            patch("nibe_ha_integration._publish_stats"),
            patch("nibe_ha_integration.notify_ha"),
        ):
            w._on_entity_enabled("switch.nibe_100")
        mock_enable.assert_called_once_with(100)

    def test_enabled_logs_info_with_real_entity_and_point_id(self):
        em = self._em_with_point(100)
        w = self._make_watcher(em)
        with (
            patch.object(em, "enable_entity"),
            patch("nibe_ha_integration._publish_stats"),
            patch("nibe_ha_integration.notify_ha"),
            self.assertLogs("nibe.registry", level="INFO") as cm,
        ):
            w._on_entity_enabled("switch.nibe_100")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Entity switch.nibe_100 (point 100) re-enabled via HA — republishing discovery"
                )
                for msg in cm.output
            )
        )

    def test_enabled_build_disable_notification_called_with_real_point_id(self):
        em = self._em_with_point(100)
        w = self._make_watcher(em)
        with (
            patch.object(em, "enable_entity"),
            patch("nibe_ha_integration._publish_stats"),
            patch.object(
                em, "build_disable_notification", wraps=em.build_disable_notification
            ) as mock_build,
            patch("nibe_ha_integration.notify_ha"),
        ):
            w._on_entity_enabled("switch.nibe_100")
        mock_build.assert_called_once_with(100, "switch.nibe_100", action="re-enabled")

    def test_enabled_already_in_mqtt_enabled_points_republishes_discovery_instead(self):
        """If the bridge already considers the point enabled (e.g. HA's
        registry briefly lagged), don't re-run the full enable_entity path
        — just republish the discovery config to be safe."""
        em = self._em_with_point(100)
        em.mqtt_enabled_points.add(100)
        pub = MagicMock()
        w = self._make_watcher(em, pub=pub)
        with (
            patch.object(em, "enable_entity") as mock_enable,
            patch("nibe_ha_integration.notify_ha"),
        ):
            w._on_entity_enabled("switch.nibe_100")
        mock_enable.assert_not_called()
        pub.publish_entity_discovery.assert_called_once_with(
            em.all_points_by_id[100],
            em.bulk_data,
        )

    def test_enabled_publishes_stats_with_real_em_and_pub(self):
        em = self._em_with_point(100)
        pub = MagicMock()
        w = self._make_watcher(em, pub=pub)
        with (
            patch.object(em, "enable_entity"),
            patch("nibe_ha_integration._publish_stats") as mock_stats,
            patch("nibe_ha_integration.notify_ha"),
        ):
            w._on_entity_enabled("switch.nibe_100")
        mock_stats.assert_called_once_with(em, pub)

    def test_enabled_dismisses_the_disable_notification(self):
        """Re-enabling must clear whatever disable notification was shown
        earlier for the same entity_id — uses the same notif_id derivation
        (dots/hyphens sanitised) as build_disable_notification."""
        em = self._em_with_point(100)
        w = self._make_watcher(em)
        with (
            patch.object(em, "enable_entity"),
            patch("nibe_ha_integration._publish_stats"),
            patch("nibe_ha_integration.dismiss_ha") as mock_dismiss,
            patch("nibe_ha_integration.notify_ha"),
        ):
            w._on_entity_enabled("switch.nibe_100")
        mock_dismiss.assert_called_once_with(em.mqtt, "nibe_ha_disable_switch_nibe_100")

    def test_enabled_sends_reenabled_notification(self):
        em = self._em_with_point(100)
        w = self._make_watcher(em)
        with (
            patch.object(em, "enable_entity"),
            patch("nibe_ha_integration._publish_stats"),
            patch("nibe_ha_integration.notify_ha") as mock_notify,
        ):
            w._on_entity_enabled("switch.nibe_100")
        mock_notify.assert_called_once()
        self.assertIn("re-enabled", mock_notify.call_args.kwargs["title"].lower())

    def test_enabled_notify_ha_receives_the_real_mqtt_and_message_and_notif_id(self):
        """notify_ha must be called with the watcher's real mqtt client, a
        real (non-None) message body, and the exact notif_id derived from
        build_disable_notification — not a wrong/missing argument."""
        em = self._em_with_point(100)
        w = self._make_watcher(em)
        with (
            patch.object(em, "enable_entity"),
            patch("nibe_ha_integration._publish_stats"),
            patch("nibe_ha_integration.notify_ha") as mock_notify,
        ):
            w._on_entity_enabled("switch.nibe_100")
        self.assertIs(mock_notify.call_args.args[0], em.mqtt)
        self.assertTrue(mock_notify.call_args.kwargs["message"])
        self.assertEqual(
            mock_notify.call_args.kwargs["notification_id"],
            "nibe_ha_disable_switch_nibe_100",
        )

    def test_reenable_dismisses_the_exact_id_the_real_disable_notification_used(self):
        """build_disable_notification() (called when the entity is
        disabled) and _on_entity_enabled() (called when it's later
        re-enabled) used to compute the notification_id via two separately
        duplicated inline string constructions — byte-identical by luck,
        with nothing enforcing that agreement. Both now delegate to the
        same EntityManager.ha_disable_notif_id() helper; this proves the
        agreement by chaining the two real call sites together, rather
        than asserting each one against a hand-typed literal that could
        drift out of sync with the other independently."""
        em = self._em_with_point(100)
        w = self._make_watcher(em)

        _title, _message, created_notif_id = em.build_disable_notification(
            100,
            "switch.nibe_100",
            action="disabled",
        )

        with (
            patch.object(em, "enable_entity"),
            patch("nibe_ha_integration._publish_stats"),
            patch("nibe_ha_integration.dismiss_ha") as mock_dismiss,
            patch("nibe_ha_integration.notify_ha"),
        ):
            w._on_entity_enabled("switch.nibe_100")

        mock_dismiss.assert_called_once_with(em.mqtt, created_notif_id)


# ===========================================================================
# 55. update_alarm_state — alarm polling and HA notification
# ===========================================================================


class TestPublishStats(unittest.TestCase):
    """_publish_stats: the dedup-log getattr default and its exact debug text."""

    def _em(self):
        em = _make_em()
        em._stats_type_counts = {}
        em._stats_category_counts = {}
        em._stats_writable_count = 0
        em._write_total = 0
        em._write_success = 0
        em._write_failed = 0
        return em

    def test_missing_last_stats_key_attr_does_not_raise(self):
        """getattr's default must be None, not omitted — an EntityManager
        that has never published stats before (no _last_stats_key attr
        yet) must not raise AttributeError on the first call."""
        from nibe_ha_integration import _publish_stats

        em = self._em()
        if hasattr(em, "_last_stats_key"):
            del em._last_stats_key
        pub = MagicMock()
        _publish_stats(em, pub)  # must not raise

    def test_dedup_debug_log_has_exact_text(self):
        from nibe_ha_integration import _publish_stats

        em = self._em()
        pub = MagicMock()
        with self.assertLogs("nibe.stats", level="DEBUG") as cm:
            _publish_stats(em, pub)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Stats: MQTT=0, Active=0, Total=0")
                for msg in cm.output
            )
        )


class TestUpdateAlarmState(unittest.TestCase):
    """Fetches /notifications and updates the Active Alarms sensor plus an
    edge-triggered HA persistent notification. Zero coverage before this.
    The edge-trigger logic (_alarm_notification_active) exists specifically
    to avoid re-notifying every poll cycle while an alarm remains active —
    getting the 0->N / N->0 transition logic wrong means either notification
    spam on every poll, or a notification that never clears after the
    alarm resolves."""

    def _alarm(
        self,
        alarm_id=1,
        header="High pressure alarm",
        description="",
        severity="Warning",
        time="2026-06-21T10:00:00",
        equip_name="",
    ):
        return {
            "alarmId": alarm_id,
            "header": header,
            "description": description,
            "severity": severity,
            "time": time,
            "equipName": equip_name,
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
        with self.assertLogs("nibe.stats", level="DEBUG") as cm:
            update_alarm_state(em, pub)
        pub.publish_alarm_state.assert_not_called()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Alarm poll skipped — fetch_notifications returned None (API error)"
                )
                for msg in cm.output
            )
        )

    def test_alarm_count_change_logged_verbatim(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._last_alarm_count = 0
        em._alarm_notification_active = True  # suppress notify_ha; not under test here
        em._api.fetch_notifications.return_value = [self._alarm(), self._alarm(alarm_id=2)]
        with self.assertLogs("nibe.stats", level="DEBUG") as cm:
            update_alarm_state(em, MagicMock())
        self.assertTrue(
            any(msg.splitlines()[0].endswith("Alarm poll: 2 active alarm(s)") for msg in cm.output)
        )

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
            self._alarm(alarm_id=42, header="Sensor fault", severity="Error"),
        ]
        pub = MagicMock()
        update_alarm_state(em, pub)
        count, clean = pub.publish_alarm_state.call_args.args
        self.assertEqual(count, 1)
        self.assertEqual(clean[0]["alarmId"], 42)
        self.assertEqual(clean[0]["header"], "Sensor fault")
        self.assertEqual(clean[0]["severity"], "Error")

    def test_clean_alarms_missing_fields_default_safely(self):
        """A real alarm dict missing optional fields (e.g. no equipName)
        must not crash — defaults to empty string, not KeyError."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True  # suppress notify_ha; not under test here
        em._api.fetch_notifications.return_value = [{"alarmId": 1, "header": "X"}]
        pub = MagicMock()
        update_alarm_state(em, pub)
        _, clean = pub.publish_alarm_state.call_args.args
        self.assertEqual(clean[0]["description"], "")
        self.assertEqual(clean[0]["equipName"], "")
        self.assertEqual(clean[0]["time"], "")

    def test_clean_alarms_missing_header_defaults_to_empty_string(self):
        """The 'header' field defaults to '' when the key is entirely
        absent — every other test always sets 'header', so this specific
        default was never exercised."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True  # suppress notify_ha; not under test here
        em._api.fetch_notifications.return_value = [{"alarmId": 1}]  # no 'header' key
        pub = MagicMock()
        update_alarm_state(em, pub)
        _, clean = pub.publish_alarm_state.call_args.args
        self.assertEqual(clean[0]["header"], "")

    def test_clean_alarms_real_time_value_propagates(self):
        """A real non-empty 'time' value must actually appear in the
        cleaned alarm dict — existing coverage only checks the absent-key
        default ('' when missing), never that a present value survives."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True
        em._api.fetch_notifications.return_value = [
            {"alarmId": 1, "header": "X", "time": "2026-08-13T12:00:00Z"},
        ]
        pub = MagicMock()
        update_alarm_state(em, pub)
        _, clean = pub.publish_alarm_state.call_args.args
        self.assertEqual(clean[0]["time"], "2026-08-13T12:00:00Z")

    # -- edge-triggered notification: 0 -> N transition ------------------------

    def test_first_alarm_triggers_notification(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = False
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        mock_notify.assert_called_once()
        self.assertTrue(em._alarm_notification_active)

    def test_notification_id_is_fixed_active_alarms(self):
        """notification_id must be the fixed 'nibe_active_alarms' string so
        dismiss_ha (using the same id) can clear exactly this notification."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertEqual(mock_notify.call_args.kwargs["notification_id"], "nibe_active_alarms")

    def test_alarm_continuing_does_not_re_notify(self):
        """The whole point of the edge-trigger flag: a second poll cycle
        with the alarm still active must NOT fire another notification."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True  # already notified previously
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        mock_notify.assert_not_called()

    def test_alarm_count_increasing_while_active_does_not_re_notify(self):
        """Even if a SECOND distinct alarm appears while one is already
        active, the flag still suppresses re-notification — by design,
        not a bug, since the user already has an active notification."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True
        em._api.fetch_notifications.return_value = [
            self._alarm(alarm_id=1),
            self._alarm(alarm_id=2),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        mock_notify.assert_not_called()

    # -- edge-triggered notification: N -> 0 transition ------------------------

    def test_alarm_cleared_dismisses_notification(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = True
        em._api.fetch_notifications.return_value = []
        with patch("nibe_ha_integration.dismiss_ha") as mock_dismiss:
            update_alarm_state(em, MagicMock())
        mock_dismiss.assert_called_once_with(em.mqtt, "nibe_active_alarms")
        self.assertIs(em._alarm_notification_active, False)

    def test_already_inactive_no_alarms_does_not_dismiss_again(self):
        """If there was no active notification to begin with, a zero-alarm
        poll must not call dismiss_ha redundantly."""
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = False
        em._api.fetch_notifications.return_value = []
        with (
            patch("nibe_ha_integration.dismiss_ha") as mock_dismiss,
            patch("nibe_ha_integration.notify_ha") as mock_notify,
        ):
            update_alarm_state(em, MagicMock())
        mock_dismiss.assert_not_called()
        # Pins `alarm_count > 0` (not `>= 0`) — at zero alarms with no
        # active notification, neither the trigger nor the clear branch
        # must fire.
        mock_notify.assert_not_called()

    def test_notify_ha_receives_the_real_mqtt_client(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = False
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIs(mock_notify.call_args.args[0], em.mqtt)

    # NOTE: the alarm-line builder's `a.get("header", "Unknown alarm")`
    # default is unreachable in practice — clean_alarms always sets the
    # 'header' key (defaulting to '' via its own a.get("header", "")), so
    # this second-level default can never fire on real cleaned-alarm dicts.
    # The corresponding mutmut survivors for that literal are equivalent
    # mutants, not real gaps.

    def test_device_model_defaults_to_s_series_when_absent(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._alarm_notification_active = False
        em.device_info = {}  # no 'model' key
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn("S-series", mock_notify.call_args.kwargs["title"])

    # -- message composition ---------------------------------------------------

    def test_message_includes_device_model_from_device_info(self):
        update_alarm_state = self._import()
        em = _make_em()
        em.device_info = {"model": "S2125-12"}
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn("S2125-12", mock_notify.call_args.kwargs["title"])
        self.assertIn("S2125-12", mock_notify.call_args.kwargs["message"])

    def test_message_falls_back_to_s_series_when_model_unknown(self):
        update_alarm_state = self._import()
        em = _make_em()
        em.device_info = {}  # no 'model' key
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn("S-series", mock_notify.call_args.kwargs["title"])

    def test_device_model_default_is_exactly_s_series(self):
        """Pins the exact default string (not just a substring match) — a
        mutated default like 'XXS-seriesXX' would still satisfy a bare
        assertIn('S-series', ...) check since 'S-series' is itself a
        substring of the mutated value."""
        update_alarm_state = self._import()
        em = _make_em()
        em.device_info = {}  # no 'model' key
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn("Nibe S-series:", mock_notify.call_args.kwargs["title"])
        self.assertIn("on the Nibe S-series:", mock_notify.call_args.kwargs["message"])

    def test_message_includes_equipment_and_severity(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header="Pump fault", equip_name="GP1", severity="Critical"),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs["message"]
        self.assertIn("Pump fault", msg)
        self.assertIn("Equipment: GP1", msg)
        self.assertIn("Severity: Critical", msg)

    def test_message_omits_equipment_when_blank(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header="Generic fault", equip_name=""),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs["message"]
        self.assertNotIn("Equipment:", msg)

    def test_message_description_omitted_when_identical_to_header(self):
        """The dedup check: if description == header, must not repeat it
        verbatim in the message — only appended when it adds information."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header="High pressure alarm", description="High pressure alarm"),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs["message"]
        # Header appears once via the line; description must not duplicate it.
        self.assertEqual(msg.count("High pressure alarm"), 1)

    def test_message_description_included_when_distinct_from_header(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header="High pressure alarm", description="Pressure exceeded 28 bar"),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs["message"]
        self.assertIn("Pressure exceeded 28 bar", msg)

    def test_message_omits_severity_when_absent(self):
        """Firmware alarms without a severity field must not render a
        dangling 'Severity: ' fragment in the notification message."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header="Sensor fault", severity=""),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs["message"]
        self.assertNotIn("Severity:", msg)

    def test_message_parts_joined_with_em_dash_separator(self):
        """The header/equipment/severity/description parts of one alarm's
        line must be joined with ' — ' (em dash) exactly."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(header="Pump fault", equip_name="GP1", severity="Critical"),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs["message"]
        self.assertIn("Pump fault — Equipment: GP1 — Severity: Critical", msg)

    def test_alarm_lines_joined_with_real_newline(self):
        """Multiple alarm bullet lines must be joined with a real newline,
        not a literal placeholder — each alarm renders on its own line."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(alarm_id=1, header="Alarm A"),
            self._alarm(alarm_id=2, header="Alarm B"),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs["message"]
        self.assertIn("• Alarm A — Severity: Warning\n• Alarm B — Severity: Warning", msg)

    def test_message_lists_multiple_alarms_as_bullet_points(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(alarm_id=1, header="Alarm A"),
            self._alarm(alarm_id=2, header="Alarm B"),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        msg = mock_notify.call_args.kwargs["message"]
        self.assertIn("• ", msg)
        self.assertIn("Alarm A", msg)
        self.assertIn("Alarm B", msg)

    def test_title_includes_correct_alarm_count(self):
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(alarm_id=1),
            self._alarm(alarm_id=2),
        ]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn("2 Active Alarm(s)", mock_notify.call_args.kwargs["title"])

    def test_message_mentions_reset_alarms_button(self):
        """The message must point the user to the actual remediation path
        (the Reset Alarms management button) — not just describe the problem."""
        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [self._alarm()]
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            update_alarm_state(em, MagicMock())
        self.assertIn("Reset Alarms", mock_notify.call_args.kwargs["message"])

    def test_raw_api_alarm_reaches_the_real_final_mqtt_payload(self):
        """Raw /notifications API response -> real update_alarm_state() ->
        real MqttDiscoveryPublisher.publish_alarm_state() -> final MQTT
        payload. Every other test in this class passes a MagicMock as the
        publisher, so publish_alarm_state()'s real implementation never
        actually ran chained from a real raw API alarm dict before this.

        notify_ha must still be patched here like every sibling test in this
        class: update_alarm_state() calls the real notify_ha() whenever
        alarm_count > 0, and notify_ha() doesn't go through the mocked MQTT
        client at all — it makes a real urllib HTTP POST straight to the
        Supervisor API, gated only by SUPERVISOR_TOKEN being present in the
        environment. The nightly test-runner subprocess inherits the whole
        parent add-on process's environment (including a real
        SUPERVISOR_TOKEN), so leaving this unmocked previously sent a
        genuine HA persistent notification with this fabricated alarm data
        on every nightly run — root-caused from a real recurring
        "Compressor overload" notification a user saw with no matching
        alarm on the physical controller."""
        from nibe_mqtt_publisher import MgmtTopic, MqttDiscoveryPublisher

        update_alarm_state = self._import()
        em = _make_em()
        em._api.fetch_notifications.return_value = [
            self._alarm(
                alarm_id=99,
                header="Compressor overload",
                severity="Critical",
                equip_name="Compressor 1",
            ),
        ]
        real_mqtt = MagicMock()
        real_mqtt.publish.return_value = MagicMock(rc=0)
        pub = MqttDiscoveryPublisher(
            mqtt_client=real_mqtt,
            device_info={"identifiers": ["t"]},
            device_id="test",
            device_name="Test Device",
        )

        with patch("nibe_ha_integration.notify_ha"):
            update_alarm_state(em, pub)

        state_calls = [
            c for c in real_mqtt.publish.call_args_list if c.args[0] == MgmtTopic.ALARM_STATE
        ]
        self.assertTrue(state_calls, "no publish reached the real ALARM_STATE topic")
        self.assertEqual(state_calls[-1].args[1], "1")

        attrs_calls = [
            c for c in real_mqtt.publish.call_args_list if c.args[0] == MgmtTopic.ALARM_ATTRS
        ]
        self.assertTrue(attrs_calls, "no publish reached the real ALARM_ATTRS topic")
        payload = json.loads(attrs_calls[-1].args[1])
        self.assertEqual(len(payload["alarms"]), 1)
        self.assertEqual(payload["alarms"][0]["alarmId"], 99)
        self.assertEqual(payload["alarms"][0]["header"], "Compressor overload")
        self.assertEqual(payload["alarms"][0]["severity"], "Critical")
        self.assertEqual(payload["alarms"][0]["equipName"], "Compressor 1")


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
        em._api.fetch_device_info.return_value = {"aidMode": "on", "smartMode": "away"}
        fn(em, MagicMock())
        em._api.fetch_device_info.assert_called_once()

    def test_clean_cache_with_data_skips_api_call(self):
        """Not dirty AND cache populated -> use cached values, no fetch."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = False
        em.device_modes_cache = {"aidMode": "on", "smartMode": "normal"}
        pub = MagicMock()
        fn(em, pub)
        em._api.fetch_device_info.assert_not_called()
        pub.publish_device_modes.assert_called_once_with(aid_mode="on", smart_mode="normal")

    def test_dirty_flag_forces_refetch_even_with_populated_cache(self):
        """A populated cache that's marked dirty (e.g. just after a mode
        write) must still trigger a fresh fetch, not serve stale data."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = True
        em.device_modes_cache = {"aidMode": "off", "smartMode": "normal"}  # stale
        em._api.fetch_device_info.return_value = {"aidMode": "on", "smartMode": "away"}
        pub = MagicMock()
        fn(em, pub)
        em._api.fetch_device_info.assert_called_once()
        pub.publish_device_modes.assert_called_once_with(aid_mode="on", smart_mode="away")

    def test_successful_fetch_updates_cache_and_clears_dirty(self):
        fn = self._import()
        em = _make_em()
        em._api.fetch_device_info.return_value = {"aidMode": "on", "smartMode": "away"}
        fn(em, MagicMock())
        self.assertEqual(em.device_modes_cache, {"aidMode": "on", "smartMode": "away"})
        # Strict identity, not assertFalse — None is also falsy and would
        # wrongly pass a mutant that sets dirty=None instead of False.
        self.assertIs(em.device_modes_dirty, False)

    def test_failed_fetch_does_not_clear_dirty_or_corrupt_cache(self):
        """fetch_device_info returning None (API error) must leave the
        dirty flag and existing cache untouched — so the NEXT poll retries
        rather than silently giving up and serving garbage forever."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = True
        em.device_modes_cache = {"aidMode": "on", "smartMode": "normal"}  # prior good data
        em._api.fetch_device_info.return_value = None
        pub = MagicMock()
        fn(em, pub)
        self.assertTrue(em.device_modes_dirty)
        self.assertEqual(em.device_modes_cache, {"aidMode": "on", "smartMode": "normal"})
        pub.publish_device_modes.assert_not_called()

    def test_concurrent_write_during_fetch_does_not_clobber_its_dirty_flag(self):
        """Regression: fetch_device_info() runs unlocked — if a mode-write
        handler on another thread sets dirty=True (bumping
        device_modes_write_seq) while this fetch is in flight, the response
        we get back may predate that write. Blindly clearing dirty=False
        afterward would clobber the writer's dirty=True with stale data,
        leaving HA showing the pre-write mode until another write happens
        to re-dirty the cache. Simulated here by bumping write_seq from
        inside the fetch_device_info side_effect, mimicking a write landing
        mid-fetch."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = True
        em.device_modes_cache = {}

        def fetch_with_concurrent_write():
            # Simulate _handle_smart_mode's write landing while our fetch
            # is in flight: it bumps the seq and (re-)sets dirty=True.
            em.device_modes_write_seq += 1
            em.device_modes_dirty = True
            return {"aidMode": "off", "smartMode": "normal"}  # stale response

        em._api.fetch_device_info.side_effect = fetch_with_concurrent_write
        pub = MagicMock()
        fn(em, pub)

        # The stale response is still published this cycle (best-effort —
        # matches existing behavior of always publishing on a successful
        # fetch), but the dirty flag the concurrent writer set must survive.
        self.assertTrue(
            em.device_modes_dirty,
            "a concurrent write's dirty=True must not be clobbered by a "
            "fetch that was already in flight when the write landed",
        )
        # And the cache must not have been overwritten with the stale
        # response either — the next poll needs to actually re-fetch.
        self.assertEqual(em.device_modes_cache, {})

    def test_no_concurrent_write_still_clears_dirty_normally(self):
        """Sanity check: without a concurrent write (write_seq unchanged
        during the fetch), the normal cache-update-and-clear-dirty path
        must still work exactly as before."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = True
        em._api.fetch_device_info.return_value = {"aidMode": "on", "smartMode": "away"}
        fn(em, MagicMock())
        self.assertIs(em.device_modes_dirty, False)
        self.assertEqual(em.device_modes_cache, {"aidMode": "on", "smartMode": "away"})

    def test_missing_aidmode_key_defaults_to_off(self):
        fn = self._import()
        em = _make_em()
        em._api.fetch_device_info.return_value = {"smartMode": "normal"}  # no aidMode
        pub = MagicMock()
        fn(em, pub)
        pub.publish_device_modes.assert_called_once_with(aid_mode="off", smart_mode="normal")

    def test_missing_smartmode_key_defaults_to_normal(self):
        fn = self._import()
        em = _make_em()
        em._api.fetch_device_info.return_value = {"aidMode": "on"}  # no smartMode
        pub = MagicMock()
        fn(em, pub)
        pub.publish_device_modes.assert_called_once_with(aid_mode="on", smart_mode="normal")

    def test_cached_path_also_applies_same_defaults(self):
        """The cache-hit branch reads from device_modes_cache with the same
        .get(key, default) fallbacks as the fetch branch — confirms both
        code paths apply identical defaulting, not just the fetch path."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = False
        em.device_modes_cache = {"aidMode": "on"}  # smartMode key missing
        pub = MagicMock()
        fn(em, pub)
        em._api.fetch_device_info.assert_not_called()
        pub.publish_device_modes.assert_called_once_with(aid_mode="on", smart_mode="normal")

    def test_cached_path_missing_aidmode_key_defaults_to_off(self):
        """Mirror of the above but for the aidMode default specifically —
        the existing cached-path test only exercises the smartMode default,
        never aidMode's."""
        fn = self._import()
        em = _make_em()
        em.device_modes_dirty = False
        em.device_modes_cache = {"smartMode": "away"}  # aidMode key missing
        pub = MagicMock()
        fn(em, pub)
        em._api.fetch_device_info.assert_not_called()
        pub.publish_device_modes.assert_called_once_with(aid_mode="off", smart_mode="away")


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
        with patch("nibe_ha_integration._publish_stats"):
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
        with patch("nibe_ha_integration._publish_stats"):
            fn(em, pub)
        pub.publish_api_reachability.assert_called_once_with(2, 5, 1500.0, 0.8)

    def test_bridge_status_includes_pending_write_count(self):
        fn = self._import()
        em = _make_em()
        em.pending_writes = {1: {}, 2: {}, 3: {}}
        pub = MagicMock()
        with patch("nibe_ha_integration._publish_stats"):
            fn(em, pub)
        kwargs = pub.publish_bridge_status.call_args.kwargs
        self.assertEqual(kwargs["pending_write_count"], 3)

    def test_bridge_status_includes_write_counters(self):
        fn = self._import()
        em = _make_em()
        em._write_total = 50
        em._write_success = 45
        em._write_failed = 5
        em._last_write_error = "point 100 failed"
        pub = MagicMock()
        with patch("nibe_ha_integration._publish_stats"):
            fn(em, pub)
        kwargs = pub.publish_bridge_status.call_args.kwargs
        self.assertEqual(kwargs["write_total"], 50)
        self.assertEqual(kwargs["write_success"], 45)
        self.assertEqual(kwargs["write_failed"], 5)
        self.assertEqual(kwargs["last_write_error"], "point 100 failed")

    def test_calls_publish_stats_once(self):
        fn = self._import()
        em = _make_em()
        pub = MagicMock()
        with patch("nibe_ha_integration._publish_stats") as mock_stats:
            fn(em, pub)
        mock_stats.assert_called_once_with(em, pub)

    def test_bridge_status_all_remaining_fields_exact(self):
        """publish_bridge_status must receive the real bridge_start_time,
        api_* fields, mqtt_enabled_count, all_points_count, and
        known_dynamic_count — the field-mapping tests above only cover
        pending_write_count and the write_* counters, leaving these six
        completely unverified."""
        fn = self._import()
        em = _make_em()
        em.bridge_start_time = 12345.0
        em.api_consecutive_failures = 2
        em.api_failure_threshold = 5
        em.api_last_success_time = 6789.0
        em.last_fetch_duration = 1.5
        em.mqtt_enabled_points = {1, 2, 3}
        em.all_points_by_id = {i: {} for i in range(7)}
        em.dynamic_point_map.all_known_dynamic_point_ids = MagicMock(return_value={10, 20})
        pub = MagicMock()
        with patch("nibe_ha_integration._publish_stats"):
            fn(em, pub)
        kwargs = pub.publish_bridge_status.call_args.kwargs
        self.assertEqual(kwargs["bridge_start_time"], 12345.0)
        self.assertEqual(kwargs["api_consecutive_failures"], 2)
        self.assertEqual(kwargs["api_failure_threshold"], 5)
        self.assertEqual(kwargs["api_last_success_time"], 6789.0)
        self.assertEqual(kwargs["last_fetch_duration"], 1.5)
        self.assertEqual(kwargs["mqtt_enabled_count"], 3)
        self.assertEqual(kwargs["all_points_count"], 7)
        self.assertEqual(kwargs["known_dynamic_count"], 2)


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
        w._unique_id_map = {"nibe_5110": "switch.nibe_5110"}
        em.resolve_point_from_entity_id.return_value = 5110
        em.mqtt_enabled_points = set()
        em.all_points_by_id = {5110: {"is_dynamic": False}}
        em.build_disable_notification.return_value = ("title", "msg", "notif_id")
        return w, em

    def test_entity_disabled_via_ha_now_fires(self):
        """HA disabling an entity (disabled_by changes from None to 'user')
        must call _on_entity_disabled — previously this never fired."""
        w, _em = self._watcher()
        event = {
            "data": {
                "action": "update",
                "entity_id": "switch.nibe_5110",
                "changes": {"disabled_by": None},  # prev was None → now 'user'
            }
        }
        with patch.object(w, "_on_entity_disabled") as mock_disabled:
            w._handle_event(event)
        mock_disabled.assert_called_once_with("switch.nibe_5110")

    def test_entity_enabled_via_ha_now_fires(self):
        """HA re-enabling an entity (disabled_by changes from 'user' to None)
        must call _on_entity_enabled — previously this never fired."""
        w, _em = self._watcher()
        event = {
            "data": {
                "action": "update",
                "entity_id": "switch.nibe_5110",
                "changes": {"disabled_by": "user"},  # prev was 'user' → now None
            }
        }
        with patch.object(w, "_on_entity_enabled") as mock_enabled:
            w._handle_event(event)
        mock_enabled.assert_called_once_with("switch.nibe_5110")

    def test_update_without_disabled_by_still_updates_map(self):
        """An update event without a disabled_by change (e.g. rename) must
        still update the unique_id_map cache — confirming the cache-update
        logic wasn't lost in the refactor."""
        w, _em = self._watcher()
        event = {
            "data": {
                "action": "update",
                "entity_id": "switch.nibe_5110_renamed",
                "unique_id": "nibe_5110",
                "changes": {"name": "New name"},  # no disabled_by
            }
        }
        w._handle_event(event)
        self.assertEqual(w._unique_id_map.get("nibe_5110"), "switch.nibe_5110_renamed")

    def test_update_without_disabled_by_does_not_call_enable_disable(self):
        """Sanity: a rename/name-change update must not trigger enable/disable."""
        w, _em = self._watcher()
        event = {
            "data": {
                "action": "update",
                "entity_id": "switch.nibe_5110",
                "changes": {"name": "New name"},
            }
        }
        with (
            patch.object(w, "_on_entity_enabled") as mock_en,
            patch.object(w, "_on_entity_disabled") as mock_dis,
        ):
            w._handle_event(event)
        mock_en.assert_not_called()
        mock_dis.assert_not_called()

    def test_create_and_remove_events_unaffected(self):
        """create/remove events must still work correctly — confirms the
        refactor didn't accidentally break the other action branches."""
        w, _ = self._watcher()
        # create: adds to map
        w._handle_event(
            {
                "data": {
                    "action": "create",
                    "entity_id": "switch.nibe_9999",
                    "unique_id": "nibe_9999",
                }
            }
        )
        self.assertEqual(w._unique_id_map.get("nibe_9999"), "switch.nibe_9999")
        # remove: cleans up map
        w._handle_event(
            {
                "data": {
                    "action": "remove",
                    "unique_id": "nibe_9999",
                }
            }
        )
        self.assertNotIn("nibe_9999", w._unique_id_map)


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
        w._unique_id_map = {"nibe_5110": "switch.nibe_5110"}
        em.resolve_point_from_entity_id.return_value = 5110
        em.build_disable_notification.return_value = (
            "title",
            "msg",
            "nibe_ha_disable_switch_nibe_5110",
        )
        em.mqtt = MagicMock()
        return w, em, pub

    def test_static_point_disabled_no_notification(self):
        """Disabling a static point must call disable_entity and NOT send
        a notification — an intentional disable needs no explanation."""
        w, em, _pub = self._watcher()
        em.all_points_by_id = {5110: {"is_dynamic": False}}
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            w._on_entity_disabled("switch.nibe_5110")
        em.disable_entity.assert_called_once_with(5110)
        mock_notify.assert_not_called()

    def test_dynamic_point_republishes_discovery_and_notifies(self):
        """Disabling a dynamic point must republish discovery (to reverse
        the HA-side disable) and send a notification explaining why."""
        w, em, pub = self._watcher()
        em.all_points_by_id = {
            5110: {"is_dynamic": True, "entity_type": "sensor", "entity_category": "diagnostic"},
        }
        em.bulk_data = {}
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            w._on_entity_disabled("switch.nibe_5110")
        pub.publish_entity_discovery.assert_called_once()
        em.disable_entity.assert_not_called()
        mock_notify.assert_called_once()

    def test_unknown_point_returns_early(self):
        """resolve_point_from_entity_id returning None must be a no-op."""
        w, em, pub = self._watcher()
        em.resolve_point_from_entity_id.return_value = None
        w._on_entity_disabled("switch.nibe_unknown")
        em.disable_entity.assert_not_called()
        pub.publish_entity_discovery.assert_not_called()

    def test_point_dict_missing_is_dynamic_key_defaults_to_static(self):
        """point.get('is_dynamic', False) — when the key is entirely absent
        (not just False), must still take the static (disable_entity)
        branch. Both existing branch tests always set the key explicitly."""
        w, em, pub = self._watcher()
        em.all_points_by_id = {5110: {}}  # no 'is_dynamic' key at all
        with patch("nibe_ha_integration.notify_ha"):
            w._on_entity_disabled("switch.nibe_5110")
        em.disable_entity.assert_called_once_with(5110)
        pub.publish_entity_discovery.assert_not_called()

    def test_build_disable_notification_called_with_correct_arguments(self):
        """build_disable_notification must receive the real point_id,
        ha_entity_id, and action='disabled' — never checked beyond the
        mock's canned return value."""
        w, em, _pub = self._watcher()
        em.all_points_by_id = {5110: {"is_dynamic": False}}
        with patch("nibe_ha_integration.notify_ha"):
            w._on_entity_disabled("switch.nibe_5110")
        em.build_disable_notification.assert_called_once_with(
            5110,
            "switch.nibe_5110",
            action="disabled",
        )

    def test_notify_ha_called_with_real_title_and_message(self):
        """The dynamic-point path's notify_ha call must use the real
        title/message from build_disable_notification's return value, and
        the real notification_id — not None or a mismatched value."""
        w, em, _pub = self._watcher()
        em.all_points_by_id = {5110: {"is_dynamic": True}}
        em.bulk_data = {}
        em.build_disable_notification.return_value = (
            "Real Title",
            "Real Message",
            "real_notif_id",
        )
        with patch("nibe_ha_integration.notify_ha") as mock_notify:
            w._on_entity_disabled("switch.nibe_5110")
        mock_notify.assert_called_once_with(
            em.mqtt,
            title="Real Title",
            message="Real Message",
            notification_id="real_notif_id",
        )


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
            ws_calls.append(payload.get("type"))
            if payload.get("type") == "lovelace/dashboards/list":
                return {}  # simulates Lovelace API not ready / timeout
            return {"success": True, "result": []}

        ws = MagicMock()
        next_id = iter(range(1, 100)).__next__

        with (
            patch("nibe_lovelace._ws_call", side_effect=fake_ws_call),
            patch("nibe_lovelace.log_startup") as mock_log,
        ):
            result = _setup_menu_dashboard_lovelace(
                ws,
                next_id,
                {"views": [{"title": "Test"}]},
                em,
                watcher,
                set(),
                set(),
            )

        self.assertIs(
            result, True, "Transient Lovelace API timeout must return True (needs retry), not False"
        )
        self.assertNotIn(
            "lovelace/config/save",
            ws_calls,
            "lovelace/config/save must not be called when dashboards list failed",
        )
        self.assertTrue(
            mock_log.warning.called, "A warning must be logged when the dashboards list call fails"
        )


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
        w._registry_map_lock = threading.Lock()
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
            _json.dumps(
                {
                    "type": "event",
                    "event": {"event_type": "entity_registry_updated"},
                    "id": 0,  # wrong id — not our request
                }
            ),
            _json.dumps(
                {
                    "id": 1,  # matches req_id (first call to _next_id returns 1)
                    "type": "result",
                    "success": True,
                    "result": [
                        {
                            "unique_id": "nibe_1234",
                            "entity_id": "sensor.nibe_1234",
                            "platform": "mqtt",
                        },
                    ],
                }
            ),
        ]
        result = w._fetch_entity_registry(ws)
        self.assertEqual(
            ws.recv.call_count, 2, "Must call recv() twice to skip the interleaved event"
        )
        self.assertEqual(
            result.get("nibe_1234"),
            "sensor.nibe_1234",
            "Must return mapping from the actual list response",
        )

    def test_direct_list_response_still_works(self):
        """Normal restart: first recv() returns the list response directly
        (no interleaved events). Must still work correctly."""
        import json as _json

        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = _json.dumps(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [
                    {"unique_id": "nibe_5110", "entity_id": "switch.nibe_5110", "platform": "mqtt"},
                ],
            }
        )
        result = w._fetch_entity_registry(ws)
        self.assertEqual(ws.recv.call_count, 1)
        self.assertEqual(result.get("nibe_5110"), "switch.nibe_5110")


class TestRegistryFetchMissingUniqueId(unittest.TestCase):
    """Test _fetch_entity_registry with entries missing unique_id."""

    def _make_watcher(self):
        import threading

        from nibe_ha_integration import HAEntityRegistryWatcher

        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._registry_map_lock = threading.Lock()
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
        response = json.dumps(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [
                    {"entity_id": "sensor.nibe_123", "unique_id": "nibe_123"},
                    {"entity_id": "sensor.no_id", "platform": "mqtt"},
                ],
            }
        )
        ws.recv.return_value = response
        result = w._fetch_entity_registry(ws)
        self.assertIn("nibe_123", result)
        self.assertNotIn("no_id", result)

    def test_nested_config_unique_id_fallback_extracted(self):
        """When 'unique_id' is absent at the top level, the nested
        entry['config']['unique_id'] fallback must be used — this fallback
        chain is never exercised by any other test (they all use the
        top-level key directly)."""
        w = self._make_watcher()
        ws = MagicMock()
        response = json.dumps(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [
                    {"entity_id": "sensor.nibe_config", "config": {"unique_id": "nibe_cfg"}},
                ],
            }
        )
        ws.recv.return_value = response
        result = w._fetch_entity_registry(ws)
        self.assertEqual(result.get("nibe_cfg"), "sensor.nibe_config")

    def test_nested_options_unique_id_fallback_extracted(self):
        """The third fallback, entry['options']['unique_id'], used only
        when both the top-level key AND 'config' are absent."""
        w = self._make_watcher()
        ws = MagicMock()
        response = json.dumps(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [
                    {"entity_id": "sensor.nibe_opt", "options": {"unique_id": "nibe_opt_id"}},
                ],
            }
        )
        ws.recv.return_value = response
        result = w._fetch_entity_registry(ws)
        self.assertEqual(result.get("nibe_opt_id"), "sensor.nibe_opt")

    def test_interleaved_message_logs_discard_with_real_type_and_id(self):
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.side_effect = [
            json.dumps({"type": "event", "id": 99}),  # wrong id — discarded
            json.dumps({"id": 1, "type": "result", "success": True, "result": []}),
        ]
        with self.assertLogs("nibe.registry", level="DEBUG") as cm:
            w._fetch_entity_registry(ws)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Registry fetch: discarding interleaved message type=event id=99"
                )
                for msg in cm.output
            )
        )

    def test_exception_logs_warning_with_real_error_text(self):
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.side_effect = OSError("timed out")
        with self.assertLogs("nibe.registry", level="WARNING") as cm:
            result = w._fetch_entity_registry(ws)
        self.assertEqual(result, {})
        self.assertTrue(
            any(
                msg.splitlines()[0]
                == "WARNING:nibe.registry:Could not fetch entity registry (timeout or error): "
                "timed out"
                for msg in cm.output
            )
        )

    def test_failed_response_logs_warning_with_the_real_response(self):
        w = self._make_watcher()
        ws = MagicMock()
        resp = {"id": 1, "type": "result", "success": False, "error": {"code": "unknown"}}
        ws.recv.return_value = json.dumps(resp)
        with self.assertLogs("nibe.registry", level="WARNING") as cm:
            result = w._fetch_entity_registry(ws)
        self.assertEqual(result, {})
        self.assertTrue(
            any(
                msg.splitlines()[0]
                == f"WARNING:nibe.registry:Could not fetch entity registry: {resp}"
                for msg in cm.output
            )
        )

    def test_missing_result_key_with_success_true_does_not_crash(self):
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = json.dumps({"id": 1, "type": "result", "success": True})
        result = w._fetch_entity_registry(ws)  # must not raise
        self.assertEqual(result, {})

    def test_success_logs_exact_total_and_nibe_counts(self):
        """Pins the exact nibe_count computation (1 per matching key, using
        the real 'nibe_' prefix) and the exact log line — a mutated
        multiplier or prefix would still often 'look right' for a single
        entry, so this uses a mix of nibe/non-nibe entries."""
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = json.dumps(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [
                    {"unique_id": "nibe_1", "entity_id": "sensor.nibe_1"},
                    {"unique_id": "nibe_2", "entity_id": "sensor.nibe_2"},
                    {"unique_id": "other_3", "entity_id": "sensor.other_3"},
                ],
            }
        )
        with self.assertLogs("nibe.registry", level="DEBUG") as cm:
            result = w._fetch_entity_registry(ws)
        self.assertEqual(len(result), 3)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Entity registry cached: 3 total entries, 2 nibe entries"
                )
                for msg in cm.output
            )
        )

    def test_sets_timeout_to_30_seconds_while_waiting_for_response(self):
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = json.dumps(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [],
            }
        )
        w._fetch_entity_registry(ws)
        ws.settimeout.assert_any_call(30)

    def test_ws_send_called_with_correct_request_payload(self):
        """ws.send() must be called with the real config/entity_registry/list
        request (correct req_id + type) — never verified elsewhere."""
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = json.dumps(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [],
            }
        )
        w._fetch_entity_registry(ws)
        sent = json.loads(ws.send.call_args.args[0])
        self.assertEqual(sent, {"id": 1, "type": "config/entity_registry/list"})


class TestNotifyHa(unittest.TestCase):
    """notify_ha sends a persistent notification via the Supervisor REST API.
    Falls back gracefully when SUPERVISOR_TOKEN is absent."""

    def test_no_token_does_not_call_urlopen(self):
        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("urllib.request.urlopen") as mock_open,
        ):
            notify_ha(None, "title", "msg", "test_id")
            mock_open.assert_not_called()

    def test_with_token_calls_urlopen(self):
        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "fake_token"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            notify_ha(None, "Test Title", "Test message", "nibe_test")
            mock_open.assert_called_once()

    def test_request_contains_notification_id(self):
        import json as _json

        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "fake_token"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            notify_ha(None, "Title", "Msg", "nibe_test_id")
            req = mock_open.call_args[0][0]
            payload = _json.loads(req.data)
            self.assertEqual(payload["notification_id"], "nibe_test_id")
            self.assertEqual(payload["title"], "Title")
            self.assertEqual(payload["message"], "Msg")

    def test_request_uses_post_method(self):
        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            notify_ha(None, "t", "m", "id")
            req = mock_open.call_args[0][0]
            self.assertEqual(req.method, "POST")

    def test_request_has_auth_header(self):
        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "mytoken"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            notify_ha(None, "t", "m", "id")
            req = mock_open.call_args[0][0]
            self.assertIn("Bearer mytoken", req.get_header("Authorization"))

    def test_urlopen_failure_does_not_raise(self):
        """Network errors must be swallowed — not raise to the caller."""
        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=Exception("timeout")),
        ):
            notify_ha(None, "t", "m", "id")  # must not raise

    def test_urlopen_called_with_ten_second_timeout(self):
        from nibe_ha_integration import notify_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            notify_ha(None, "t", "m", "id")
        self.assertEqual(mock_open.call_args.kwargs["timeout"], 10)

    def test_mqtt_client_argument_not_used(self):
        """mqtt_client is accepted for API compatibility but not used.

        A bare sentinel object (no attributes, no methods) is passed as
        mqtt_client with a token set so the function runs its full body —
        any attribute access on it would raise AttributeError.
        """
        from nibe_ha_integration import notify_ha

        sentinel = object()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            notify_ha(sentinel, "t", "m", "id")
        mock_open.assert_called_once()


class TestDismissHa(unittest.TestCase):
    """dismiss_ha dismisses a persistent notification via the Supervisor API."""

    def test_no_token_does_not_call_urlopen(self):
        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("urllib.request.urlopen") as mock_open,
        ):
            dismiss_ha(None, "test_id")
            mock_open.assert_not_called()

    def test_with_token_calls_urlopen(self):
        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            dismiss_ha(None, "nibe_test")
            mock_open.assert_called_once()

    def test_request_contains_notification_id(self):
        import json as _json

        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            dismiss_ha(None, "nibe_dismiss_id")
            req = mock_open.call_args[0][0]
            payload = _json.loads(req.data)
            self.assertEqual(payload["notification_id"], "nibe_dismiss_id")

    def test_urlopen_failure_does_not_raise(self):
        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=Exception("refused")),
        ):
            dismiss_ha(None, "id")  # must not raise

    def test_dismiss_url_is_dismiss_endpoint(self):
        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            dismiss_ha(None, "id")
            req = mock_open.call_args[0][0]
            self.assertIn("dismiss", req.full_url)

    def test_dismiss_uses_post_method(self):
        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            dismiss_ha(None, "id")
            req = mock_open.call_args[0][0]
            self.assertEqual(req.get_method(), "POST")

    def test_dismiss_urlopen_called_with_ten_second_timeout(self):
        from nibe_ha_integration import dismiss_ha

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            dismiss_ha(None, "id")
        self.assertEqual(mock_open.call_args.kwargs["timeout"], 10)


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
        w._registry_map_lock = threading.Lock()
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
        response = _json.dumps(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": entries,
            }
        )
        ws.recv.side_effect = [
            _json.dumps({"type": "auth_required"}),  # auth_required
            _json.dumps({"type": "auth_ok"}),  # auth_ok
            response,  # list response
        ]
        return ws

    def test_no_token_returns_immediately(self):
        w = self._make_watcher()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("websocket.create_connection") as mock_conn,
        ):
            w.refresh_registry()
            mock_conn.assert_not_called()

    def test_nibe_entries_added_to_map(self):
        w = self._make_watcher()
        entries = [
            {"unique_id": "nibe_1234", "entity_id": "sensor.nibe_1234", "platform": "mqtt"},
            {"unique_id": "nibe_5678", "entity_id": "switch.nibe_5678", "platform": "mqtt"},
        ]
        ws = self._mock_ws(entries)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
        ):
            w.refresh_registry()
        self.assertEqual(w._unique_id_map.get("nibe_1234"), "sensor.nibe_1234")
        self.assertEqual(w._unique_id_map.get("nibe_5678"), "switch.nibe_5678")

    def test_non_nibe_entries_excluded(self):
        w = self._make_watcher()
        entries = [
            {"unique_id": "nibe_100", "entity_id": "sensor.nibe_100", "platform": "mqtt"},
            {"unique_id": "other_integration", "entity_id": "sensor.other", "platform": "other"},
        ]
        ws = self._mock_ws(entries)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
        ):
            w.refresh_registry()
        self.assertIn("nibe_100", w._unique_id_map)
        self.assertNotIn("other_integration", w._unique_id_map)

    def test_refresh_timer_cleared_after_run(self):
        """_refresh_timer must be set to None after the fetch completes."""
        w = self._make_watcher()
        ws = self._mock_ws([])
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
        ):
            w.refresh_registry()
        self.assertIsNone(w._refresh_timer)

    def test_websocket_exception_does_not_raise(self):
        """Network errors must be swallowed — registry fetch is best-effort."""
        w = self._make_watcher()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", side_effect=Exception("connection refused")),
            self.assertLogs("nibe.registry", level="WARNING") as cm,
        ):
            w.refresh_registry()  # must not raise
        self.assertTrue(
            any(
                msg.splitlines()[0]
                == "WARNING:nibe.registry:Registry refresh failed: connection refused"
                for msg in cm.output
            )
        )

    def test_ws_authenticate_called_with_the_real_token(self):
        w = self._make_watcher()
        ws = self._mock_ws([])
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "real-tok"}),
            patch("websocket.create_connection", return_value=ws),
            patch.object(w, "_ws_authenticate") as mock_auth,
        ):
            w.refresh_registry()
        mock_auth.assert_called_once_with(ws, "real-tok")

    def test_auth_failure_logs_warning_verbatim(self):
        w = self._make_watcher()
        ws = MagicMock()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
            patch.object(w, "_ws_authenticate", side_effect=RuntimeError("bad auth")),
            self.assertLogs("nibe.registry", level="WARNING") as cm,
        ):
            w.refresh_registry()  # must not raise
        self.assertTrue(
            any(
                msg.splitlines()[0] == "WARNING:nibe.registry:Registry refresh: bad auth"
                for msg in cm.output
            )
        )

    def test_success_logs_updated_count_verbatim(self):
        w = self._make_watcher()
        entries = [{"unique_id": "nibe_1", "entity_id": "sensor.nibe_1"}]
        ws = self._mock_ws(entries)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            w.refresh_registry()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Registry refresh: updated 1 nibe entries")
                for msg in cm.output
            )
        )

    def test_empty_result_does_not_crash(self):
        w = self._make_watcher()
        ws = self._mock_ws([])
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
        ):
            w.refresh_registry()
        self.assertEqual(w._unique_id_map, {})

    def test_missing_result_key_with_success_true_does_not_crash(self):
        """success=True but no 'result' key at all (not just an empty list)
        must be handled by the resp.get('result', []) default — not
        crash on iterating None."""
        import json as _json

        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.side_effect = [
            _json.dumps({"type": "auth_required"}),
            _json.dumps({"type": "auth_ok"}),
            _json.dumps({"id": 1, "type": "result", "success": True}),  # no 'result'
        ]
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
        ):
            w.refresh_registry()  # must not raise
        self.assertEqual(w._unique_id_map, {})

    def test_missing_result_key_completes_without_logging_a_warning(self):
        """resp.get('result', []) must genuinely default to an empty list
        and let the for-loop complete cleanly with zero iterations — a
        mutated default of None would make the loop raise TypeError, which
        the broad except below silently swallows and logs as a warning.
        'must not raise' alone can't distinguish that from a clean
        zero-entries pass, since both leave _unique_id_map == {} — but the
        absence of a 'Registry refresh failed' warning can."""
        import json as _json

        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.side_effect = [
            _json.dumps({"type": "auth_required"}),
            _json.dumps({"type": "auth_ok"}),
            _json.dumps({"id": 1, "type": "result", "success": True}),  # no 'result'
        ]
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
            patch("nibe_ha_integration.log_registry") as mock_log,
        ):
            w.refresh_registry()
        mock_log.warning.assert_not_called()

    def test_create_connection_called_with_correct_url_and_timeout(self):
        w = self._make_watcher()
        ws = self._mock_ws([])
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws) as mock_conn,
        ):
            w.refresh_registry()
        mock_conn.assert_called_once_with(
            "ws://supervisor/core/websocket",
            timeout=10,
        )

    def test_ws_send_uses_correct_registry_list_payload(self):
        w = self._make_watcher()
        ws = self._mock_ws([])
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
        ):
            w.refresh_registry()
        sent = json.loads(ws.send.call_args.args[0])
        self.assertEqual(sent, {"id": 1, "type": "config/entity_registry/list"})

    def test_log_reports_the_exact_updated_count(self):
        """The 'updated %d nibe entries' debug log must report the real
        count — not a value inflated/deflated by an arithmetic bug that
        would be invisible to every other test here (none of which
        inspect the log content, only the resulting map)."""
        w = self._make_watcher()
        entries = [
            {"unique_id": "nibe_1", "entity_id": "sensor.nibe_1"},
            {"unique_id": "nibe_2", "entity_id": "sensor.nibe_2"},
            {"unique_id": "nibe_3", "entity_id": "sensor.nibe_3"},
        ]
        ws = self._mock_ws(entries)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            w.refresh_registry()
        self.assertTrue(any("updated 3 nibe entries" in msg for msg in cm.output))


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
            self.mqtt,
            self.em,
            self.publisher,
            self.executor,
        ).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=False)

    def _get_regen_handler(self):
        topic = self.MgmtTopic.REGEN_DASH_PRESS
        for call in self.mqtt.message_callback_add.call_args_list:
            if call.args[0] == topic:
                return call.args[1]
        raise KeyError("No handler for REGEN_DASH_PRESS")

    def test_callback_called_when_registered(self):
        """Regression: _handle_regen_dashboard now dispatches through
        _submit() (like every other handler) so an exception in the
        callback gets this project's own log_commands.exception safety net
        instead of silently vanishing into paho's own message-dispatch
        exception handling — so this must wait for the executor before
        asserting, not assert immediately after the synchronous-looking
        handler call."""
        callback = MagicMock()
        self.em._on_enabled_state_change = callback
        handler = self._get_regen_handler()
        with self.assertLogs("nibe.startup", level="INFO") as cm:
            handler(None, None, MagicMock())
            self.executor.shutdown(wait=True)
        callback.assert_called_once()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Regenerate Dashboard triggered from HA")
                for msg in cm.output
            )
        )

    def test_no_crash_when_callback_is_none(self):
        """If no callback is registered the handler must not raise."""
        self.em._on_enabled_state_change = None
        handler = self._get_regen_handler()
        with self.assertLogs("nibe.startup", level="INFO") as cm:
            handler(None, None, MagicMock())  # must not raise
            self.executor.shutdown(wait=True)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Regenerate Dashboard: no callback registered")
                for msg in cm.output
            )
        )

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
        em.all_points_by_id = {}
        em.dynamic_point_map = MagicMock()
        em.dynamic_point_map.values.return_value = []
        em.dynamic_point_map.all_known_dynamic_point_ids.return_value = set()
        em.active_dynamic_points = set()
        em.bulk_data = {}
        em.mqtt_enabled_points = set()
        em.point_to_menu_map = {}
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
        with patch("nibe_lovelace._ws_call", side_effect=fake_ws_call):
            return _setup_menu_dashboard_lovelace(
                ws,
                iter(range(1, 100)).__next__,
                {"views": [{"title": "Test View"}]},
                em,
                watcher,
                set(),
                set(),
            )

    def _base_ws(self, save_success=True):
        """Return a fake_ws_call that succeeds on dashboards/list and config/save."""

        def fake(ws, _msg_id, payload, _timeout=10):
            t = payload.get("type")
            if t == "lovelace/dashboards/list":
                return {"success": True, "result": [{"url_path": "nibe-menus", "id": 99}]}
            if t == "lovelace/config/save":
                return {"success": save_success}
            return {"success": True, "result": []}

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
            ws_calls.append(payload.get("type"))
            t = payload.get("type")
            if t == "lovelace/dashboards/list":
                return {"success": True, "result": [{"url_path": "nibe-menus", "id": 99}]}
            if t == "lovelace/config/save":
                return {"success": True}
            return {"success": True, "result": []}

        self._run(fake)
        self.assertIn("fire_event", ws_calls)

    def test_config_save_success_with_missing_dynamic_returns_true(self):
        """Save succeeded but a dynamic point is still missing → True (needs retry)."""
        watcher = self._make_watcher()
        watcher._em.all_points_by_id = {100: {}}
        watcher._em.active_dynamic_points = {9999}
        # point 100 resolves; dynamic 9999 does not
        watcher.entity_id_for = lambda pid: "sensor.nibe_100" if pid == 100 else None

        from nibe_lovelace import _setup_menu_dashboard_lovelace

        ws = MagicMock()
        with patch("nibe_lovelace._ws_call", side_effect=self._base_ws(save_success=True)):
            result = _setup_menu_dashboard_lovelace(
                ws,
                iter(range(1, 100)).__next__,
                {"views": [{"title": "Test"}]},
                watcher._em,
                watcher,
                {100},  # available_menu_points — point 100 resolves
                {9999},  # active_dynamic — 9999 does not resolve
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
            ws_calls.append(payload.get("type"))
            t = payload.get("type")
            if t == "lovelace/dashboards/list":
                return {"success": True, "result": []}  # dashboard absent
            if t == "lovelace/dashboards/create":
                return {"success": True, "result": {"id": 42}}
            if t == "lovelace/config/save":
                return {"success": True}
            return {"success": True, "result": []}

        self._run(fake)
        self.assertIn("lovelace/dashboards/create", ws_calls)
        self.assertIn("lovelace/config/save", ws_calls)

    def test_dashboard_create_url_already_exists_proceeds_to_config_save(self):
        """Create failing with 'url_already_exists' is not an error — must continue."""
        ws_calls = []

        def fake(ws, _msg_id, payload, _timeout=10):
            ws_calls.append(payload.get("type"))
            t = payload.get("type")
            if t == "lovelace/dashboards/list":
                return {"success": True, "result": []}
            if t == "lovelace/dashboards/create":
                return {"success": False, "error": {"message": "url_already_exists"}}
            if t == "lovelace/config/save":
                return {"success": True}
            return {"success": True, "result": []}

        self._run(fake)
        self.assertIn(
            "lovelace/config/save",
            ws_calls,
            "url_already_exists on create must not abort — config/save must still run",
        )

    def test_dashboard_create_fatal_error_returns_false(self):
        """Create failing with an unexpected error must abort and return False."""
        ws_calls = []

        def fake(ws, _msg_id, payload, _timeout=10):
            ws_calls.append(payload.get("type"))
            t = payload.get("type")
            if t == "lovelace/dashboards/list":
                return {"success": True, "result": []}
            if t == "lovelace/dashboards/create":
                return {"success": False, "error": {"message": "internal server error"}}
            return {"success": True, "result": []}

        result = self._run(fake)
        self.assertIs(result, False)
        self.assertNotIn("lovelace/config/save", ws_calls)


# ===========================================================================
# 81. _build_menu_view — tip alert path and dynamic default in divider
# ===========================================================================


class TestManagementHandlerEdgePaths(unittest.TestCase):
    def setUp(self):
        import concurrent.futures

        from nibe_ha_integration import ManagementCommandHandler

        self.em = _make_em()
        self.mqtt = MagicMock()
        self.publisher = MagicMock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ManagementCommandHandler(self.mqtt, self.em, self.publisher, self.executor).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=True)

    def _get_handler(self, topic_attr):
        from nibe_mqtt_publisher import MgmtTopic

        topic = getattr(MgmtTopic, topic_attr)
        for call in self.mqtt.message_callback_add.call_args_list:
            if call.args[0] == topic:
                return call.args[1]
        raise KeyError(f"No handler for {topic_attr}")

    def _msg(self, payload):
        m = MagicMock()
        m.payload = payload.encode()
        return m

    def test_publish_device_modes_uses_cache_when_not_dirty(self):
        """_publish_device_modes must return early from cache when dirty=False and cache exists."""
        from nibe_ha_integration import _publish_device_modes

        em = MagicMock()
        em.api_consecutive_failures = 0
        em.device_modes_dirty = False
        em.device_modes_cache = {"aidMode": "on", "smartMode": "away"}
        pub = MagicMock()
        _publish_device_modes(em, pub)
        # Must publish from cache without hitting the API
        em._api.fetch_device_info.assert_not_called()
        pub.publish_device_modes.assert_called_once_with(aid_mode="on", smart_mode="away")

    def test_publish_device_modes_fetch_failure_does_not_raise(self):
        """When fetch_device_info returns None/falsy, must log warning and return cleanly."""
        from nibe_ha_integration import _publish_device_modes

        em = MagicMock()
        em.api_consecutive_failures = 0
        em.device_modes_dirty = True
        em.device_modes_cache = {}
        em._api.fetch_device_info.return_value = None
        pub = MagicMock()
        with self.assertLogs("nibe.commands", level="WARNING") as cm:
            _publish_device_modes(em, pub)  # must not raise
        pub.publish_device_modes.assert_not_called()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Could not fetch device mode states — aid/smart mode display may be stale "
                    "(see API errors above for the cause)"
                )
                for msg in cm.output
            )
        )


class TestManagementRunTestsFailures(unittest.TestCase):
    """Test the run_tests handler with subprocess failures."""

    def setUp(self):
        import concurrent.futures

        from nibe_ha_integration import ManagementCommandHandler

        # run_test_suite is wired with the REAL notify_ha/dismiss_ha by
        # ManagementCommandHandler (not caller-injectable) — notify_ha
        # doesn't use the mqtt_client arg at all, it POSTs straight to the
        # Supervisor API gated only by SUPERVISOR_TOKEN being set. That env
        # var is never set on a dev machine, so unpatched notify_ha silently
        # no-ops there — but IS set when this suite runs for real inside the
        # deployed add-on (e.g. via the nightly "Run Test Suite" button),
        # where every test in this class would otherwise fire a real HA
        # persistent notification. Must patch per CLAUDE.md's test-path rule.
        self._notify_patcher = patch("nibe_ha_integration.notify_ha")
        self._dismiss_patcher = patch("nibe_ha_integration.dismiss_ha")
        # run_test_suite also gets _get_ha_base_url (for the report link),
        # wired the same non-injectable way — it makes its own real,
        # unmocked GET to the Supervisor API when SUPERVISOR_TOKEN is set.
        self._base_url_patcher = patch("nibe_ha_integration._get_ha_base_url", return_value="")
        self.mock_notify = self._notify_patcher.start()
        self.mock_dismiss = self._dismiss_patcher.start()
        self._base_url_patcher.start()
        self.addCleanup(self._notify_patcher.stop)
        self.addCleanup(self._dismiss_patcher.stop)
        self.addCleanup(self._base_url_patcher.stop)

        # Create a fresh EntityManager with its own mock, then override its mqtt
        self.em = _make_em()
        self.mqtt = MagicMock()
        self.em.mqtt = self.mqtt  # <-- CRITICAL FIX: use same mock for EM

        self.publisher = MagicMock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.test_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        # Register the management handlers; they will use self.em.mqtt (our mock)
        ManagementCommandHandler(
            self.mqtt, self.em, self.publisher, self.executor, self.test_executor
        ).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=True)
        self.test_executor.shutdown(wait=True)

    def _get_handler(self):
        from nibe_mqtt_publisher import MgmtTopic

        topic = MgmtTopic.RUN_TESTS_PRESS
        for call in self.mqtt.message_callback_add.call_args_list:
            if call.args[0] == topic:
                return call.args[1]
        raise KeyError("No handler for RUN_TESTS_PRESS")

    def _msg(self, payload):
        m = MagicMock()
        m.payload = payload.encode()
        return m

    def test_subprocess_timeout(self):
        import subprocess

        mock_proc = MagicMock(pid=12345)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 3600),
            ("", ""),
        ]
        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg") as mock_killpg,
        ):
            handler = self._get_handler()
            handler(None, None, self._msg(""))
            self.test_executor.shutdown(wait=True)
            from nibe_mqtt_publisher import MgmtTopic

            states = [
                c.args[1]
                for c in self.mqtt.publish.call_args_list
                if c.args[0] == MgmtTopic.RUN_TESTS_STATE
            ]
            self.assertIn("timed_out", states)
            mock_killpg.assert_called_once_with(99999, signal.SIGKILL)

    def test_subprocess_generic_exception(self):
        with patch("subprocess.Popen", side_effect=Exception("permission denied")):
            handler = self._get_handler()
            handler(None, None, self._msg(""))
            self.test_executor.shutdown(wait=True)
            from nibe_mqtt_publisher import MgmtTopic

            states = [
                c.args[1]
                for c in self.mqtt.publish.call_args_list
                if c.args[0] == MgmtTopic.RUN_TESTS_STATE
            ]
            self.assertIn("error", states)


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
            t = payload.get("type")
            if t == "lovelace/dashboards/list":
                return {"success": True, "result": [{"url_path": "nibe-menus", "id": 99}]}
            if t == "lovelace/config/save":
                return {"success": save_success}
            return {"success": True, "result": []}

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
        import io

        import nibe_lovelace as nl

        call_order = []
        watcher = self._make_watcher()

        def tracking_open_ws():
            call_order.append("open_ws_fn")
            return (MagicMock(), iter(range(1, 100)).__next__)

        def tracking_build_config(*args, **kwargs):
            call_order.append("build_config")
            return {"views": [{"title": "Test"}]}

        with (
            patch("nibe_lovelace.os.path.exists", return_value=True),
            patch("builtins.open", return_value=io.StringIO(self._YAML)),
            patch("nibe_lovelace.time.sleep"),
            patch("nibe_lovelace._build_menu_dashboard_config", side_effect=tracking_build_config),
            patch("nibe_lovelace._setup_menu_dashboard_lovelace", return_value=False),
        ):
            nl._setup_menu_dashboard(tracking_open_ws, watcher)

        self.assertIn(
            "build_config", call_order, "_build_menu_dashboard_config must have been called"
        )
        self.assertIn("open_ws_fn", call_order, "open_ws_fn must have been called")
        build_idx = call_order.index("build_config")
        open_idx = call_order.index("open_ws_fn")
        self.assertGreater(
            open_idx,
            build_idx,
            f"open_ws_fn (position {open_idx}) must be called AFTER "
            f"_build_menu_dashboard_config (position {build_idx}), "
            f"not before the registry wait — order was: {call_order}",
        )

    def test_ws_open_failure_after_wait_returns_true_for_retry(self):
        """If open_ws_fn returns None after the registry wait, the function
        must return True (signal retry) rather than raising or returning False.
        This is the new coverage path: the ws open now happens inside
        _setup_menu_dashboard, after the registry wait."""
        import io

        import nibe_lovelace as nl

        watcher = self._make_watcher()
        with (
            patch("nibe_lovelace.os.path.exists", return_value=True),
            patch("builtins.open", return_value=io.StringIO(self._YAML)),
            patch("nibe_lovelace.time.sleep"),
            patch(
                "nibe_lovelace._build_menu_dashboard_config",
                return_value={"views": [{"title": "T"}]},
            ),
        ):
            result = nl._setup_menu_dashboard(lambda: None, watcher)
        self.assertIs(result, True)

    def test_ws_close_exception_in_finally_does_not_propagate(self):
        """ws.close() raising in the finally block must not propagate."""
        import io

        import nibe_lovelace as nl

        watcher = self._make_watcher()
        ws = MagicMock()
        ws.close.side_effect = OSError("already closed")
        next_id = iter(range(1, 100)).__next__
        with (
            patch("nibe_lovelace.os.path.exists", return_value=True),
            patch("builtins.open", return_value=io.StringIO(self._YAML)),
            patch("nibe_lovelace.time.sleep"),
            patch(
                "nibe_lovelace._build_menu_dashboard_config",
                return_value={"views": [{"title": "T"}]},
            ),
            patch("nibe_lovelace._setup_menu_dashboard_lovelace", return_value=False),
        ):
            # Must not raise even though ws.close() raises
            nl._setup_menu_dashboard(lambda: (ws, next_id), watcher)

    def test_registry_wait_timeout_logs_warning(self):
        """If the while loop exhausts _limit without stability, the else branch fires."""
        import io

        import nibe_lovelace as nl

        watcher = self._make_watcher()

        sleep_calls = [0]

        def fake_sleep(t):
            sleep_calls[0] += 1
            if sleep_calls[0] > 200:
                raise RuntimeError("infinite loop guard")

        # open_ws_fn returns a fresh connection after the wait
        open_ws_fn = MagicMock(return_value=(MagicMock(), iter(range(1, 100)).__next__))

        # entity_id_for always returns None → count never stabilises → timeout
        with (
            patch("nibe_lovelace.os.path.exists", return_value=True),
            patch("builtins.open", return_value=io.StringIO(self._YAML)),
            patch("nibe_lovelace._setup_menu_dashboard_lovelace", return_value=False),
            patch("nibe_lovelace.time.sleep", side_effect=fake_sleep),
            patch("nibe_lovelace.time.time", side_effect=lambda: sleep_calls[0] * 0.5),
            self.assertLogs("nibe.startup", level="WARNING") as cm,
        ):
            nl._setup_menu_dashboard(open_ws_fn, watcher)
        self.assertTrue(any("timed out" in msg for msg in cm.output))

    def test_no_views_generated_returns_false(self):
        """When _build_menu_dashboard_config returns no views, must return False."""
        import io

        import nibe_lovelace as nl

        watcher = self._make_watcher()
        open_ws_fn = MagicMock()
        with (
            patch("nibe_lovelace.os.path.exists", return_value=True),
            patch("builtins.open", return_value=io.StringIO(self._YAML)),
            patch("nibe_lovelace.time.sleep"),
            patch("nibe_lovelace._build_menu_dashboard_config", return_value={"views": []}),
        ):
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
            MagicMock(),
            debug_mode=False,
            attempt=1,
            open_ws_fn=open_ws_fn,
            setup_dashboard_fn=setup_dashboard_fn,
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
        w._registry_map_lock = threading.Lock()
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

    def test_recv_exception_returns_empty_dict(self):
        """If ws.recv raises (timeout / connection drop), return {}."""
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.side_effect = OSError("timed out")
        result = w._fetch_entity_registry(ws)
        self.assertEqual(result, {})
        ws.settimeout.assert_any_call(None)  # finally branch always resets timeout

    def test_failed_response_returns_empty_dict(self):
        """If the response arrives but success=False, return {}."""
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = json.dumps(
            {"id": 1, "type": "result", "success": False, "error": {"code": "unknown"}}
        )
        result = w._fetch_entity_registry(ws)
        self.assertEqual(result, {})


class TestRegistryWatcherStart(unittest.TestCase):
    """start(): no-token early return and normal thread-start path."""

    def _make_watcher(self):
        import threading

        from nibe_ha_integration import HAEntityRegistryWatcher

        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._registry_map_lock = threading.Lock()
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

    def test_no_supervisor_token_returns_without_starting_thread(self):
        w = self._make_watcher()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("threading.Thread") as mock_thread,
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            w.start()
        mock_thread.assert_not_called()
        self.assertIsNone(w._thread)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "No SUPERVISOR_TOKEN — entity registry watcher disabled "
                    "(running outside HA add-on environment)"
                )
                for msg in cm.output
            )
        )

    def test_supervisor_token_starts_daemon_thread(self):
        w = self._make_watcher()
        mock_thread = MagicMock()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("threading.Thread", return_value=mock_thread) as mock_thread_cls,
            self.assertLogs("nibe.registry", level="INFO") as cm,
        ):
            w.start()
        mock_thread.start.assert_called_once()
        self.assertIs(w._thread, mock_thread)
        mock_thread_cls.assert_called_once_with(
            target=w._run,
            name="nibe_registry_watcher",
            daemon=True,
        )
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Entity registry watcher started") for msg in cm.output
            )
        )


class TestRegistryWatcherStop(unittest.TestCase):
    """stop(): sets stop event, closes current ws, joins thread."""

    def _make_watcher(self):
        import threading

        from nibe_ha_integration import HAEntityRegistryWatcher

        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._registry_map_lock = threading.Lock()
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

    def test_stop_logs_stopped_message(self):
        w = self._make_watcher()
        with self.assertLogs("nibe.registry", level="DEBUG") as cm:
            w.stop()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Entity registry watcher stopped") for msg in cm.output
            )
        )

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
        w._registry_map_lock = threading.Lock()
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

    def _make_ws_mod(self, recv_sequence):
        ws = MagicMock()
        ws.recv.side_effect = [json.dumps(m) for m in recv_sequence]
        ws_mod = MagicMock()
        ws_mod.create_connection.return_value = ws
        return ws_mod, ws

    def test_wrong_greeting_type_closes_and_raises(self):
        w = self._make_watcher()
        ws_mod, ws = self._make_ws_mod([{"type": "auth_ok"}])  # wrong greeting
        with (
            patch.dict("sys.modules", {"websocket": ws_mod}),
            self.assertRaises(RuntimeError),
        ):
            w._connect_and_subscribe("tok")
        ws.close.assert_called_once()

    def test_auth_failure_closes_and_raises(self):
        w = self._make_watcher()
        ws_mod, ws = self._make_ws_mod(
            [
                {"type": "auth_required"},
                {"type": "auth_invalid"},  # auth failed
            ]
        )
        with (
            patch.dict("sys.modules", {"websocket": ws_mod}),
            self.assertRaises(RuntimeError),
        ):
            w._connect_and_subscribe("tok")
        ws.close.assert_called_once()

    def test_subscription_failure_closes_and_raises(self):
        w = self._make_watcher()
        sub_result = {"id": 1, "type": "result", "success": False}
        ws_mod, ws = self._make_ws_mod(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                sub_result,  # sub failed
            ]
        )
        with (
            patch.dict("sys.modules", {"websocket": ws_mod}),
            self.assertRaises(RuntimeError) as cm,
        ):
            w._connect_and_subscribe("tok")
        ws.close.assert_called_once()
        self.assertEqual(str(cm.exception), f"Event subscription failed: {sub_result}")

    def test_connects_authenticates_and_subscribes_with_real_arguments(self):
        """create_connection must use the real supervisor websocket URL and
        a 10s timeout, _ws_authenticate must receive the real token, and the
        subscribe request sent must use the real sub_id and event type —
        none of these are checked by the existing success/failure tests,
        which only assert on the outer return value / exception / close()."""
        w = self._make_watcher()
        # _ws_authenticate is mocked out, so it consumes no recv() calls —
        # the sequence here only needs to cover this function's own two
        # recv() calls: the subscribe result, then _fetch_entity_registry's.
        ws_mod, ws = self._make_ws_mod(
            [
                {"id": 1, "type": "result", "success": True},
                {"id": 2, "type": "result", "success": True, "result": []},
            ]
        )
        with (
            patch.dict("sys.modules", {"websocket": ws_mod}),
            patch.object(w, "_ws_authenticate") as mock_auth,
        ):
            w._connect_and_subscribe("real-supervisor-token")

        ws_mod.create_connection.assert_called_once_with(
            "ws://supervisor/core/websocket",
            timeout=10,
        )
        mock_auth.assert_called_once_with(ws, "real-supervisor-token")
        sent_payload = json.loads(ws.send.call_args_list[0].args[0])
        self.assertEqual(
            sent_payload,
            {
                "id": 1,
                "type": "subscribe_events",
                "event_type": "entity_registry_updated",
            },
        )

    def test_success_returns_ws_and_sets_timeout(self):
        w = self._make_watcher()
        ws_mod, ws = self._make_ws_mod(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": True},  # sub OK
                # _fetch_entity_registry will call recv once more
                {"id": 2, "type": "result", "success": True, "result": []},
            ]
        )
        with (
            patch.dict("sys.modules", {"websocket": ws_mod}),
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            result = w._connect_and_subscribe("tok")
        self.assertIs(result, ws)
        from nibe_ha_integration import HAEntityRegistryWatcher

        # assert_any_call(30) would pass trivially even if this specific
        # call were mutated: _fetch_entity_registry (called internally,
        # just before this line runs) also calls ws.settimeout(30) for an
        # unrelated reason, and _PING_INTERVAL_S's real value (30) happens
        # to match. The LAST call is the one this line actually makes —
        # _fetch_entity_registry's own settimeout(30)/settimeout(None) both
        # happen earlier, inside its try/finally.
        ws.settimeout.assert_called_with(HAEntityRegistryWatcher._PING_INTERVAL_S)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "WebSocket connected and subscribed to entity_registry_updated events"
                )
                for msg in cm.output
            )
        )

    def test_success_updates_unique_id_map_from_fetch_entity_registry(self):
        """_unique_id_map must be set to _fetch_entity_registry()'s actual
        return value, not left as None/unset — patch it directly so the
        assertion is independent of the real registry-fetch parsing logic."""
        w = self._make_watcher()
        ws_mod, _ws = self._make_ws_mod(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": True},  # sub OK
            ]
        )
        sentinel_map = {"sensor.x": "unique_id_x"}
        with (
            patch.dict("sys.modules", {"websocket": ws_mod}),
            patch.object(w, "_fetch_entity_registry", return_value=sentinel_map),
        ):
            w._connect_and_subscribe("tok")
        self.assertEqual(w._unique_id_map, sentinel_map)


class TestRegistryWatcherPingPong(unittest.TestCase):
    """WebSocket keepalive: ping sent on recv timeout, reconnect if no pong."""

    def _make_watcher(self):
        import threading

        from nibe_ha_integration import HAEntityRegistryWatcher

        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._registry_map_lock = threading.Lock()
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
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

        with (
            patch(
                "nibe_ha_integration.HAEntityRegistryWatcher._connect_and_subscribe",
                return_value=ws,
            ),
            patch(
                "nibe_ha_integration.HAEntityRegistryWatcher._fetch_entity_registry",
                return_value={},
            ),
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
        ):
            w._run()

        # Confirm a ping was sent (not a reconnect)
        self.assertTrue(ws.send.called)
        sent = json.loads(ws.send.call_args[0][0])
        self.assertEqual(sent.get("type"), "ping")

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

        with (
            patch(
                "nibe_ha_integration.HAEntityRegistryWatcher._connect_and_subscribe",
                return_value=ws,
            ),
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_handle_event") as mock_event,
        ):
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

        real_time = __import__("time").time

        call_count = [0]

        def fake_time():
            call_count[0] += 1
            # First call: return a timestamp far past the pong timeout
            # so reconnect is triggered immediately
            if call_count[0] <= 1:
                from nibe_ha_integration import HAEntityRegistryWatcher

                return real_time() - HAEntityRegistryWatcher._PING_TIMEOUT_S - 5
            return real_time()

        with (
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_ha_integration.time.time", side_effect=fake_time),
            patch(
                "builtins.__import__",
                side_effect=lambda name, *a, **kw: (
                    (_ for _ in ()).throw(ImportError("no websocket"))
                    if name == "websocket"
                    else __import__(name, *a, **kw)
                ),
            ),
        ):
            w._run()

        self.assertGreaterEqual(
            connect_count[0],
            2,
            "ImportError fallback must still trigger reconnect on keepalive timeout",
        )

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
        real_time = __import__("time").time

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

        with (
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_ha_integration.time.time", side_effect=fake_time),
        ):
            w._run()

        self.assertGreaterEqual(connect_count[0], 2, "Keepalive timeout must trigger reconnect")


class TestRegistryWatcherRun(unittest.TestCase):
    """_run(): all exit paths and inner-loop branches."""

    def _make_watcher(self):
        import threading

        from nibe_ha_integration import HAEntityRegistryWatcher

        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._registry_map_lock = threading.Lock()
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

    def test_thread_exiting_log_has_exact_text(self):
        w = self._make_watcher()
        w._stop_event.set()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            w._run()
        self.assertTrue(
            any(
                msg.splitlines()[0] == "DEBUG:nibe.registry:Registry watcher thread exiting"
                for msg in cm.output
            )
        )

    def test_missing_supervisor_token_passes_real_empty_string_default(self):
        """os.environ.get('SUPERVISOR_TOKEN', '') must default to '' — not
        None or a truthy placeholder — when the env var is unset. _run()
        itself has no early-return guard for a missing token (unlike
        start()/refresh_registry(), which do); it always calls
        _connect_and_subscribe(token) regardless, so a truthy garbage
        default here would send bogus auth credentials over the WebSocket
        instead of the real, honest empty string."""
        w = self._make_watcher()
        captured = []

        def fake_connect(token):
            captured.append(token)
            w._stop_event.set()
            raise RuntimeError("stop probing")

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.object(w._stop_event, "wait"),
        ):
            w._run()
        self.assertEqual(captured, [""])

    def test_stop_event_set_before_run_exits_immediately(self):
        """If stop_event is already set, the while loop body never executes."""
        w = self._make_watcher()
        w._stop_event.set()
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}):
            w._run()  # must return without calling _connect_and_subscribe

    def test_import_error_returns_without_retry(self):
        """If websocket-client is missing, _run logs and returns — no retry."""
        w = self._make_watcher()
        call_count = [0]

        def fake_connect(_token):
            call_count[0] += 1
            raise ImportError("no module named websocket")

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
        ):
            w._run()
        self.assertEqual(call_count[0], 1)  # tried exactly once, then gave up

    def test_consecutive_failures_give_up_after_max(self):
        """After _MAX_CONSEC_FAILURES consecutive exceptions, _run returns."""
        w = self._make_watcher()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(
                w, "_connect_and_subscribe", side_effect=RuntimeError("connection refused")
            ),
            patch.object(w._stop_event, "wait"),
        ):  # skip real sleep
            w._run()
        # after MAX_CONSEC_FAILURES=10 attempts it returns; stop_event not set
        self.assertFalse(w._stop_event.is_set())

    def test_empty_recv_raises_with_exact_connection_error_text(self):
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = ""
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch.object(w._stop_event, "wait") as mock_wait,
            self.assertLogs("nibe.registry", level="WARNING") as cm,
        ):

            def fake_wait(timeout=None):
                w._stop_event.set()

            mock_wait.side_effect = fake_wait
            w._run()
        self.assertTrue(any("WebSocket closed by server (empty recv)" in msg for msg in cm.output))

    def test_malformed_frame_logged_with_exact_debug_text(self):
        w = self._make_watcher()
        ws = MagicMock()
        call_count = [0]

        def fake_recv():
            call_count[0] += 1
            if call_count[0] == 1:
                return "NOT_JSON {{{"
            w._stop_event.set()
            return json.dumps({"type": "pong"})

        ws.recv.side_effect = fake_recv
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            w._run()
        self.assertTrue(
            any(
                msg.splitlines()[0].startswith(
                    "DEBUG:nibe.registry:Registry watcher: discarding malformed frame: "
                )
                for msg in cm.output
            )
        )

    def test_malformed_frame_debug_call_has_exact_format_and_real_exception(self):
        """Same scenario, but asserting the mocked log call's exact args
        directly — the sibling test above only checks a startswith prefix,
        which can't distinguish the real exception object from a
        dropped/None arg, since both produce a message with that same
        prefix once formatted into text."""
        w = self._make_watcher()
        ws = MagicMock()
        call_count = [0]

        def fake_recv():
            call_count[0] += 1
            if call_count[0] == 1:
                return "NOT_JSON {{{"
            w._stop_event.set()
            return json.dumps({"type": "pong"})

        ws.recv.side_effect = fake_recv
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch("nibe_ha_integration.log_registry") as mock_log,
        ):
            w._run()
        debug_call = next(
            c
            for c in mock_log.debug.call_args_list
            if c.args[0] == "Registry watcher: discarding malformed frame: %s"
        )
        self.assertIsInstance(debug_call.args[1], json.JSONDecodeError)

    def test_empty_recv_error_message_has_exact_text(self):
        """The sibling `_in_` test above can't distinguish the real text
        from an XX-wrapped mutant, since 'text' is still a substring of
        'XXtextXX' — an exact equality check on the captured exception is
        needed instead."""
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = ""

        def fake_connect(_token):
            return ws

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.object(w._stop_event, "wait") as mock_wait,
            patch("nibe_ha_integration.log_registry") as mock_log,
        ):

            def fake_wait(timeout=None):
                w._stop_event.set()

            mock_wait.side_effect = fake_wait
            w._run()
        # "Registry watcher disconnected (%s) — reconnecting in %ds (failure %d/%d)"
        warning_call = mock_log.warning.call_args
        caught_exc = warning_call.args[1]
        self.assertEqual(str(caught_exc), "WebSocket closed by server (empty recv)")

    def test_import_error_logged_with_exact_warning_text(self):
        w = self._make_watcher()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=ImportError("no module")),
            self.assertLogs("nibe.registry", level="WARNING") as cm,
        ):
            w._run()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "websocket-client not installed — entity registry watcher cannot run. "
                    "Add 'websocket-client' to requirements.txt."
                )
                for msg in cm.output
            )
        )

    def test_give_up_logged_with_exact_warning_text_and_count(self):
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = self._make_watcher()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=RuntimeError("refused")),
            patch.object(w._stop_event, "wait"),
            self.assertLogs("nibe.registry", level="WARNING") as cm,
        ):
            w._run()
        expected = (
            f"Registry watcher: {HAEntityRegistryWatcher._MAX_CONSEC_FAILURES} "
            "consecutive failures — giving up. HA-side entity enable/disable "
            "events will not be detected."
        )
        self.assertTrue(any(msg.splitlines()[0].endswith(expected) for msg in cm.output))

    def test_reconnect_warning_includes_the_real_exception_text(self):
        w = self._make_watcher()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(
                w,
                "_connect_and_subscribe",
                side_effect=[RuntimeError("specific reason"), RuntimeError("stop")],
            ),
            patch.object(w._stop_event, "wait") as mock_wait,
            self.assertLogs("nibe.registry", level="WARNING") as cm,
        ):

            def fake_wait(timeout=None):
                w._stop_event.set()

            mock_wait.side_effect = fake_wait
            w._run()
        self.assertTrue(
            any(
                "Registry watcher disconnected (specific reason) — reconnecting in " in msg
                and "(failure 1/10)" in msg
                for msg in cm.output
            )
        )

    def test_reconnect_warning_has_exact_text_and_real_args(self):
        """The sibling test above uses `in`, which can't distinguish the
        real text from an XX-wrapped mutant ('text' is still a substring
        of 'XXtextXX') — assert on the mocked call's exact args instead."""
        w = self._make_watcher()
        real_err = RuntimeError("specific reason")
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=[real_err, RuntimeError("stop")]),
            patch.object(w._stop_event, "wait") as mock_wait,
            patch("nibe_ha_integration.log_registry") as mock_log,
        ):

            def fake_wait(timeout=None):
                w._stop_event.set()

            mock_wait.side_effect = fake_wait
            w._run()
        mock_log.warning.assert_called_once_with(
            "Registry watcher disconnected (%s) — reconnecting in %ds (failure %d/%d)",
            real_err,
            w._INITIAL_BACKOFF,
            1,
            w._MAX_CONSEC_FAILURES,
        )

    def test_handle_event_error_warning_has_exact_text_and_real_exception(self):
        w = self._make_watcher()
        ws = MagicMock()
        real_err = RuntimeError("handler exploded")

        def fake_recv():
            w._stop_event.set()
            return json.dumps({"type": "event", "event": {}})

        ws.recv.side_effect = fake_recv
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch.object(w, "_handle_event", side_effect=real_err),
            patch("nibe_ha_integration.log_registry") as mock_log,
        ):
            w._run()
        mock_log.warning.assert_called_once_with(
            "Error handling registry event: %s",
            real_err,
            exc_info=True,
        )

    def test_ws_close_exception_in_finally_is_swallowed(self):
        """ws.close() raising during teardown must not propagate — it's
        wrapped in a deliberate except Exception: pass since we're already
        tearing down and there's nothing useful to do with a close error."""
        w = self._make_watcher()
        ws = MagicMock()
        ws.close.side_effect = RuntimeError("socket already closed")
        ws.recv.side_effect = lambda: (w._stop_event.set(), "")[1]  # exit loop on first recv
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
        ):
            w._run()  # must not raise despite ws.close() failing
        ws.close.assert_called_once()

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
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.object(w._stop_event, "wait"),
        ):  # skip real sleep
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
                return ""  # empty → break inner loop
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

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.object(w._stop_event, "wait"),
        ):
            w._run()
        self.assertEqual(inner_call[0], 1)  # recv called once before empty break

    def test_inner_loop_empty_recv_goes_through_backoff_and_failure_count(self):
        """Regression: an empty recv() (server-initiated clean close) must be
        treated like any other disconnect — going through the except
        Exception branch's backoff wait and consec_failures increment — not
        a bare `break` straight to the outer loop's immediate reconnect.
        Without this, a proxy that closes cleanly on every attempt (restart
        loop, rate-limiting, HA Core not yet ready at startup) spins in a
        zero-delay reconnect loop that can never trip _MAX_CONSEC_FAILURES
        and give up."""
        w = self._make_watcher()
        ws = MagicMock()
        ws.recv.return_value = ""  # every connect's recv immediately closes
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch.object(w._stop_event, "wait") as mock_wait,
        ):
            # Stop after a few reconnect attempts rather than running until
            # _MAX_CONSEC_FAILURES (10) — set the stop event once we've seen
            # enough backoff waits to prove the throttle is engaged.
            def fake_wait(timeout=None):
                if mock_wait.call_count >= 3:
                    w._stop_event.set()

            mock_wait.side_effect = fake_wait
            w._run()
        self.assertGreaterEqual(
            mock_wait.call_count,
            3,
            "empty recv() must trigger the backoff wait on every disconnect, "
            "not bypass it via a bare break",
        )

    def test_inner_loop_invalid_json_continues(self):
        """Unparseable JSON is silently skipped; loop continues."""
        w = self._make_watcher()
        ws = MagicMock()
        recv_calls = [0]

        def fake_recv():
            recv_calls[0] += 1
            if recv_calls[0] == 1:
                return "NOT_JSON"  # parse error → continue
            w._stop_event.set()
            return json.dumps({"type": "event", "event": {}})

        ws.recv.side_effect = fake_recv
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
        ):
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
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch.object(w, "_handle_event", side_effect=handled.append),
        ):
            w._run()
        self.assertEqual(len(handled), 1)

    def test_stop_event_set_during_exception_breaks_cleanly(self):
        """If stop_event is set before exception is caught, _run breaks without retry."""
        w = self._make_watcher()

        def fake_connect(_token):
            w._stop_event.set()
            raise RuntimeError("shutting down")

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
        ):
            w._run()  # must return without scheduling retry

    def test_stop_event_set_during_exception_still_reaches_exit_log(self):
        """The stop_event-set branch inside the except handler must `break`
        out of the while loop (falling through to the trailing 'thread
        exiting' debug log just after it), not `return` early and skip
        that log line — a mutated `return` here would still pass every
        other _run test since none of them assert on this final log."""
        w = self._make_watcher()

        def fake_connect(_token):
            w._stop_event.set()
            raise RuntimeError("shutting down")

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            w._run()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Registry watcher thread exiting") for msg in cm.output
            )
        )

    def test_token_defaults_to_empty_string_when_env_var_unset(self):
        w = self._make_watcher()

        def fake_connect(_token):
            w._stop_event.set()
            raise RuntimeError("boom")

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect) as mock_connect,
        ):
            w._run()
        mock_connect.assert_called_once_with("")

    def test_current_ws_is_set_to_the_real_connected_ws_object(self):
        """_current_ws must be set to the actual ws returned by
        _connect_and_subscribe — not None/some other object — so stop()
        can close the right socket."""
        w = self._make_watcher()
        ws = MagicMock()
        seen = []

        def fake_recv():
            seen.append(w._current_ws)
            w._stop_event.set()
            return ""

        ws.recv.side_effect = fake_recv
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
        ):
            w._run()
        self.assertEqual(seen, [ws])

    def test_consec_failures_resets_to_zero_on_reconnect_then_counts_again(self):
        """After a successful reconnect, consec_failures must reset to
        exactly 0 — not some other fixed value. Pins the EXACT number of
        post-reset failures tolerated before give-up (_MAX_CONSEC_FAILURES,
        i.e. 10): a reset to 1 instead of 0 would give up one attempt
        early, which a loose '>= budget' check wouldn't catch."""
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = self._make_watcher()
        ws = MagicMock()
        attempt = [0]

        def fake_connect(_token):
            attempt[0] += 1
            if attempt[0] == 1:
                raise RuntimeError("first failure")
            if attempt[0] == 2:
                return ws  # succeeds — must reset consec_failures to 0
            raise RuntimeError(f"failure after reset #{attempt[0]}")

        ws.recv.return_value = ""  # immediately disconnects after the success
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.object(w._stop_event, "wait"),
        ):
            w._run()
        # Attempt 1: fails (consec_failures 0->1).
        # Attempt 2: connect succeeds (resets consec_failures to 0), but its
        # immediate empty recv() is itself a failure, bumping 0->1 again.
        # Attempts 3..11 (9 more): each a fresh connect failure, bumping
        # 1->10, at which point the give-up path returns without attempt 12.
        self.assertEqual(attempt[0], 1 + HAEntityRegistryWatcher._MAX_CONSEC_FAILURES)

    def test_ping_timeout_uses_elapsed_time_not_sum(self):
        """The keepalive check must be `now - ping_sent_at` (elapsed time
        since the ping), not `now + ping_sent_at` — with real epoch
        timestamps, a `+` here would make the check always true (the sum
        of two large epoch values always exceeds _PING_TIMEOUT_S), forcing
        a reconnect on literally the next recv after any ping is sent,
        even though no time has actually elapsed."""
        w = self._make_watcher()
        ws = MagicMock()
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            WebSocketTimeoutException = TimeoutError

        call_count = [0]

        def fake_recv():
            call_count[0] += 1
            if call_count[0] <= 2:
                raise WebSocketTimeoutException("timeout")
            w._stop_event.set()
            raise WebSocketTimeoutException("stop")

        ws.recv.side_effect = fake_recv
        ws.send = MagicMock()
        connect_count = [0]

        def fake_connect(_token):
            connect_count[0] += 1
            return ws

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
        ):
            w._run()
        # A real elapsed-time check never trips the keepalive-timeout
        # reconnect here (pings are sent back-to-back, no real delay) —
        # only one connection should ever have been made.
        self.assertEqual(connect_count[0], 1)

    def test_ping_payload_uses_id_key(self):
        w = self._make_watcher()
        ws = MagicMock()
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            WebSocketTimeoutException = TimeoutError

        def fake_recv():
            w._stop_event.set()
            raise WebSocketTimeoutException("timeout")

        ws.recv.side_effect = fake_recv
        ws.send = MagicMock()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
        ):
            w._run()
        sent = json.loads(ws.send.call_args.args[0])
        self.assertIn("id", sent)
        self.assertEqual(sent["type"], "ping")

    def test_keepalive_timeout_boundary_exactly_at_limit_does_not_reconnect(self):
        """The keepalive check is `elapsed > _PING_TIMEOUT_S` (strictly
        greater), so elapsed == _PING_TIMEOUT_S exactly must NOT trigger a
        reconnect — a `>=` mutant would reconnect one instant too early."""
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = self._make_watcher()
        ws = MagicMock()
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            WebSocketTimeoutException = TimeoutError

        real_time = __import__("time").time
        base = real_time()
        call_count = [0]

        def fake_time():
            call_count[0] += 1
            # 1st call: store ping_sent_at = base. 2nd call: check elapsed —
            # return exactly base + _PING_TIMEOUT_S (elapsed == limit).
            if call_count[0] == 1:
                return base
            if call_count[0] == 2:
                return base + HAEntityRegistryWatcher._PING_TIMEOUT_S
            w._stop_event.set()
            return real_time()

        recv_count = [0]

        def fake_recv():
            recv_count[0] += 1
            if recv_count[0] <= 2:
                raise WebSocketTimeoutException("timeout")
            w._stop_event.set()
            raise WebSocketTimeoutException("stop")

        ws.recv.side_effect = fake_recv
        ws.send = MagicMock()
        connect_count = [0]

        def fake_connect(_token):
            connect_count[0] += 1
            return ws

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch("nibe_ha_integration.time.time", side_effect=fake_time),
        ):
            w._run()
        self.assertEqual(connect_count[0], 1, "exactly-at-limit must not reconnect")

    def test_keepalive_timeout_past_limit_reconnects_with_exact_error_text(self):
        """One instant past _PING_TIMEOUT_S must reconnect, and the
        ConnectionError's message — surfaced via the 'Registry watcher
        disconnected (%s)' warning — must be the real, exact text, not
        dropped/None."""
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = self._make_watcher()
        ws = MagicMock()
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            WebSocketTimeoutException = TimeoutError

        real_time = __import__("time").time
        base = real_time()
        call_count = [0]

        def fake_time():
            call_count[0] += 1
            if call_count[0] == 1:
                return base
            if call_count[0] == 2:
                return base + HAEntityRegistryWatcher._PING_TIMEOUT_S + 0.001
            w._stop_event.set()
            return real_time()

        def fake_recv():
            raise WebSocketTimeoutException("timeout")

        ws.recv.side_effect = fake_recv
        ws.send = MagicMock()

        def fake_connect(_token):
            return ws

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch("nibe_ha_integration.time.time", side_effect=fake_time),
            self.assertLogs("nibe.registry", level="WARNING") as cm,
        ):
            w._run()
        expected = (
            f"WebSocket keepalive timeout — no pong received "
            f"in {HAEntityRegistryWatcher._PING_TIMEOUT_S}s after ping"
        )
        self.assertTrue(any(expected in msg for msg in cm.output))

    def test_ping_timeout_branch_continues_not_breaks_inner_loop(self):
        """After sending a keepalive ping on recv timeout, the inner recv
        loop must `continue` (go straight back to ws.recv()) — not `break`
        out to the outer loop, which would tear down and reconnect the
        whole WebSocket unnecessarily on every single ping."""
        w = self._make_watcher()
        ws = MagicMock()
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            WebSocketTimeoutException = TimeoutError

        recv_count = [0]

        def fake_recv():
            recv_count[0] += 1
            if recv_count[0] >= 3:
                w._stop_event.set()
            raise WebSocketTimeoutException("timeout")

        ws.recv.side_effect = fake_recv
        ws.send = MagicMock()
        connect_count = [0]

        def fake_connect(_token):
            connect_count[0] += 1
            return ws

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
        ):
            w._run()
        self.assertEqual(
            connect_count[0],
            1,
            "repeated ping timeouts must not force a reconnect each time",
        )
        self.assertGreaterEqual(recv_count[0], 3)

    def test_ping_sent_at_reset_to_zero_after_any_non_timeout_recv(self):
        """Any successfully received frame (not just pong) must reset
        ping_sent_at back to 0.0 — not None or a nonzero placeholder —
        clearing the in-flight-ping state so the next timeout starts a
        fresh keepalive cycle rather than immediately looking stale."""
        w = self._make_watcher()
        ws = MagicMock()
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            WebSocketTimeoutException = TimeoutError

        call_count = [0]

        def fake_recv():
            call_count[0] += 1
            if call_count[0] == 1:
                raise WebSocketTimeoutException("timeout")  # sends a ping
            if call_count[0] == 2:
                return json.dumps({"type": "pong"})  # resets ping_sent_at
            if call_count[0] == 3:
                # A second timeout right after the pong: if ping_sent_at
                # wasn't reset to a falsy 0.0, `ping_sent_at > 0` would be
                # true here and (with real elapsed time near 0) still not
                # raise — so this alone can't distinguish 0.0 from a huge
                # leftover value staying > 0 without also being "elapsed".
                # What it DOES prove: no crash/TypeError from a None
                # ping_sent_at flowing into the `now - ping_sent_at` subtraction.
                w._stop_event.set()
                raise WebSocketTimeoutException("timeout2")
            raise WebSocketTimeoutException("stop")

        ws.recv.side_effect = fake_recv
        ws.send = MagicMock()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
        ):
            w._run()  # must not raise (TypeError if ping_sent_at were None)
        self.assertGreaterEqual(ws.send.call_count, 2)

    def test_invalid_json_frame_continues_not_breaks_inner_loop(self):
        """A single malformed frame must not tear down and reconnect the
        whole WebSocket — the inner loop must `continue`, not `break`."""
        w = self._make_watcher()
        ws = MagicMock()
        recv_count = [0]

        def fake_recv():
            recv_count[0] += 1
            if recv_count[0] >= 3:
                w._stop_event.set()
            return "NOT_JSON"

        ws.recv.side_effect = fake_recv
        connect_count = [0]

        def fake_connect(_token):
            connect_count[0] += 1
            return ws

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
        ):
            w._run()
        self.assertEqual(connect_count[0], 1)
        self.assertGreaterEqual(recv_count[0], 3)

    def test_pong_message_continues_not_breaks_inner_loop(self):
        """Receiving a pong must not tear down and reconnect the whole
        WebSocket — the inner loop must `continue`, not `break`."""
        w = self._make_watcher()
        ws = MagicMock()
        recv_count = [0]

        def fake_recv():
            recv_count[0] += 1
            if recv_count[0] >= 3:
                w._stop_event.set()
            return json.dumps({"type": "pong"})

        ws.recv.side_effect = fake_recv
        connect_count = [0]

        def fake_connect(_token):
            connect_count[0] += 1
            return ws

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
        ):
            w._run()
        self.assertEqual(connect_count[0], 1)
        self.assertGreaterEqual(recv_count[0], 3)

    def test_handle_event_error_logs_with_traceback_info(self):
        """A raised exception inside _handle_event must be logged with
        exc_info=True (real traceback attached) — not False, which would
        silently drop the stack trace needed to actually debug it."""
        w = self._make_watcher()
        ws = MagicMock()

        def fake_recv():
            w._stop_event.set()
            return json.dumps({"type": "event", "event": {}})

        ws.recv.side_effect = fake_recv
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch.object(w, "_handle_event", side_effect=RuntimeError("boom")),
            patch("nibe_ha_integration.log_registry") as mock_log,
        ):
            w._run()
        mock_log.warning.assert_called_once()
        self.assertTrue(mock_log.warning.call_args.kwargs.get("exc_info"))

    def test_consec_failures_increments_by_one_per_failure(self):
        """consec_failures must increment by exactly 1 per failed connect
        attempt — pins the exact operator (+=1), not e.g. a fixed
        reassignment or decrement, by checking the reported count in the
        reconnect warning across two consecutive failures."""
        w = self._make_watcher()
        attempt = [0]

        def fake_connect(_token):
            attempt[0] += 1
            if attempt[0] >= 3:
                w._stop_event.set()
            raise RuntimeError(f"failure {attempt[0]}")

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.object(w._stop_event, "wait"),
            self.assertLogs("nibe.registry", level="WARNING") as cm,
        ):
            w._run()
        # Second reconnect warning must report "failure 2/10" — a `=1`
        # mutation would report 1/10 again; a `-=1` would report -1/10.
        self.assertTrue(any("failure 2/10" in msg for msg in cm.output))

    def test_handle_event_receives_empty_dict_default_when_event_key_absent(self):
        """msg.get('event', {}) must default to an empty dict — not None —
        when the 'event' key is entirely absent from an event-type message,
        or _handle_event(None) would crash on its own .get() calls."""
        w = self._make_watcher()
        ws = MagicMock()

        def fake_recv():
            w._stop_event.set()
            return json.dumps({"type": "event"})  # no 'event' key

        ws.recv.side_effect = fake_recv
        received = []
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch.object(w, "_handle_event", side_effect=received.append),
        ):
            w._run()
        self.assertEqual(received, [{}])


class TestUpdateDeviceModesWrapper(unittest.TestCase):
    """update_device_modes() is a thin public wrapper — delegates to _publish_device_modes."""

    def test_delegates_to_publish_device_modes(self):
        from nibe_ha_integration import update_device_modes

        em = MagicMock()
        pub = MagicMock()
        with patch("nibe_ha_integration._publish_device_modes") as mock_fn:
            update_device_modes(em, pub)
        mock_fn.assert_called_once_with(em, pub)


class TestPublishDynamicChangesDashboardNotificationException(unittest.TestCase):
    """Exception in dashboard notification block is silently logged."""

    def test_notify_exception_does_not_raise(self):
        em = _make_em()
        em.initial_discovery_complete = True
        point_id = 6666
        em.all_points_by_id[point_id] = {
            "variableId": point_id,
            "display_title": "Point 6666",
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
        with (
            patch("nibe_ha_integration.notify_ha", side_effect=RuntimeError("boom")),
            patch.object(em, "publish_enabled_state"),
            patch.object(em, "disable_entity"),
            patch.object(em, "_persist_active_dynamic"),
        ):
            em._publish_dynamic_changes([], {point_id})  # must not raise


class TestHandleEventExceptionIsolation(unittest.TestCase):
    """Exceptions inside _handle_event must not propagate out of _run()'s
    inner recv loop — they should be caught and logged, not trigger a reconnect."""

    def _make_watcher(self):
        import threading

        from nibe_ha_integration import HAEntityRegistryWatcher

        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._registry_map_lock = threading.Lock()
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
            return json.dumps({"type": "pong"})  # non-event, loop exits via stop

        ws.recv.side_effect = fake_recv

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch.object(w, "_handle_event", side_effect=bad_handle_event),
        ):
            w._run()

        # _handle_event was called, and despite the exception, _run exited
        # cleanly (stop_event set) rather than treating it as a connection error.
        self.assertEqual(events_processed[0], 1)
        # stop_event is set → run exited normally, not via exception path
        self.assertTrue(w._stop_event.is_set())

    def test_handle_event_receives_the_real_event_payload(self):
        """_handle_event must be called with the actual msg['event'] dict —
        not None or an empty placeholder. The exception-swallowing test
        above only checks it was called, never with what."""
        w = self._make_watcher()
        captured = []
        real_event = {"data": {"entity_id": "sensor.nibe_100"}, "event_type": "x"}

        def fake_recv():
            w._stop_event.set()
            return json.dumps({"type": "event", "event": real_event})

        ws = MagicMock()
        ws.recv.side_effect = fake_recv

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch.object(w, "_handle_event", side_effect=lambda e: captured.append(e)),
        ):
            w._run()

        self.assertEqual(captured, [real_event])

    def test_connect_and_subscribe_called_with_real_env_token(self):
        """_run must pass the actual SUPERVISOR_TOKEN env value through to
        _connect_and_subscribe — not None or a placeholder."""
        w = self._make_watcher()
        captured = []

        def fake_connect(token):
            captured.append(token)
            raise RuntimeError("stop here")

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "the-real-token"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.object(w._stop_event, "wait"),
        ):
            w._run()
        self.assertTrue(captured)
        self.assertTrue(all(tok == "the-real-token" for tok in captured))

    def test_backoff_doubles_across_consecutive_failures(self):
        """backoff must grow (double, capped at _MAX_BACKOFF) after each
        consecutive failure — not shrink or stay constant. Verified via the
        timeout values passed to _stop_event.wait()."""
        w = self._make_watcher()
        waits = []

        def fake_wait(timeout=None):
            waits.append(timeout)
            if len(waits) >= 3:
                w._stop_event.set()

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(
                w, "_connect_and_subscribe", side_effect=RuntimeError("connection refused")
            ),
            patch.object(w._stop_event, "wait", side_effect=fake_wait),
        ):
            w._run()
        # _INITIAL_BACKOFF=2, doubling: 2, 4, 8, ...
        self.assertEqual(waits, [2, 4, 8])

    def test_backoff_sequence_caps_at_max_backoff_before_giving_up(self):
        """The doubling-with-cap formula (backoff = min(backoff*2,
        _MAX_BACKOFF)) is only exercised up to 8s by the 3-step doubling
        test above — it never reaches _MAX_BACKOFF=300. With
        _INITIAL_BACKOFF=2, the cap is first hit on the 8th wait (256*2=512
        > 300), so run the watcher through all 10 failures (the real
        give-up threshold) and check every wait against a formula computed
        independently of the code under test, not read back from it."""
        w = self._make_watcher()
        waits = []
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(
                w, "_connect_and_subscribe", side_effect=RuntimeError("connection refused")
            ),
            patch.object(
                w._stop_event, "wait", side_effect=lambda timeout=None: waits.append(timeout)
            ),
        ):
            w._run()
        expected = [min(2 * (2**i), 300) for i in range(9)]
        self.assertEqual(waits, expected)
        self.assertEqual(waits[-1], 300, "backoff must be capped, not left to grow unbounded")

    def test_gives_up_at_exactly_max_consec_failures_not_one_before(self):
        """The give-up threshold is '>=' _MAX_CONSEC_FAILURES (10) — must
        NOT give up after only 9 failures, and MUST give up at exactly 10."""
        w = self._make_watcher()
        attempts = []

        def fake_connect(_token):
            attempts.append(1)
            raise RuntimeError("connection refused")

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.object(w, "_connect_and_subscribe", side_effect=fake_connect),
            patch.object(w._stop_event, "wait"),
        ):
            w._run()
        self.assertEqual(len(attempts), 10)


class TestRefreshRegistryAuthHandshake(unittest.TestCase):
    """refresh_registry: correct auth order (recv auth_required first),
    auth failure detection, dead header arg removed."""

    def _make_watcher(self):
        import threading

        from nibe_ha_integration import HAEntityRegistryWatcher

        w = object.__new__(HAEntityRegistryWatcher)
        w._unique_id_map = {}
        w._registry_map_lock = threading.Lock()
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
        ws_mod, ws = self._make_ws([{"type": "auth_ok"}])  # wrong first message
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.dict("sys.modules", {"websocket": ws_mod}),
        ):
            w.refresh_registry()
        ws.close.assert_called_once()
        # Auth should never have been sent
        send_calls = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        auth_sends = [c for c in send_calls if c.get("type") == "auth"]
        self.assertEqual(auth_sends, [])

    def test_auth_failure_closes_and_returns(self):
        """If auth is rejected, close and return rather than sending a registry
        request that would be silently ignored by the server."""
        w = self._make_watcher()
        ws_mod, ws = self._make_ws(
            [
                {"type": "auth_required"},
                {"type": "auth_invalid"},
            ]
        )
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.dict("sys.modules", {"websocket": ws_mod}),
        ):
            w.refresh_registry()
        ws.close.assert_called_once()
        # Registry request must not have been sent after auth failure
        send_calls = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        registry_sends = [c for c in send_calls if c.get("type") == "config/entity_registry/list"]
        self.assertEqual(registry_sends, [])

    def test_post_auth_fetch_failure_still_closes_ws(self):
        """Regression: a failure after successful auth (send/recv/json.loads
        raising — a network hiccup, timeout, or malformed frame from the
        Supervisor's WebSocket proxy) must still close the socket. Before the
        fix, ws.close() only ran after send/recv/json.loads succeeded, so
        any exception in that fetch step leaked the connection — the outer
        except caught the error but never reached the close() call."""
        w = self._make_watcher()
        ws_mod, ws = self._make_ws([{"type": "auth_required"}, {"type": "auth_ok"}])
        ws.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
            RuntimeError("connection reset mid-fetch"),
        ]
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.dict("sys.modules", {"websocket": ws_mod}),
        ):
            w.refresh_registry()  # must not raise
        ws.close.assert_called_once()

    def test_auth_required_received_before_sending_auth(self):
        """Verify the correct handshake order: recv auth_required FIRST,
        then send auth — not the reversed order that was previously used."""
        w = self._make_watcher()
        ws = MagicMock()
        recv_calls_at_send = [0]

        def fake_send(payload):
            msg = json.loads(payload)
            if msg.get("type") == "auth":
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
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.dict("sys.modules", {"websocket": ws_mod}),
        ):
            w.refresh_registry()
        # auth_required must have been received before auth was sent
        self.assertGreaterEqual(
            recv_calls_at_send[0], 1, "auth must be sent only after auth_required is received"
        )

    def test_no_authorization_header_in_create_connection(self):
        """create_connection must not receive an Authorization header —
        the dead header arg has been removed."""
        import inspect

        from nibe_ha_integration import HAEntityRegistryWatcher

        src = inspect.getsource(HAEntityRegistryWatcher.refresh_registry)
        self.assertNotIn(
            "Authorization",
            src,
            "Dead Authorization header arg must be removed from create_connection call",
        )

    def test_successful_refresh_populates_map(self):
        """Full happy-path: correct handshake, successful registry fetch,
        unique_id_map populated."""
        w = self._make_watcher()
        ws_mod, _ws = self._make_ws(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": [
                        {"unique_id": "nibe_100", "entity_id": "sensor.nibe_100"},
                        {"unique_id": "other_100", "entity_id": "sensor.other_100"},
                    ],
                },
            ]
        )
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch.dict("sys.modules", {"websocket": ws_mod}),
        ):
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

        self.em = _make_em()
        self.mqtt = MagicMock()
        self.em.mqtt = self.mqtt
        self.publisher = MagicMock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.test_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ManagementCommandHandler(
            self.mqtt, self.em, self.publisher, self.executor, self.test_executor
        ).register_all()

    def tearDown(self):
        self.executor.shutdown(wait=True)
        self.test_executor.shutdown(wait=True)

    def _get_handler(self):
        from nibe_mqtt_publisher import MgmtTopic

        for call in self.mqtt.message_callback_add.call_args_list:
            if call.args[0] == MgmtTopic.RUN_TESTS_PRESS:
                return call.args[1]
        raise KeyError("No handler for RUN_TESTS_PRESS")

    def _msg(self):
        m = MagicMock()
        m.payload = b""
        return m

    def _trigger_and_wait(
        self,
        returncode=0,
        stdout="",
        open_side_effect=FileNotFoundError,
        patch_notify=True,
        report_exists=False,
    ):
        """Fire the handler, wait for completion, return all publish pairs.

        patch_notify=True (default) patches notify_ha and dismiss_ha to
        prevent live Supervisor calls during tests that don't need to inspect
        the notification content.  Pass patch_notify=False when the caller
        supplies its own patch.object(notify_ha) to capture the message.

        report_exists=False (default) patches os.path.isfile so the summary
        content doesn't depend on whether a report file happens to be sitting
        at /homeassistant/www/nibe_test_report.html on whatever machine runs
        this test — on a real deployment that path legitimately exists from
        prior runs, which previously made report-dependent assertions here
        pass or fail depending on environment rather than on this test's own
        setup.
        """
        import concurrent.futures as _cf
        from contextlib import ExitStack

        proc = MagicMock(returncode=returncode, stdout=stdout, stderr="")
        proc.communicate.return_value = (stdout, "")
        handler = self._get_handler()
        with ExitStack() as stack:
            stack.enter_context(patch("subprocess.Popen", return_value=proc))
            if patch_notify:
                stack.enter_context(patch("nibe_ha_integration.notify_ha"))
                stack.enter_context(patch("nibe_ha_integration.dismiss_ha"))
            stack.enter_context(patch("os.path.isfile", return_value=report_exists))
            if report_exists:
                stack.enter_context(patch("os.path.getsize", return_value=123456))
            # run_test_suite (and its HTML post-processing) lives in
            # nibe_test_runner.py, not nibe_ha_integration.py — patching the
            # wrong module's `open` silently no-ops, and the real
            # filesystem's FileNotFoundError on the nonexistent report path
            # would then mask any other open_side_effect this test wants to
            # simulate (e.g. PermissionError, to exercise the generic
            # except Exception branch rather than the FileNotFoundError one).
            stack.enter_context(
                patch("nibe_test_runner.open", side_effect=open_side_effect, create=True)
            )
            handler(None, None, self._msg())
            # Wait inside the patch context so the thread sees the mock
            self.test_executor.shutdown(wait=True)
        # Recreate for tearDown
        self.test_executor = _cf.ThreadPoolExecutor(max_workers=1)
        return [(c.args[0], c.args[1]) for c in self.mqtt.publish.call_args_list]

    def _get_attrs(self, calls):
        """Return the LAST RUN_TESTS_ATTRS publish payload as a dict."""
        import json as _json

        from nibe_mqtt_publisher import MgmtTopic

        attrs_payloads = [p for t, p in calls if t == MgmtTopic.RUN_TESTS_ATTRS]
        self.assertTrue(attrs_payloads, "No RUN_TESTS_ATTRS publish found")
        return _json.loads(attrs_payloads[-1])

    # ── HTML post-processing exception ────────────────────────────────────────

    def test_html_postprocess_generic_exception_does_not_crash_handler(self):
        """A non-FileNotFoundError from open() (e.g. PermissionError) must be
        caught — the handler must still publish a final status."""
        calls = self._trigger_and_wait(
            returncode=0,
            stdout="2226 passed in 51s",
            open_side_effect=PermissionError("read-only filesystem"),
        )
        from nibe_mqtt_publisher import MgmtTopic

        states = [p for t, p in calls if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn("passed", states)

    def test_html_postprocess_unicode_decode_error_does_not_crash_handler(self):
        """Regression: a kill (abort or hard-timeout) can truncate the HTML
        report mid-write, mid-multibyte-UTF-8-sequence — open(encoding='utf-8')
        then raises UnicodeDecodeError, which is NOT an OSError subclass. It
        must be caught alongside OSError, not left to propagate to the outer
        except Exception, which would replace the real 'passed' status with a
        generic 'error' state."""
        calls = self._trigger_and_wait(
            returncode=0,
            stdout="2226 passed in 51s",
            open_side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        )
        from nibe_mqtt_publisher import MgmtTopic

        states = [p for t, p in calls if t == MgmtTopic.RUN_TESTS_STATE]
        self.assertIn("passed", states)

    def test_html_postprocess_exception_log_has_exact_text_and_real_args(self):
        """The sibling tests above only check the run doesn't crash — the
        diagnostic log's exact text/path/exception are otherwise untested."""
        real_err = PermissionError("read-only filesystem")
        with patch("nibe_test_runner.log_commands") as mock_log:
            self._trigger_and_wait(
                returncode=0,
                stdout="2226 passed in 51s",
                open_side_effect=real_err,
            )
        warning_call = next(
            c
            for c in mock_log.warning.call_args_list
            if c.args and c.args[0] == "Could not post-process HTML report at %s: %s"
        )
        self.assertEqual(warning_call.args[1], "/homeassistant/www/nibe_test_report.html")
        self.assertEqual(warning_call.args[2], real_err)

    def test_post_run_report_check_log_has_exact_text_and_real_args(self):
        with patch("nibe_test_runner.log_commands") as mock_log:
            self._trigger_and_wait(
                returncode=0,
                stdout="2226 passed in 51s",
                report_exists=True,
            )
        info_call = next(
            c
            for c in mock_log.info.call_args_list
            if c.args and c.args[0] == "Post-run report check: exists=%s size=%d at %s"
        )
        self.assertEqual(info_call.args[1], True)
        self.assertEqual(info_call.args[2], 123456)
        self.assertEqual(info_call.args[3], "/homeassistant/www/nibe_test_report.html")

    # ── Pass-path output parsing ──────────────────────────────────────────────

    def test_pass_empty_stdout_summary_is_empty_string(self):
        """Pass with empty stdout: 'if lines:' is False — summary is '' (raw output)."""
        calls = self._trigger_and_wait(returncode=0, stdout="")
        attrs = self._get_attrs(calls)
        self.assertEqual(attrs["summary"], "")

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

    def test_failure_line_splits_on_first_dash_separator_not_last(self):
        """FAILED lines with multiple ' - ' occurrences (e.g. the assertion
        message itself contains ' - ') must split the test path from the
        message at the FIRST occurrence — the pytest format is always
        '<test path> - <message>', and the message can legitimately
        contain more ' - ' sequences of its own. `rpartition` here would
        wrongly fold part of the message into the "test path" bold
        heading."""
        output = (
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_x.py::TestFoo::test_bar - AssertionError: got 1 - 2 = -1\n"
            "======================================================================\n"
            "1 failed in 2.00s"
        )
        captured = {}
        import nibe_ha_integration as _hi

        def _fake_notify(mqtt_client, title, message, notification_id):
            captured["message"] = message

        with patch.object(_hi, "notify_ha", side_effect=_fake_notify):
            self._trigger_and_wait(returncode=1, stdout=output, patch_notify=False)
        message = captured["message"]
        self.assertIn("**tests/test_x.py::TestFoo::test_bar**", message)
        self.assertIn("`AssertionError: got 1 - 2 = -1`", message)

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
        test_pos = msg.find("TestFoo::test_bar")
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

    def test_failure_notification_body_joins_multiple_fail_lines_with_blank_line(self):
        """Multiple formatted FAILED entries in the notification body must
        be separated by a real blank line ('\\n\\n'), not a literal
        placeholder — this is what makes each failure render as its own
        paragraph in the HA notification."""
        output = (
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_foo.py::test_one - AssertionError: first\n"
            "FAILED tests/test_foo.py::test_two - AssertionError: second\n"
            "2 failed in 0.5s\n"
        )
        captured = {}

        def _fake_notify(mqtt_client, title, message, notification_id):
            captured["message"] = message

        import nibe_ha_integration as _hi

        with patch.object(_hi, "notify_ha", side_effect=_fake_notify):
            self._trigger_and_wait(returncode=1, stdout=output, patch_notify=False)
        message = captured["message"]
        self.assertIn(
            "**tests/test_foo.py::test_one**\n`AssertionError: first`\n\n"
            "**tests/test_foo.py::test_two**\n`AssertionError: second`",
            message,
        )

    def test_failure_notification_body_wraps_summary_in_code_block_when_no_fail_lines(self):
        """When no FAILED lines could be extracted, the body must fall back
        to the plain summary wrapped in a triple-backtick code block — not
        an empty/None body."""
        output = "some unparseable failure output\n1 failed in 0.5s\n"
        captured = {}

        def _fake_notify(mqtt_client, title, message, notification_id):
            captured["message"] = message

        import nibe_ha_integration as _hi

        with patch.object(_hi, "notify_ha", side_effect=_fake_notify):
            self._trigger_and_wait(returncode=1, stdout=output, patch_notify=False)
        message = captured["message"]
        self.assertIn("```\n", message)
        self.assertIn("\n```", message)
        self.assertIn("1 failed in 0.5s", message)

    def test_failure_notification_truncated_when_exceeds_max(self):
        """When the assembled notification message exceeds _MAX_NOTIF=2048 chars
        the truncation suffix is appended and the report link reattached.
        patch notify_ha to capture the message directly (no SUPERVISOR_TOKEN needed)."""
        long_summary = "x" * 2200
        captured = {}

        def _fake_notify(mqtt_client, title, message, notification_id):
            captured["message"] = message

        import nibe_ha_integration as _hi

        with patch.object(_hi, "notify_ha", side_effect=_fake_notify):
            self._trigger_and_wait(returncode=1, stdout=long_summary, patch_notify=False)
        self.assertIn("message", captured, "notify_ha was not called")
        # Pin the exact truncated length: kept-prefix (_MAX_NOTIF -
        # len(report_link) - 10) + len('\n…\n\n') (4) + report_link —
        # simplifies to exactly _MAX_NOTIF - 6, catching an off-by-N error
        # in either the -10 offset or the slice's sign that a looser
        # "roughly under budget" check would miss.
        self.assertEqual(len(captured["message"]), 2048 - 6)
        # Production code appends "\n…\n\n" (ellipsis) — not the word "truncated"
        self.assertIn("…", captured["message"])
        self.assertIn("nibe_test_report.html", captured["message"])
        self.assertLessEqual(len(captured["message"]), 2048 + 200)  # truncation applied

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

    def test_pass_counts_line_not_duplicated_when_already_meaningful(self):
        """When the counts line is itself a normal (non-noise) meaningful
        line, it's already captured by the list comprehension — the
        `counts_line not in meaningful` guard must prevent it being
        appended a second time. An `or` in place of `and`, or `in` in
        place of `not in`, would duplicate it."""
        output = "5 passed in 1.23s\n"
        calls = self._trigger_and_wait(returncode=0, stdout=output)
        attrs = self._get_attrs(calls)
        summary = attrs["summary"]
        self.assertEqual(summary.count("5 passed in 1.23s"), 1)

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

    def test_pass_summary_joins_meaningful_lines_with_real_newline(self):
        """When two non-noise lines both survive the filter, they must be
        joined with a real newline — not some other separator — so the
        sensor attributes tab renders them as separate lines."""
        output = "a genuinely meaningful warning line\n5 passed, 1 warning in 1.23s\n"
        calls = self._trigger_and_wait(returncode=0, stdout=output)
        attrs = self._get_attrs(calls)
        summary = attrs["summary"]
        self.assertEqual(
            summary.split("\n"),
            ["a genuinely meaningful warning line", "5 passed, 1 warning in 1.23s"],
        )

    def test_pass_summary_strips_equals_wrapped_section_headers(self):
        """A pytest section header wrapped in '=' padding (e.g. the
        'warnings summary' banner) must be stripped as noise on the pass
        path — '=== ' is a distinct noise prefix from '--- ', not covered
        by the dash-prefix case alone."""
        output = "=== warnings summary ===\n5 passed, 1 warning in 1.23s\n"
        calls = self._trigger_and_wait(returncode=0, stdout=output)
        attrs = self._get_attrs(calls)
        summary = attrs["summary"]
        self.assertNotIn("warnings summary", summary)
        self.assertIn("5 passed", summary)

    def test_pass_summary_progress_marker_charset_is_case_sensitive(self):
        """The progress-dot filter set only matches uppercase 'F'/'E'
        (pytest's real failure/error dot-mode markers) — a line containing
        lowercase 'f'/'e' text is NOT a progress-dot line and must survive
        into the summary, while a line built purely from the real uppercase
        markers is noise and must be stripped."""
        output = ".FFF..EEE...sx......... [ 50%]\nbefore the fix\n5 passed in 1.23s\n"
        calls = self._trigger_and_wait(returncode=0, stdout=output)
        attrs = self._get_attrs(calls)
        summary = attrs["summary"]
        self.assertNotIn(".FFF..EEE...sx", summary)
        self.assertIn("before the fix", summary)

    def test_pass_summary_strips_report_line_regardless_of_dash_count(self):
        """Regression test: pytest-html's report line doesn't always use the
        exact '--- ' (3 dashes) prefix the noise filter originally matched
        on — a real ODROID run produced a 4-dash line
        ('---- Generated html report: ... ----') that leaked straight
        through into the sensor summary because the old prefix check
        required an exact match. The filter must strip this regardless of
        how many dashes wrap it, and regardless of skipped-test 's' markers
        in the progress-dot line (also previously not recognised as noise
        and left in the summary alongside it)."""
        output = (
            "............................ssssssssssssssssssssssssssss................ [ 32%]\n"
            "---- Generated html report: file:///homeassistant/www/nibe_test_report.html ----\n"
            "3287 passed, 28 skipped, 19 subtests passed in 1641.49s (0:27:21)\n"
        )
        calls = self._trigger_and_wait(returncode=0, stdout=output, report_exists=True)
        attrs = self._get_attrs(calls)
        summary = attrs["summary"]
        self.assertNotIn("Generated html report", summary)
        self.assertNotIn("ssssss", summary)
        self.assertNotIn("[ 32%]", summary)
        self.assertIn("3287 passed, 28 skipped", summary)

    def test_pass_summary_strips_bare_dash_wrapped_section_marker(self):
        """A '--- ' (3-dash) wrapped line whose content isn't 'generated
        html report' or 'bringing up nodes' must still be recognised as
        noise via the standalone '--- ' prefix entry — the two existing
        dash-prefix regression tests both happen to use lines whose
        content also matches a different tuple entry ('generated html
        report'), so neither actually exercises this entry on its own."""
        output = "--- section marker ---\n5 passed in 1.23s\n"
        calls = self._trigger_and_wait(returncode=0, stdout=output)
        attrs = self._get_attrs(calls)
        summary = attrs["summary"]
        self.assertNotIn("section marker", summary)
        self.assertIn("5 passed", summary)

    def test_pass_summary_includes_report_link_when_report_exists(self):
        """On success, the summary must point the user at the HTML report
        as an actual clickable Markdown link — [text](url) — not a bare
        URL, with a note that it can be slow to load. Regression test: an
        earlier version wrote 'Report: <url>' as plain text, which broke
        clickability entirely (no markdown link syntax for the renderer to
        turn into an anchor). Previously the only indication of the
        report's location on success was the raw pytest-html stdout line,
        which the noise filter was supposed to (and now does) strip out
        entirely."""
        calls = self._trigger_and_wait(returncode=0, stdout="5 passed in 1.23s", report_exists=True)
        attrs = self._get_attrs(calls)
        summary = attrs["summary"]
        # _get_ha_base_url() is the real (uninjected) function here — with
        # no SUPERVISOR_TOKEN set in the test environment it resolves to
        # '' (and caches that at module level for the process lifetime),
        # so the link is the bare relative path.
        # The link carries a cache-busting ?v=<timestamp> query param, so
        # match the surrounding text and verify the param's shape separately
        # rather than asserting an exact, timestamp-dependent literal.
        self.assertIn("[View full report](/local/nibe_test_report.html?v=", summary)
        self.assertIn("may take a moment to load", summary)
        match = re.search(r"/local/nibe_test_report\.html\?v=(\d+)\)", summary)
        self.assertIsNotNone(match, "report link must have a numeric ?v= cache-buster")
        # Pin the exact trailing text verbatim (not just a substring) so a
        # stray marker/case/wording change anywhere in this literal is caught.
        tail = summary[summary.index("(large file") :]
        self.assertEqual(
            tail,
            "(large file — may take a moment to load. Left-click opens the "
            "HA dashboard instead of the report — right-click and choose "
            '"Open link in new tab" to view it.)',
        )

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

        with patch.object(_time_mod, "monotonic", side_effect=_fake_monotonic):
            calls = self._trigger_and_wait(returncode=0, stdout="2260 passed in 1:30:00")
        attrs = self._get_attrs(calls)
        self.assertIn("elapsed", attrs)
        # Must use minutes format, not decimal-seconds format
        self.assertRegex(attrs["elapsed"], r"^\d+m \d+s$")


class TestExtractFailureLinesDirect(unittest.TestCase):
    """Direct tests of _extract_failure_lines() — previously only exercised
    indirectly through run_test_suite's full subprocess/MQTT harness, which
    never used more than 1 failure line (so the [:5]/[:10] truncation caps
    were never actually tested) and never had content before the FAILURES
    marker (so the in_failures initial-value bug was invisible)."""

    def _fn(self):
        from nibe_test_runner import _extract_failure_lines

        return _extract_failure_lines

    def test_content_before_failures_marker_is_not_captured(self):
        """in_failures must start False — an E-prefixed line appearing
        BEFORE the '=== FAILURES ===' marker must not be captured by the
        fallback extractor."""
        fn = self._fn()
        text = (
            "E       this looks like an E-line but appears before FAILURES\n"
            "=================================== FAILURES ===================================\n"
            "E       AssertionError: the real one\n"
        )
        result = fn(text)
        self.assertEqual(result, ["E       AssertionError: the real one"])

    def test_e_lines_are_stripped_of_leading_whitespace(self):
        fn = self._fn()
        text = (
            "=================================== FAILURES ===================================\n"
            "    E       AssertionError: indented\n"
        )
        result = fn(text)
        self.assertEqual(result, ["E       AssertionError: indented"])

    def test_e_line_cap_is_exactly_five(self):
        """More than 5 E-prefixed lines must be truncated to exactly 5 —
        not 6 (off-by-one) and not silently unbounded."""
        fn = self._fn()
        lines = [f"E       AssertionError: failure {i}" for i in range(8)]
        text = (
            "=================================== FAILURES ===================================\n"
            + "\n".join(lines)
            + "\n"
        )
        result = fn(text)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0], "E       AssertionError: failure 0")
        self.assertEqual(result[4], "E       AssertionError: failure 4")

    def test_fallback_block_cap_is_exactly_ten_when_no_e_lines(self):
        """When the FAILURES section has NO E-prefixed lines at all, the
        raw block fallback must cap at exactly 10 lines — not 11."""
        fn = self._fn()
        lines = [f"non-E line {i}" for i in range(15)]
        text = (
            "=================================== FAILURES ===================================\n"
            + "\n".join(lines)
            + "\n"
        )
        result = fn(text)
        self.assertEqual(len(result), 10)
        self.assertEqual(result[0], "non-E line 0")
        self.assertEqual(result[9], "non-E line 9")

    def test_short_summary_takes_priority_over_failures_fallback(self):
        """When BOTH a short-summary block and a FAILURES section are
        present, the short-summary FAILED lines must be returned — the
        fallback is only used when the short-summary block yields nothing."""
        fn = self._fn()
        text = (
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_x.py::test_a - err\n"
            "=================================== FAILURES ===================================\n"
            "E       this must not appear\n"
        )
        result = fn(text)
        self.assertEqual(result, ["tests/test_x.py::test_a - err"])


class TestRunTestSuiteOuterCrashRecovery(unittest.TestCase):
    """run_test_suite's outer except Exception: — anything unprotected
    inside the try (e.g. dismiss_fn/notify_fn raising, an MQTT publish
    failing mid-run) must not propagate silently out of this
    background-executor function; it must force RUN_TESTS_STATE to
    'error' so a crashed run doesn't leave a stuck 'running' state
    forever. Calls run_test_suite directly rather than through the MQTT
    handler layer for precise control over dismiss_fn raising."""

    def test_dismiss_fn_raising_is_caught_and_publishes_error_state(self):
        import threading

        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        notify_fn = MagicMock()
        dismiss_fn = MagicMock(side_effect=RuntimeError("Supervisor API unreachable"))
        proc = MagicMock(returncode=0, stdout="1 passed in 0.1s", stderr="")
        proc.communicate.return_value = ("1 passed in 0.1s", "")
        with (
            patch("subprocess.Popen", return_value=proc),
            self.assertLogs("nibe.commands", level="ERROR") as cm,
        ):
            run_test_suite(mqtt_client, notify_fn, dismiss_fn, lambda: "http://ha", done_event)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("run_test_suite crashed unexpectedly")
                for msg in cm.output
            )
        )
        from nibe_mqtt_publisher import MgmtTopic

        states = [
            c.args[1]
            for c in mqtt_client.publish.call_args_list
            if c.args[0] == MgmtTopic.RUN_TESTS_STATE
        ]
        self.assertIn("error", states)
        self.assertFalse(done_event.is_set())  # finally: done_event.clear() still ran
        # Pin the exact crash-recovery publish calls — topic, retained
        # flag, and JSON payload shape all matter for the HA sensor to
        # reflect the crash rather than silently keeping a stale value.
        from nibe_mqtt_publisher import MgmtTopic

        state_call = [
            c for c in mqtt_client.publish.call_args_list if c.args[0] == MgmtTopic.RUN_TESTS_STATE
        ][-1]
        self.assertEqual(state_call.args[1], "error")
        self.assertTrue(state_call.kwargs.get("retain"))
        attrs_call = [
            c for c in mqtt_client.publish.call_args_list if c.args[0] == MgmtTopic.RUN_TESTS_ATTRS
        ][-1]
        import json as _json

        payload = _json.loads(attrs_call.args[1])
        self.assertEqual(payload["status"], "error")
        self.assertIn("timestamp", payload)
        self.assertTrue(attrs_call.kwargs.get("retain"))

    def test_publish_of_crash_state_itself_failing_is_also_caught(self):
        """If even the crash-recovery publish fails (e.g. broker down), the
        inner except must also swallow it — done_event.clear() in the
        outer finally must still run rather than the whole function
        propagating an exception out of the background executor."""
        import threading

        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        mqtt_client.publish.side_effect = ConnectionError("broker unreachable")
        done_event = threading.Event()
        done_event.set()
        notify_fn = MagicMock()
        dismiss_fn = MagicMock(side_effect=RuntimeError("Supervisor API unreachable"))
        proc = MagicMock(returncode=0, stdout="1 passed in 0.1s", stderr="")
        proc.communicate.return_value = ("1 passed in 0.1s", "")
        with (
            patch("subprocess.Popen", return_value=proc),
            self.assertLogs("nibe.commands", level="ERROR") as cm,
        ):
            run_test_suite(
                mqtt_client, notify_fn, dismiss_fn, lambda: "http://ha", done_event
            )  # must not raise
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Failed to publish crash state for run_test_suite")
                for msg in cm.output
            )
        )
        self.assertFalse(done_event.is_set())


class TestRunTestSuiteStaleProcessCleanup(unittest.TestCase):
    """run_test_suite's defensive cleanup: if _current_proc is somehow
    still set and still running when a new run starts (guard/state desync),
    the stale subprocess must be killed via abort_test_suite() before the
    new one launches — must NOT fire when there is no stale process."""

    def test_stale_still_running_process_is_killed_before_new_run(self):
        import threading

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        stale_proc = MagicMock()
        stale_proc.poll.return_value = None  # still running
        stale_proc.pid = 4242
        stale_proc.wait.return_value = None
        nibe_test_runner._current_proc = stale_proc

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        new_proc = MagicMock(returncode=0, stdout="1 passed in 0.1s", stderr="")
        new_proc.communicate.return_value = ("1 passed in 0.1s", "")
        try:
            with (
                patch("subprocess.Popen", return_value=new_proc),
                patch("os.killpg") as mock_killpg,
                patch("os.getpgid", return_value=4242),
            ):
                run_test_suite(
                    mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event
                )
            mock_killpg.assert_called_once_with(4242, signal.SIGKILL)
            stale_proc.wait.assert_called_once_with(timeout=10)
        finally:
            nibe_test_runner._current_proc = None

    def test_stale_process_not_dying_within_10s_logs_error_but_continues(self):
        """If SIGKILL doesn't make the stale process exit within the 10s
        wait, that must be logged as an error — but the new run must
        still proceed rather than getting stuck."""
        import subprocess
        import threading

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        stale_proc = MagicMock()
        stale_proc.poll.return_value = None  # still running
        stale_proc.pid = 4242
        stale_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="pytest", timeout=10)
        nibe_test_runner._current_proc = stale_proc

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        new_proc = MagicMock(returncode=0, stdout="1 passed in 0.1s", stderr="")
        new_proc.communicate.return_value = ("1 passed in 0.1s", "")
        try:
            with (
                patch("subprocess.Popen", return_value=new_proc),
                patch("os.killpg"),
                patch("os.getpgid", return_value=4242),
                self.assertLogs("nibe.commands", level="ERROR") as cm,
            ):
                run_test_suite(
                    mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event
                )
            self.assertTrue(any("did not exit within 10s" in msg for msg in cm.output))
        finally:
            nibe_test_runner._current_proc = None

    def test_stale_process_timeout_error_has_exact_text_and_real_pid(self):
        """The sibling test above uses `in`, which can't distinguish the
        real text from an XX-wrapped mutant — assert on the mocked call's
        exact args instead."""
        import subprocess
        import threading

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        stale_proc = MagicMock()
        stale_proc.poll.return_value = None
        stale_proc.pid = 4242
        stale_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="pytest", timeout=10)
        nibe_test_runner._current_proc = stale_proc

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        new_proc = MagicMock(returncode=0, stdout="1 passed in 0.1s", stderr="")
        new_proc.communicate.return_value = ("1 passed in 0.1s", "")
        try:
            with (
                patch("subprocess.Popen", return_value=new_proc),
                patch("os.killpg"),
                patch("os.getpgid", return_value=4242),
                patch("nibe_test_runner.log_commands") as mock_log,
            ):
                run_test_suite(
                    mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event
                )
            error_call = next(
                c
                for c in mock_log.error.call_args_list
                if c.args[0].startswith("Stale test subprocess")
            )
            self.assertEqual(
                error_call.args[0],
                "Stale test subprocess (pid %d) did not exit within 10s after SIGKILL",
            )
            self.assertEqual(error_call.args[1], 4242)
        finally:
            nibe_test_runner._current_proc = None

    def test_stale_process_warning_has_exact_text_and_real_pid(self):
        import threading

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        stale_proc = MagicMock()
        stale_proc.poll.return_value = None
        stale_proc.pid = 4242
        stale_proc.wait.return_value = None
        nibe_test_runner._current_proc = stale_proc

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        new_proc = MagicMock(returncode=0, stdout="1 passed in 0.1s", stderr="")
        new_proc.communicate.return_value = ("1 passed in 0.1s", "")
        try:
            with (
                patch("subprocess.Popen", return_value=new_proc),
                patch("os.killpg"),
                patch("os.getpgid", return_value=4242),
                patch("nibe_test_runner.log_commands") as mock_log,
            ):
                run_test_suite(
                    mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event
                )
            from unittest.mock import call

            self.assertEqual(
                mock_log.warning.call_args_list[0],
                call(
                    "Stale test subprocess (pid %d) found before starting a new "
                    "run — killing it first",
                    4242,
                ),
            )
        finally:
            nibe_test_runner._current_proc = None

    def test_stale_process_abort_reason_has_exact_text(self):
        import threading

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        stale_proc = MagicMock()
        stale_proc.poll.return_value = None
        stale_proc.pid = 4242
        stale_proc.wait.return_value = None
        nibe_test_runner._current_proc = stale_proc

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        new_proc = MagicMock(returncode=0, stdout="1 passed in 0.1s", stderr="")
        new_proc.communicate.return_value = ("1 passed in 0.1s", "")
        try:
            with (
                patch("subprocess.Popen", return_value=new_proc),
                patch("os.killpg"),
                patch("os.getpgid", return_value=4242),
                patch("nibe_test_runner.abort_test_suite") as mock_abort,
            ):
                run_test_suite(
                    mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event
                )
            mock_abort.assert_called_once_with("superseded by a new test run")
        finally:
            nibe_test_runner._current_proc = None

    def test_no_stale_process_when_current_proc_already_finished(self):
        """poll() returning a real exit code means the tracked process has
        already finished — this must NOT be treated as stale/kill it."""
        import threading

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        finished_proc = MagicMock()
        finished_proc.poll.return_value = 0  # already exited
        nibe_test_runner._current_proc = finished_proc

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        new_proc = MagicMock(returncode=0, stdout="1 passed in 0.1s", stderr="")
        new_proc.communicate.return_value = ("1 passed in 0.1s", "")
        try:
            with (
                patch("subprocess.Popen", return_value=new_proc),
                patch("os.killpg") as mock_killpg,
            ):
                run_test_suite(
                    mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event
                )
            mock_killpg.assert_not_called()
            finished_proc.wait.assert_not_called()
        finally:
            nibe_test_runner._current_proc = None


class TestAbortTestSuite(unittest.TestCase):
    """abort_test_suite() — lets the add-on's shutdown sequence kill an
    in-flight pytest subprocess directly, rather than leaving Python's own
    atexit hook for ThreadPoolExecutor to block process exit until that
    subprocess finishes on its own (which can be 25-30+ minutes, far longer
    than Docker's stop grace period — the container gets SIGKILLed before a
    naturally-finishing wait would ever complete). Real regression coverage
    for the fix, not just the surrounding wiring: this drives an actual
    in-flight run_test_suite() call on a background thread, waits for it to
    genuinely reach the blocked-in-communicate() state, then calls
    abort_test_suite() and verifies the process *group* is killed (not just
    the top-level PID — see abort_test_suite's docstring for why that
    distinction matters with pytest-xdist) and the run thread unblocks and
    finishes."""

    def test_default_reason_is_add_on_shutting_down(self):
        """Directly pins abort_test_suite()'s default `reason` value,
        without needing a full in-flight run_test_suite() — the default is
        only exercised when the shutdown sequence calls abort_test_suite()
        with no explicit reason."""
        import nibe_test_runner
        from nibe_test_runner import abort_test_suite

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 4242
        nibe_test_runner._current_proc = mock_proc
        nibe_test_runner._abort_reason = None
        try:
            with patch("os.killpg"), patch("os.getpgid", return_value=4242):
                abort_test_suite()
            self.assertEqual(nibe_test_runner._abort_reason, "add-on shutting down")
        finally:
            nibe_test_runner._current_proc = None
            nibe_test_runner._abort_reason = None

    def test_abort_kills_in_flight_subprocess_and_run_completes(self):
        import threading
        import time

        import nibe_test_runner
        from nibe_test_runner import abort_test_suite, run_test_suite

        # A real threading.Event drives the mock Popen's communicate(): the
        # first call blocks (simulating the real subprocess still running)
        # until the process group is killed, at which point a second call —
        # matching run_test_suite's own kill()-then-drain sequence — returns
        # output. This proves abort_test_suite() actually unblocks the run
        # rather than the test just asserting the kill call was made in
        # isolation.
        killed = threading.Event()

        def _communicate(timeout=None):
            if not killed.is_set():
                # Real subprocess.Popen.communicate() blocks until the
                # process exits or the timeout elapses; here it blocks
                # until the process group is killed, standing in for "the
                # process is genuinely still running".
                killed.wait(timeout=5)
                if not killed.is_set():
                    raise AssertionError(
                        "communicate() was never unblocked by the kill — "
                        "abort_test_suite() did not actually kill the process group"
                    )
            return ("aborted", "")

        mock_proc = MagicMock(pid=12345, returncode=-9)
        mock_proc.communicate.side_effect = _communicate
        mock_proc.poll.return_value = None  # still running, from abort_test_suite's perspective

        mqtt_client = MagicMock()
        notify_fn = MagicMock()
        dismiss_fn = MagicMock()
        done_event = threading.Event()
        done_event.set()

        run_thread_exception = []

        def _run():
            try:
                with patch("subprocess.Popen", return_value=mock_proc):
                    run_test_suite(
                        mqtt_client,
                        notify_fn,
                        dismiss_fn,
                        lambda: "http://ha.local",
                        done_event,
                    )
            except Exception as exc:  # noqa: BLE001 — surfaced via assertion below, not swallowed  # pragma: no cover
                run_thread_exception.append(exc)

        t = threading.Thread(target=_run)
        t.start()
        try:
            # Wait for run_test_suite to actually reach the blocked
            # communicate() call (i.e. nibe_test_runner._current_proc is
            # set) before aborting — asserting this rather than sleeping a
            # fixed guess keeps the test from being a race.
            for _ in range(50):
                if nibe_test_runner._current_proc is not None:
                    break
                time.sleep(0.1)
            else:
                self.fail("run_test_suite never reached the in-flight state")

            with (
                patch("os.getpgid", return_value=99999) as mock_getpgid,
                patch("os.killpg", side_effect=lambda *a: killed.set()) as mock_killpg,
            ):
                abort_test_suite("test abort")
                mock_getpgid.assert_called_once_with(12345)
                mock_killpg.assert_called_once_with(99999, signal.SIGKILL)

            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "run_test_suite did not unblock after abort")
        finally:
            killed.set()  # make sure the thread can't stay blocked even on failure
            t.join(timeout=5)

        self.assertEqual(run_thread_exception, [])
        # abort_test_suite() must be a safe no-op once the run has finished.
        self.assertIsNone(nibe_test_runner._current_proc)
        abort_test_suite("second call after completion")

        # The aborted run must be reported as 'aborted', not a misleading
        # 'failed' (which the raw negative returncode -9 would otherwise
        # classify it as) — and must not trigger a FAILED-looking HA
        # notification, since no tests actually finished running.
        from nibe_mqtt_publisher import MgmtTopic

        states = [
            c.args[1]
            for c in mqtt_client.publish.call_args_list
            if c.args[0] == MgmtTopic.RUN_TESTS_STATE
        ]
        self.assertIn("aborted", states)
        self.assertNotIn("failed", states)
        notify_fn.assert_not_called()
        dismiss_fn.assert_not_called()

    def test_abort_reason_appears_in_published_summary(self):
        """The aborted run's sensor summary must state plainly that it was
        aborted and why — not attempt to parse the killed process's partial
        stdout/stderr as if it were a real pass/fail result."""
        import json as _json
        import threading
        import time

        import nibe_test_runner
        from nibe_test_runner import abort_test_suite, run_test_suite

        killed = threading.Event()

        def _communicate(timeout=None):
            if not killed.is_set():
                killed.wait(timeout=5)
            return ("garbage partial output", "")

        mock_proc = MagicMock(pid=12345, returncode=-9)
        mock_proc.communicate.side_effect = _communicate
        mock_proc.poll.return_value = None

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()

        def _run():
            with patch("subprocess.Popen", return_value=mock_proc):
                run_test_suite(
                    mqtt_client,
                    MagicMock(),
                    MagicMock(),
                    lambda: "http://ha.local",
                    done_event,
                )

        t = threading.Thread(target=_run)
        t.start()
        try:
            for _ in range(50):
                if nibe_test_runner._current_proc is not None:
                    break
                time.sleep(0.1)
            else:
                self.fail("run_test_suite never reached the in-flight state")

            with (
                patch("os.getpgid", return_value=99999),
                patch("os.killpg", side_effect=lambda *a: killed.set()),
            ):
                abort_test_suite("add-on shutting down")

            t.join(timeout=5)
            self.assertFalse(t.is_alive())
        finally:
            killed.set()
            t.join(timeout=5)

        from nibe_mqtt_publisher import MgmtTopic

        attrs_calls = [
            c.args[1]
            for c in mqtt_client.publish.call_args_list
            if c.args[0] == MgmtTopic.RUN_TESTS_ATTRS
        ]
        final = _json.loads(attrs_calls[-1])
        self.assertEqual(final["status"], "aborted")
        self.assertIn("add-on shutting down", final["summary"])
        self.assertNotIn("garbage partial output", final["summary"])

    def test_abort_is_a_noop_when_no_run_is_in_flight(self):
        import nibe_test_runner
        from nibe_test_runner import abort_test_suite

        self.assertIsNone(nibe_test_runner._current_proc)
        abort_test_suite("nothing running")  # must not raise

    def test_abort_logs_exact_warning_message_with_reason(self):
        """abort_test_suite() must log the warning with the exact format
        string and reason substituted via %s — not a hardcoded/garbled
        message, and not the reason alone with no format string."""
        import nibe_test_runner
        from nibe_test_runner import abort_test_suite

        mock_proc = MagicMock(pid=4242)
        mock_proc.poll.return_value = None
        nibe_test_runner._current_proc = mock_proc
        try:
            with (
                patch("os.killpg"),
                patch("os.getpgid", return_value=4242),
                self.assertLogs("nibe.commands", level="WARNING") as cm,
            ):
                abort_test_suite("custom shutdown reason")
            self.assertEqual(
                cm.output,
                ["WARNING:nibe.commands:Aborting in-flight test suite run: custom shutdown reason"],
            )
        finally:
            nibe_test_runner._current_proc = None
            nibe_test_runner._abort_reason = None

    def test_abort_swallows_process_lookup_error_race(self):
        """If the process exits on its own between abort_test_suite()'s
        poll() check and the actual killpg() call, os.killpg() raises
        ProcessLookupError (no such process/group) — that race must be
        swallowed, not propagate and crash the shutdown sequence calling
        this from generate_nibe_mqtt.py."""
        import nibe_test_runner
        from nibe_test_runner import abort_test_suite

        mock_proc = MagicMock(pid=12345)
        mock_proc.poll.return_value = None  # still running per poll()
        nibe_test_runner._current_proc = mock_proc
        try:
            with (
                patch("os.getpgid", return_value=99999),
                patch("os.killpg", side_effect=ProcessLookupError),
            ):
                abort_test_suite("race with natural exit")  # must not raise
        finally:
            nibe_test_runner._current_proc = None


class TestRunTestSuiteMainPaths(unittest.TestCase):
    """run_test_suite: the main success/failure/timeout/launch-error paths
    and their MQTT publishes / notifications — previously exercised only
    by the outer-crash-recovery tests above (which only cover the passed
    path incidentally, as setup for triggering a crash elsewhere)."""

    def _run(
        self,
        proc_returncode=None,
        proc_stdout="",
        proc_stderr="",
        subprocess_side_effect=None,
        notify_fn=None,
        dismiss_fn=None,
        get_base_url_fn=None,
    ):
        import threading
        from contextlib import ExitStack

        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        notify_fn = notify_fn if notify_fn is not None else MagicMock()
        dismiss_fn = dismiss_fn if dismiss_fn is not None else MagicMock()
        get_base_url_fn = get_base_url_fn or (lambda: "http://ha.local:8123")
        with ExitStack() as stack:
            if isinstance(subprocess_side_effect, subprocess.TimeoutExpired):
                # A real subprocess.run(timeout=...)-style timeout is raised
                # by .communicate(), not by Popen() construction itself —
                # the process launches fine and only the *wait* times out.
                # The Popen mock's constructor must succeed; only
                # communicate() (and, after the kill, the follow-up drain
                # call) needs to model that. The timeout path kills the
                # whole process group via os.getpgid()/os.killpg() (not
                # proc.kill() — see abort_test_suite's docstring for why),
                # both of which must be patched here since proc.pid on a
                # MagicMock isn't a real PID the OS calls could accept.
                proc = MagicMock(pid=12345)
                proc.communicate.side_effect = [subprocess_side_effect, ("", "")]
                stack.enter_context(patch("subprocess.Popen", return_value=proc))
                stack.enter_context(patch("os.getpgid", return_value=99999))
                stack.enter_context(patch("os.killpg"))
            elif subprocess_side_effect is not None:
                stack.enter_context(patch("subprocess.Popen", side_effect=subprocess_side_effect))
            else:
                proc = MagicMock(returncode=proc_returncode, stdout=proc_stdout, stderr=proc_stderr)
                proc.communicate.return_value = (proc_stdout, proc_stderr)
                stack.enter_context(patch("subprocess.Popen", return_value=proc))
            run_test_suite(mqtt_client, notify_fn, dismiss_fn, get_base_url_fn, done_event)
        return mqtt_client, notify_fn, dismiss_fn, done_event

    def _attrs(self, mqtt_client):
        import json as _json

        from nibe_mqtt_publisher import MgmtTopic

        # There are two ATTRS publishes: the initial 'running' one and the
        # final result one — the final one has a 'status' other than
        # 'running' inside its JSON payload; find that one specifically.
        for c in mqtt_client.publish.call_args_list:
            if c.args[0] == MgmtTopic.RUN_TESTS_ATTRS:
                payload = _json.loads(c.args[1])
                if payload.get("status") != "running":
                    return payload
        raise AssertionError("no final RUN_TESTS_ATTRS publish found")

    def test_passed_publishes_passed_state_and_dismisses_notification(self):
        from nibe_mqtt_publisher import MgmtTopic

        mqtt_client, notify_fn, dismiss_fn, _ = self._run(
            proc_returncode=0,
            proc_stdout="5 passed in 1.23s",
            proc_stderr="",
        )
        states = [
            c.args[1]
            for c in mqtt_client.publish.call_args_list
            if c.args[0] == MgmtTopic.RUN_TESTS_STATE
        ]
        self.assertIn("passed", states)
        dismiss_fn.assert_called_once_with(mqtt_client, "nibe_test_suite_result")
        notify_fn.assert_not_called()

    def test_passed_attrs_payload_has_correct_exit_code_and_status(self):
        mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="5 passed in 1.23s")
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["status"], "passed")
        self.assertEqual(attrs["exit_code"], 0)

    def test_failed_publishes_failed_state_and_notifies(self):
        from nibe_mqtt_publisher import MgmtTopic

        output = (
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_x.py::TestFoo::test_bar - AssertionError: expected 1, got 2\n"
            "======================================================================\n"
            "1 failed, 4 passed in 2.00s"
        )
        mqtt_client, notify_fn, dismiss_fn, _ = self._run(
            proc_returncode=1,
            proc_stdout=output,
            proc_stderr="",
        )
        states = [
            c.args[1]
            for c in mqtt_client.publish.call_args_list
            if c.args[0] == MgmtTopic.RUN_TESTS_STATE
        ]
        self.assertIn("failed", states)
        dismiss_fn.assert_not_called()
        notify_fn.assert_called_once()
        kwargs = notify_fn.call_args.kwargs
        self.assertIn("FAILED", kwargs["title"])
        self.assertIn("tests/test_x.py::TestFoo::test_bar", kwargs["message"])
        self.assertIn("AssertionError: expected 1, got 2", kwargs["message"])
        self.assertEqual(kwargs["notification_id"], "nibe_test_suite_result")

    def test_notify_fn_receives_the_real_mqtt_client(self):
        """notify_fn's first positional argument must be the real
        mqtt_client passed into run_test_suite, not None/a placeholder —
        notify_ha uses it to actually publish the notification."""
        mqtt_client, notify_fn, _dismiss_fn, _ = self._run(
            proc_returncode=1,
            proc_stdout="1 failed, 4 passed in 2.00s",
        )
        self.assertIs(notify_fn.call_args.args[0], mqtt_client)

    def test_failed_attrs_payload_exit_code_and_status(self):
        mqtt_client, *_ = self._run(
            proc_returncode=1,
            proc_stdout="1 failed, 4 passed in 2.00s",
        )
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["status"], "failed")
        self.assertEqual(attrs["exit_code"], 1)

    def test_timeout_kill_race_process_already_gone_does_not_raise(self):
        """If the process group is already gone by the time the hard
        timeout tries to SIGKILL it (a race with the process exiting on
        its own right at the 4-hour mark), os.killpg raising
        ProcessLookupError must be swallowed — not propagate and crash
        the run."""
        import subprocess as _sp

        proc = MagicMock(pid=12345)
        proc.communicate.side_effect = [
            _sp.TimeoutExpired(cmd="pytest", timeout=14400),
            ("", ""),
        ]
        mqtt_client = MagicMock()
        import threading

        from nibe_test_runner import run_test_suite

        done_event = threading.Event()
        done_event.set()
        with (
            patch("subprocess.Popen", return_value=proc),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg", side_effect=ProcessLookupError),
        ):
            run_test_suite(
                mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event
            )  # must not raise
        from nibe_mqtt_publisher import MgmtTopic

        states = [
            c.args[1]
            for c in mqtt_client.publish.call_args_list
            if c.args[0] == MgmtTopic.RUN_TESTS_STATE
        ]
        self.assertIn("timed_out", states)

    def test_timeout_sets_timed_out_status_and_notifies(self):
        import subprocess as _sp

        from nibe_mqtt_publisher import MgmtTopic

        mqtt_client, notify_fn, _dismiss_fn, _ = self._run(
            subprocess_side_effect=_sp.TimeoutExpired(cmd="pytest", timeout=14400),
        )
        states = [
            c.args[1]
            for c in mqtt_client.publish.call_args_list
            if c.args[0] == MgmtTopic.RUN_TESTS_STATE
        ]
        self.assertIn("timed_out", states)
        notify_fn.assert_called_once()
        self.assertIn("TIMED OUT", notify_fn.call_args.kwargs["title"])

    def _run_timeout(self, elapsed_seconds, output=""):
        import subprocess as _sp
        import threading

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        notify_fn = MagicMock()
        timeout_exc = _sp.TimeoutExpired(cmd="pytest", timeout=14400)
        proc = MagicMock(pid=12345)
        proc.communicate.side_effect = [timeout_exc, (output, "")]
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            return 0.0 if calls["n"] == 1 else float(elapsed_seconds)

        with (
            patch("subprocess.Popen", return_value=proc),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
            patch.object(nibe_test_runner.time, "monotonic", side_effect=fake_monotonic),
        ):
            run_test_suite(mqtt_client, notify_fn, MagicMock(), lambda: "http://ha", done_event)
        return notify_fn

    def test_timeout_near_4hr_limit_gets_reduce_max_examples_advice(self):
        """elapsed >= 14000 (close to the 4-hour hard limit) must produce
        the exact "reduce max_examples" body verbatim — pins the >= boundary,
        distinguishes it from the "not necessarily a problem" nowhere-near
        case, and catches any wording/case/marker drift in the literal."""
        notify_fn = self._run_timeout(14000)
        message = notify_fn.call_args.kwargs["message"]
        self.assertEqual(
            message.split("\n\n")[1],
            "The test process was killed after running for "
            "233m 20s, close to the 4-hour hard limit. "
            "Reduce `max_examples` or `stateful_step_count` in "
            "`tests/conftest.py` and rebuild the add-on.",
        )

    def test_timeout_far_from_4hr_limit_gets_diagnostic_advice(self):
        """elapsed just under 14000 must produce the exact "nowhere near"
        diagnostic body verbatim, not the "reduce max_examples" advice — a
        process that died almost instantly is a different problem than one
        that ran the tests too slowly."""
        notify_fn = self._run_timeout(13999)
        message = notify_fn.call_args.kwargs["message"]
        self.assertEqual(
            message.split("\n\n")[1],
            "The test process was killed after only 233m 19s — "
            "nowhere near the 4-hour limit, so this is not a "
            '"tests are too slow" situation. Check the captured '
            "output above and the add-on log for what actually "
            "happened to the subprocess.",
        )

    def test_timeout_falls_back_to_pre_kill_captured_output_when_drain_empty(self):
        """If the post-kill drain communicate() returns nothing (process
        already fully drained/closed), the partial output reported must
        fall back to whatever the original timeout exception itself had
        already captured — not silently become empty. An `and` in place of
        this `or` would break the fallback whenever the drain returns ''
        (falsy) rather than None."""
        import subprocess as _sp
        import threading

        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        timeout_exc = _sp.TimeoutExpired(
            cmd="pytest",
            timeout=14400,
            output="pre-kill stdout capture",
            stderr="pre-kill stderr capture",
        )
        proc = MagicMock(pid=12345)
        proc.communicate.side_effect = [timeout_exc, ("", "")]
        with (
            patch("subprocess.Popen", return_value=proc),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
            self.assertLogs("nibe.commands", level="ERROR") as cm,
        ):
            run_test_suite(mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event)
        logged = "\n".join(cm.output)
        self.assertIn("pre-kill stdout capture", logged)
        self.assertIn("pre-kill stderr capture", logged)

    def test_timeout_elapsed_is_end_minus_start_not_reversed(self):
        """Pins the timeout branch's elapsed-time computation direction —
        t_start is read before the subprocess launches and this second
        time.monotonic() call after the kill must be *later*, so elapsed
        must be positive and equal to (end - start), not (start - end)."""
        import subprocess as _sp
        import threading

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        timeout_exc = _sp.TimeoutExpired(cmd="pytest", timeout=14400)
        proc = MagicMock(pid=12345)
        proc.communicate.side_effect = [timeout_exc, ("", "")]
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            return 100.0 if calls["n"] == 1 else 340.0

        with (
            patch("subprocess.Popen", return_value=proc),
            patch("os.getpgid", return_value=99999),
            patch("os.killpg"),
            patch.object(nibe_test_runner.time, "monotonic", side_effect=fake_monotonic),
        ):
            run_test_suite(mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event)
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["elapsed_s"], 240.0)

    def test_html_report_gets_viewport_meta_and_widened_min_width(self):
        """The HTML post-processing must inject a mobile viewport meta tag
        right after the charset meta tag, and relax the desktop-oriented
        800px min-width to 320px so the report is actually usable on a
        phone — the two string literals involved (and the exact insertion
        point) are load-bearing, not incidental."""
        import threading
        from unittest.mock import mock_open

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        proc = MagicMock(returncode=0, stdout="1 passed in 0.1s", stderr="")
        proc.communicate.return_value = ("1 passed in 0.1s", "")
        original_html = (
            '<html><head><meta charset="utf-8"/>'
            "<style>.wrapper{min-width: 800px}</style></head><body></body></html>"
        )
        m = mock_open(read_data=original_html)
        with (
            patch("subprocess.Popen", return_value=proc),
            patch.object(nibe_test_runner, "open", m, create=True),
            patch("os.path.isfile", return_value=True),
            patch("os.path.getsize", return_value=999),
        ):
            run_test_suite(mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event)
        written = "".join(c.args[0] for c in m().write.call_args_list)
        self.assertIn(
            '<meta charset="utf-8"/>\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1"/>',
            written,
        )
        self.assertIn("min-width: 320px", written)
        self.assertNotIn("min-width: 800px", written)

    def test_timeout_attrs_exit_code_is_negative_one(self):
        import subprocess as _sp

        mqtt_client, *_ = self._run(
            subprocess_side_effect=_sp.TimeoutExpired(cmd="pytest", timeout=14400),
        )
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["status"], "timed_out")
        self.assertEqual(attrs["exit_code"], -1)

    def test_python_exe_falls_back_to_shutil_which_when_sys_executable_empty(self):
        """Regression test: on some Alpine/musl container setups
        sys.executable comes back as '' rather than the interpreter's real
        path. The old fallback ('python3' passed straight to subprocess.run)
        depended on the *subprocess's* PATH resolution succeeding, which
        isn't guaranteed — a real run on the ODROID hit exactly this and
        failed with "no such file: python3". The interpreter path must
        instead be resolved via shutil.which() in this process first."""
        import threading

        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        proc = MagicMock(returncode=0, stdout="", stderr="")
        proc.communicate.return_value = ("", "")
        with (
            patch("sys.executable", ""),
            patch("shutil.which", return_value="/usr/bin/python3") as mock_which,
            patch("subprocess.Popen", return_value=proc) as mock_run,
        ):
            run_test_suite(
                mqtt_client,
                MagicMock(),
                MagicMock(),
                lambda: "http://ha.local",
                done_event,
            )
        mock_which.assert_any_call("python3")
        args = mock_run.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/python3")

    def test_python_exe_falls_back_to_which_python_when_python3_not_found(self):
        """When both sys.executable and `which python3` come up empty, the
        second-tier fallback must try `which python` before finally
        hardcoding 'python3' — an `and` in place of the `or` chain here
        would short-circuit this fallback and jump straight to the
        hardcoded default even when `which python` would have found a real
        interpreter."""
        import threading

        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        proc = MagicMock(returncode=0, stdout="", stderr="")
        proc.communicate.return_value = ("", "")

        def _which(name):
            return "/usr/bin/python" if name == "python" else None

        with (
            patch("sys.executable", ""),
            patch("shutil.which", side_effect=_which),
            patch("subprocess.Popen", return_value=proc) as mock_run,
        ):
            run_test_suite(
                mqtt_client,
                MagicMock(),
                MagicMock(),
                lambda: "http://ha.local",
                done_event,
            )
        args = mock_run.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/python")

    def test_python_exe_hardcoded_fallback_when_nothing_else_resolves(self):
        """If sys.executable is empty and neither `which python3` nor
        `which python` finds anything, the interpreter path must fall back
        to the literal 'python3' rather than None/empty."""
        import threading

        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        proc = MagicMock(returncode=0, stdout="", stderr="")
        proc.communicate.return_value = ("", "")
        with (
            patch("sys.executable", ""),
            patch("shutil.which", return_value=None),
            patch("subprocess.Popen", return_value=proc) as mock_run,
        ):
            run_test_suite(
                mqtt_client,
                MagicMock(),
                MagicMock(),
                lambda: "http://ha.local",
                done_event,
            )
        args = mock_run.call_args.args[0]
        self.assertEqual(args[0], "python3")

    def test_pythonpath_env_points_at_app_directory(self):
        """The subprocess's PYTHONPATH must be set to <addon_dir>/app —
        wrong key casing or a wrong path segment would make the spawned
        pytest process unable to import the bridge's own app modules."""
        import os
        import threading

        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        proc = MagicMock(returncode=0, stdout="", stderr="")
        proc.communicate.return_value = ("", "")
        with patch("subprocess.Popen", return_value=proc) as mock_run:
            run_test_suite(
                mqtt_client,
                MagicMock(),
                MagicMock(),
                lambda: "http://ha.local",
                done_event,
            )
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(os.path.basename(env["PYTHONPATH"]), "app")
        self.assertNotIn("pythonpath", env)

    def test_running_state_published_with_retain_true(self):
        """RUN_TESTS_STATE/ATTRS must be published retained — a subscriber
        connecting mid-run (e.g. the HA sensor re-subscribing after a
        restart) needs the last-known 'running' state immediately, not just
        future updates."""
        import threading

        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        proc = MagicMock(returncode=0, stdout="", stderr="")
        proc.communicate.return_value = ("", "")
        with patch("subprocess.Popen", return_value=proc):
            run_test_suite(
                mqtt_client,
                MagicMock(),
                MagicMock(),
                lambda: "http://ha.local",
                done_event,
            )
        for call in mqtt_client.publish.call_args_list:
            self.assertTrue(call.kwargs.get("retain"), f"not retained: {call}")

    def test_launch_error_message_includes_attempted_python_exe(self):
        """A launch failure's notification body must name the interpreter
        path that was actually attempted, so a "no such file" error is
        diagnosable from the notification alone rather than requiring log
        access to figure out what path was even tried."""
        with patch("sys.executable", "/usr/bin/python3"):
            _mqtt_client, notify_fn, _dismiss_fn, _ = self._run(
                subprocess_side_effect=FileNotFoundError("no such file: python3"),
            )
        kwargs = notify_fn.call_args.kwargs
        self.assertIn("/usr/bin/python3", kwargs["message"])

    def test_launch_error_sets_error_status_and_notifies_with_exception_text(self):
        from nibe_mqtt_publisher import MgmtTopic

        mqtt_client, notify_fn, _dismiss_fn, _ = self._run(
            subprocess_side_effect=OSError("pytest executable not found"),
        )
        states = [
            c.args[1]
            for c in mqtt_client.publish.call_args_list
            if c.args[0] == MgmtTopic.RUN_TESTS_STATE
        ]
        self.assertIn("error", states)
        notify_fn.assert_called_once()
        kwargs = notify_fn.call_args.kwargs
        self.assertIn("LAUNCH ERROR", kwargs["title"])
        self.assertIn("pytest executable not found", kwargs["message"])

    def test_launch_error_attrs_exit_code_is_negative_two(self):
        mqtt_client, *_ = self._run(subprocess_side_effect=OSError("boom"))
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["status"], "error")
        self.assertEqual(attrs["exit_code"], -2)

    def test_elapsed_s_attr_rounded_to_one_decimal_not_two(self):
        """Pins round(elapsed, 1) — a mutant rounding to 2 decimals instead
        would report 5.57 rather than the intended 5.6."""
        times = iter([1000.0, 1005.567])
        with patch("time.monotonic", side_effect=lambda: next(times)):
            mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["elapsed_s"], 5.6)

    def test_report_size_is_zero_when_report_does_not_exist(self):
        """report_size must default to 0 (not e.g. 1) when the report file
        doesn't exist — os.path.getsize() is never even called on a
        nonexistent path. os.path.isfile is patched explicitly rather than
        relying on ambient filesystem state — on a real deployment (unlike
        the dev sandbox) the report path can genuinely already exist from
        a prior real test run, which previously made this test flaky
        depending on which machine it ran on."""
        with patch("nibe_test_runner.os.path.isfile", return_value=False):
            mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client)
        self.assertFalse(attrs["report_exists"])
        self.assertEqual(attrs["report_size"], 0)

    def test_final_attrs_timestamp_key_present_and_string(self):
        """The final attrs publish must carry a 'timestamp' key (not e.g.
        a typo'd/wrong-cased key) with the formatted-timestamp value."""
        with patch("nibe_test_runner._fmt_ts", return_value="2026-08-22 12:00:00"):
            mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["timestamp"], "2026-08-22 12:00:00")

    def test_final_attrs_report_path_key_present_and_correct(self):
        """The final attrs publish must carry a 'report_path' key (not
        e.g. a typo'd/wrong-cased key) with the real report path."""
        mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["report_path"], "/homeassistant/www/nibe_test_report.html")

    def test_failure_summary_joins_fail_lines_with_real_newline(self):
        """On a failing run, multiple extracted FAILED lines must be joined
        with a real newline in the published summary."""
        output = (
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_foo.py::test_one - AssertionError: first\n"
            "FAILED tests/test_foo.py::test_two - AssertionError: second\n"
            "2 failed in 0.5s\n"
        )
        mqtt_client, *_ = self._run(proc_returncode=1, proc_stdout=output)
        attrs = self._attrs(mqtt_client)
        lines = attrs["summary"].split("\n")
        self.assertIn("tests/test_foo.py::test_one - AssertionError: first", lines)
        self.assertIn("tests/test_foo.py::test_two - AssertionError: second", lines)

    def test_pass_result_logged_via_info_not_error(self):
        """A passing run (exit_code 0) must log its 'Test suite %s in %s'
        result line through .info(), not .error() — the reverse (exit_code
        != 0 or == 1) would flip which log level a clean run shows up
        under."""
        with patch("nibe_test_runner.log_commands") as mock_log:
            self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        info_msgs = [c.args[0] for c in mock_log.info.call_args_list]
        error_msgs = [c.args[0] for c in mock_log.error.call_args_list]
        self.assertIn("Test suite %s in %s", info_msgs)
        self.assertNotIn("Test suite %s in %s (exit code %d)", error_msgs)

    def test_fail_result_logged_via_error_not_info(self):
        """A failing run (exit_code != 0) must log its result line through
        .error(), not .info()."""
        with patch("nibe_test_runner.log_commands") as mock_log:
            self._run(proc_returncode=1, proc_stdout="1 failed in 0.1s")
        info_msgs = [c.args[0] for c in mock_log.info.call_args_list]
        error_msgs = [c.args[0] for c in mock_log.error.call_args_list]
        self.assertIn("Test suite %s in %s (exit code %d)", error_msgs)
        self.assertNotIn("Test suite %s in %s", info_msgs)

    def test_pass_result_log_has_the_real_status_and_elapsed_args(self):
        """The sibling tests above only check the format string is used —
        not that its %s args are the real status/elapsed_str, not None."""
        with patch("nibe_test_runner.log_commands") as mock_log:
            self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        info_call = next(
            c for c in mock_log.info.call_args_list if c.args and c.args[0] == "Test suite %s in %s"
        )
        self.assertEqual(info_call.args[1], "passed")
        self.assertIsNotNone(info_call.args[2])

    def test_fail_result_log_has_the_real_status_elapsed_and_exit_code_args(self):
        with patch("nibe_test_runner.log_commands") as mock_log:
            self._run(proc_returncode=1, proc_stdout="1 failed in 0.1s")
        error_call = next(
            c
            for c in mock_log.error.call_args_list
            if c.args and c.args[0] == "Test suite %s in %s (exit code %d)"
        )
        self.assertEqual(error_call.args[1], "failed")
        self.assertIsNotNone(error_call.args[2])
        self.assertEqual(error_call.args[3], 1)

    def test_notification_message_exactly_at_max_notif_is_not_truncated(self):
        """The truncation must trigger on strictly-greater-than _MAX_NOTIF
        (2048), not >= — a message of exactly 2048 chars must survive
        untouched (no ellipsis inserted). A single FAILED entry's assertion
        text (n chars) appears exactly once in the body, and every other
        part of the message has fixed length for a given call, so message
        length is `n + C` for some constant C — solve for C with one
        throwaway call, then hit 2048 exactly on the real call."""

        def _stdout(n):
            return (
                "=========================== short test summary info "
                "============================\n"
                f"FAILED tests/test_x.py::test_x - {'x' * n}\n"
                "1 failed in 0.5s\n"
            )

        notify_fn = MagicMock()
        self._run(proc_returncode=1, proc_stdout=_stdout(1), notify_fn=notify_fn)
        baseline_len = len(notify_fn.call_args.kwargs["message"])
        target_n = 2048 - (baseline_len - 1)
        notify_fn.reset_mock()
        self._run(proc_returncode=1, proc_stdout=_stdout(target_n), notify_fn=notify_fn)
        message = notify_fn.call_args.kwargs["message"]
        self.assertEqual(len(message), 2048)
        self.assertNotIn("…", message)

    def test_report_size_reflects_getsize_of_report_path_when_it_exists(self):
        """When the report file does exist, report_size must be
        os.path.getsize() of the actual report_path — not a fixed/zero
        value and not the size of some other path."""
        with (
            patch("nibe_test_runner.os.path.isfile", return_value=True),
            patch("nibe_test_runner.os.path.getsize") as mock_getsize,
        ):
            mock_getsize.return_value = 123456
            mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client)
        mock_getsize.assert_called_once_with("/homeassistant/www/nibe_test_report.html")
        self.assertTrue(attrs["report_exists"])
        self.assertEqual(attrs["report_size"], 123456)

    def test_launch_error_elapsed_is_end_minus_start_not_reversed(self):
        import threading

        import nibe_test_runner
        from nibe_test_runner import run_test_suite

        mqtt_client = MagicMock()
        done_event = threading.Event()
        done_event.set()
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            return 100.0 if calls["n"] == 1 else 155.0

        with (
            patch("subprocess.Popen", side_effect=OSError("boom")),
            patch.object(nibe_test_runner.time, "monotonic", side_effect=fake_monotonic),
        ):
            run_test_suite(mqtt_client, MagicMock(), MagicMock(), lambda: "http://ha", done_event)
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["elapsed_s"], 55.0)

    def test_notification_link_uses_real_base_url(self):
        """The 'View full report' link must use the real get_base_url_fn()
        result, not a hardcoded/placeholder host."""
        _mqtt_client, notify_fn, _dismiss_fn, _ = self._run(
            proc_returncode=1,
            proc_stdout="1 failed, 0 passed in 1.0s",
            get_base_url_fn=lambda: "http://distinctive-host:9999",
        )
        message = notify_fn.call_args.kwargs["message"]
        self.assertIn("http://distinctive-host:9999/local/nibe_test_report.html?v=", message)

    def test_long_failure_message_is_truncated_to_max_notif_length(self):
        """A notification body longer than _MAX_NOTIF (2048 chars) must be
        truncated (the untruncated message would be far longer than this),
        and the truncated version must still end with the clickable report
        link so it remains useful even when cut short. The truncation
        budget (_MAX_NOTIF - 60) is sized for a typical short base URL, so
        the final length is approximately 2048, not an exact byte ceiling —
        this test checks the real contract (truncated + link preserved),
        not a precise byte count that depends on the base URL's length."""
        long_output = (
            "=========================== short test summary info ============================\n"
            + "\n".join(
                f"FAILED tests/test_x.py::TestFoo::test_bar_{i} - AssertionError: " + ("x" * 100)
                for i in range(40)
            )
            + "\n======================================================================\n"
            "40 failed in 2.00s"
        )
        _mqtt_client, notify_fn, _dismiss_fn, _ = self._run(
            proc_returncode=1,
            proc_stdout=long_output,
            get_base_url_fn=lambda: "http://ha.local:8123",
        )
        message = notify_fn.call_args.kwargs["message"]
        self.assertLess(len(message), len(long_output))  # genuinely truncated
        self.assertLess(len(message), 2300)  # roughly bounded, not runaway
        # The link carries a cache-busting ?v=<timestamp> query param, so
        # match the surrounding text via regex rather than an exact literal.
        self.assertRegex(
            message.rstrip(),
            re.escape("[View full report](http://ha.local:8123/local/nibe_test_report.html?v=")
            + r"\d+"
            + re.escape(
                ') (right-click → "Open link in new tab" — left-click opens the HA '
                "dashboard instead)"
            ),
        )

    def test_done_event_cleared_on_success(self):
        _, _, _, done_event = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        self.assertFalse(done_event.is_set())

    def test_done_event_cleared_on_failure(self):
        _, _, _, done_event = self._run(proc_returncode=1, proc_stdout="1 failed in 0.1s")
        self.assertFalse(done_event.is_set())

    def test_elapsed_under_60s_formatted_with_decimal_seconds(self):
        times = iter([1000.0, 1005.5])
        _mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        with patch("time.monotonic", side_effect=lambda: next(times)):
            mqtt_client2, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client2)
        self.assertEqual(attrs["elapsed"], "5.5s")

    def test_elapsed_over_60s_formatted_as_minutes_and_seconds(self):
        times = iter([1000.0, 1195.0])  # 195s elapsed = 3m 15s
        _mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        with patch("time.monotonic", side_effect=lambda: next(times)):
            mqtt_client2, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client2)
        self.assertEqual(attrs["elapsed"], "3m 15s")

    def test_elapsed_exactly_60s_uses_minutes_format(self):
        """The seconds/minutes format boundary is `elapsed < 60` — exactly
        60.0s must already use the 'Xm Ys' format ('1m 0s'), not '60.0s'.
        Distinguishes `<` from `<=`/`< 61`."""
        times = iter([1000.0, 1060.0])  # exactly 60.0s elapsed
        with patch("time.monotonic", side_effect=lambda: next(times)):
            mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["elapsed"], "1m 0s")

    def test_elapsed_121s_is_two_minutes_not_one(self):
        """121 // 60 == 2 but 121 // 61 == 1 — pins the divisor used for the
        minutes component against an off-by-one divisor mutation."""
        times = iter([1000.0, 1121.0])  # 121s elapsed
        with patch("time.monotonic", side_effect=lambda: next(times)):
            mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["elapsed"], "2m 1s")

    @given(elapsed=st.integers(min_value=0, max_value=59))
    def test_elapsed_under_60s_formats_as_decimal_seconds(self, elapsed):
        """For any elapsed < 60 (whole seconds, to avoid float-rounding
        ambiguity in the `:.0f` seconds format used above 60s), the format
        must be 'N.0s' — generalizes the single hand-picked 5.5s example
        elsewhere in this file."""
        times = iter([1000.0, 1000.0 + elapsed])
        with patch("time.monotonic", side_effect=lambda: next(times)):
            mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["elapsed"], f"{elapsed:.1f}s")

    @given(elapsed=st.integers(min_value=60, max_value=100_000))
    @example(elapsed=60)  # the exact boundary
    @example(elapsed=121)  # the // vs // 61 off-by-one divisor
    def test_elapsed_60s_and_above_formats_as_minutes_and_seconds(self, elapsed):
        """For any elapsed >= 60 (whole seconds), the format must be
        'Mm Ss' where M = elapsed // 60 and S = elapsed % 60 exactly —
        generalizes the two hand-picked boundary tests above to the whole
        space of minute/second decompositions."""
        times = iter([1000.0, 1000.0 + elapsed])
        with patch("time.monotonic", side_effect=lambda: next(times)):
            mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout="1 passed in 0.1s")
        attrs = self._attrs(mqtt_client)
        self.assertEqual(attrs["elapsed"], f"{elapsed // 60}m {elapsed % 60}s")

    def test_pass_summary_filters_progress_dot_noise_lines(self):
        """On a pass, pure progress-dot/percentage lines (e.g. '....... [ 50%]')
        must be filtered out of the summary — only meaningful lines (warnings,
        the final counts line) survive."""
        output = (
            "....................................................... [ 80%]\n"
            ".................                                        [100%]\n"
            "bringing up nodes...\n"
            "5 passed in 1.23s"
        )
        mqtt_client, *_ = self._run(proc_returncode=0, proc_stdout=output)
        attrs = self._attrs(mqtt_client)
        self.assertNotIn("[ 80%]", attrs["summary"])
        self.assertNotIn("bringing up nodes", attrs["summary"].lower())
        self.assertIn("5 passed in 1.23s", attrs["summary"])


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
        w._registry_map_lock = threading.Lock()
        w._stop_event = threading.Event()
        w._thread = None
        w._ws_lock = threading.Lock()
        w._current_ws = None
        w._msg_id = 0
        w._refresh_timer = None
        w._refresh_timer_lock = threading.Lock()
        w._em = em or MagicMock()
        w._pub = pub or MagicMock()
        return w

    def test_logs_registry_event_with_real_action_and_entity_id(self):
        w = self._make_watcher()
        with (
            patch.object(w, "_schedule_refresh_registry"),
            self.assertLogs("nibe.registry", level="DEBUG") as cm,
        ):
            w._handle_event(
                {
                    "data": {
                        "action": "create",
                        "entity_id": "sensor.nibe_100",
                    }
                }
            )
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Registry event: action=create, entity_id=sensor.nibe_100"
                )
                for msg in cm.output
            )
        )

    def test_logs_unknown_entity_id_when_key_absent(self):
        w = self._make_watcher()
        with self.assertLogs("nibe.registry", level="DEBUG") as cm:
            w._handle_event({"data": {"action": "some_other_action"}})
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Registry event: action=some_other_action, entity_id=unknown"
                )
                for msg in cm.output
            )
        )

    # ── create: no uid → _schedule_refresh_registry ──────────────────────────

    def test_create_no_uid_calls_schedule_refresh_registry(self):
        """create event with eid but no unique_id must call
        _schedule_refresh_registry() — the coalescing path (501→511)."""
        w = self._make_watcher()
        with patch.object(w, "_schedule_refresh_registry") as mock_sched:
            w._handle_event(
                {
                    "data": {
                        "action": "create",
                        "entity_id": "sensor.nibe_100",
                        # deliberately no 'unique_id'
                    }
                }
            )
            if w._refresh_timer is not None:
                w._refresh_timer.cancel()
        mock_sched.assert_called_once()

    # ── update: unique_id nested under 'config' ───────────────────────────────

    def test_update_unique_id_falls_back_to_nested_config_key(self):
        """When the top-level 'unique_id' key is absent but a nested
        config.unique_id is present, that nested value must be used —
        not treated as missing (which would wrongly trigger the
        _schedule_refresh_registry fallback instead of updating the map
        directly)."""
        w = self._make_watcher()
        with patch.object(w, "_schedule_refresh_registry") as mock_sched:
            w._handle_event(
                {
                    "data": {
                        "action": "update",
                        "entity_id": "sensor.nibe_100",
                        "config": {"unique_id": "nibe_100"},
                        # deliberately no top-level 'unique_id'
                    }
                }
            )
        mock_sched.assert_not_called()
        self.assertEqual(w._unique_id_map.get("nibe_100"), "sensor.nibe_100")

    # ── update: no uid → _schedule_refresh_registry ──────────────────────────

    def test_update_no_uid_calls_schedule_refresh_registry(self):
        """update event with eid but no unique_id must call
        _schedule_refresh_registry() — the coalescing path (518→524)."""
        w = self._make_watcher()
        with patch.object(w, "_schedule_refresh_registry") as mock_sched:
            w._handle_event(
                {
                    "data": {
                        "action": "update",
                        "entity_id": "sensor.nibe_100",
                        # deliberately no 'unique_id'
                    }
                }
            )
            if w._refresh_timer is not None:
                w._refresh_timer.cancel()
        mock_sched.assert_called_once()

    # ── remove: no uid → _schedule_refresh_registry ──────────────────────────

    def test_remove_no_uid_calls_schedule_refresh_registry(self):
        """Regression: remove event with eid but no unique_id must call
        _schedule_refresh_registry(), mirroring the create/update fallback
        above — otherwise, if HA's remove event genuinely lacks unique_id
        (the registry entry being deleted isn't necessarily echoed back in
        full), the reverse pop by uid can't happen and the stale
        _unique_id_map entry never clears until the next full
        refresh_registry()/reconnect."""
        w = self._make_watcher()
        with patch.object(w, "_schedule_refresh_registry") as mock_sched:
            w._handle_event(
                {
                    "data": {
                        "action": "remove",
                        "entity_id": "sensor.nibe_100",
                        # deliberately no 'unique_id'
                    }
                }
            )
            if w._refresh_timer is not None:
                w._refresh_timer.cancel()
        mock_sched.assert_called_once()

    def test_remove_with_uid_does_not_call_schedule_refresh_registry(self):
        """When unique_id IS present, remove must take the direct pop path,
        not the refresh fallback — no wasted WebSocket round-trip."""
        w = self._make_watcher()
        w._unique_id_map["nibe_100"] = "sensor.nibe_100"
        with patch.object(w, "_schedule_refresh_registry") as mock_sched:
            w._handle_event(
                {
                    "data": {
                        "action": "remove",
                        "entity_id": "sensor.nibe_100",
                        "unique_id": "nibe_100",
                    }
                }
            )
        mock_sched.assert_not_called()
        self.assertNotIn("nibe_100", w._unique_id_map)

    # ── update: disabled_by change → _on_entity_enabled / _disabled ──────────

    def test_update_disabled_by_user_calls_on_entity_enabled(self):
        """update with changes={disabled_by: 'user'} means the entity WAS
        disabled and is now enabled → _on_entity_enabled must be called (527→529)."""
        w = self._make_watcher()
        with patch.object(w, "_on_entity_enabled") as mock_enabled:
            w._handle_event(
                {
                    "data": {
                        "action": "update",
                        "entity_id": "switch.nibe_100",
                        "unique_id": "nibe_100",
                        "changes": {"disabled_by": "user"},
                    }
                }
            )
        mock_enabled.assert_called_once_with("switch.nibe_100")

    def test_update_disabled_by_none_calls_on_entity_disabled(self):
        """update with changes={disabled_by: None} means the entity WAS
        enabled and is now disabled → _on_entity_disabled must be called (529→531)."""
        w = self._make_watcher()
        with patch.object(w, "_on_entity_disabled") as mock_disabled:
            w._handle_event(
                {
                    "data": {
                        "action": "update",
                        "entity_id": "switch.nibe_100",
                        "unique_id": "nibe_100",
                        "changes": {"disabled_by": None},
                    }
                }
            )
        mock_disabled.assert_called_once_with("switch.nibe_100")

    def test_update_disabled_by_other_value_calls_neither(self):
        """update with changes={disabled_by: 'integration'} (not user, not None)
        must call neither _on_entity_enabled nor _on_entity_disabled."""
        w = self._make_watcher()
        with (
            patch.object(w, "_on_entity_enabled") as mock_en,
            patch.object(w, "_on_entity_disabled") as mock_dis,
        ):
            w._handle_event(
                {
                    "data": {
                        "action": "update",
                        "entity_id": "switch.nibe_100",
                        "unique_id": "nibe_100",
                        "changes": {"disabled_by": "integration"},
                    }
                }
            )
        mock_en.assert_not_called()
        mock_dis.assert_not_called()

    # ── remove: no uid → map unchanged (537→539 False branch) ────────────────

    def test_remove_without_uid_does_not_touch_map(self):
        """remove event with no unique_id (uid is None/falsy) must not
        attempt to pop from _unique_id_map — the if uid: False branch (537→539)."""
        w = self._make_watcher()
        w._unique_id_map["nibe_100"] = "sensor.nibe_100"
        w._handle_event(
            {
                "data": {
                    "action": "remove",
                    "entity_id": "sensor.nibe_100",
                    # deliberately no 'unique_id'
                }
            }
        )
        # Map must be unchanged
        self.assertIn("nibe_100", w._unique_id_map)


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
        _hi._ha_base_url_retry_after = 0.0

    def tearDown(self):
        import nibe_ha_integration as _hi

        _hi._ha_base_url = None
        _hi._ha_base_url_retry_after = 0.0

    def _mock_api(self, response_dict):
        """Return a context manager that mocks the supervisor config API."""
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(response_dict).encode()
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def test_returns_internal_url_when_present(self):
        from nibe_ha_integration import _get_ha_base_url

        cfg = {"internal_url": "http://192.168.1.10:8123", "external_url": "https://my.nabu.casa"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api(cfg):
            result = _get_ha_base_url()
        self.assertEqual(result, "http://192.168.1.10:8123")

    def test_falls_back_to_external_url(self):
        from nibe_ha_integration import _get_ha_base_url

        cfg = {"internal_url": "", "external_url": "https://my.nabu.casa"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api(cfg):
            result = _get_ha_base_url()
        self.assertEqual(result, "https://my.nabu.casa")

    def test_returns_empty_string_without_supervisor_token(self):
        from nibe_ha_integration import _get_ha_base_url

        with patch.dict("os.environ", {}, clear=True):
            result = _get_ha_base_url()
        self.assertEqual(result, "")

    def test_returns_empty_string_on_api_error(self):
        from nibe_ha_integration import _get_ha_base_url

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
        ):
            result = _get_ha_base_url()
        self.assertEqual(result, "")

    def test_caches_result_after_first_fetch(self):
        from nibe_ha_integration import _get_ha_base_url

        cfg = {"internal_url": "http://192.168.1.10:8123"}
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            self._mock_api(cfg) as mock_open,
        ):
            _get_ha_base_url()
            _get_ha_base_url()  # second call
        # urlopen must only be called once — second call uses cache
        self.assertEqual(mock_open.call_count, 1)

    def test_trailing_slash_stripped(self):
        from nibe_ha_integration import _get_ha_base_url

        cfg = {"internal_url": "http://192.168.1.10:8123/"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api(cfg):
            result = _get_ha_base_url()
        self.assertFalse(result.endswith("/"))

    def test_api_error_is_not_cached_forever(self):
        """A transient failure (e.g. Supervisor not up yet at startup) must
        not permanently poison every future notification link — unlike a
        missing SUPERVISOR_TOKEN, a fetch failure is retried after a
        cooldown rather than cached as '' forever."""
        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_base_url

        cfg = {"internal_url": "http://192.168.1.10:8123"}
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
        ):
            first = _get_ha_base_url()
        self.assertEqual(first, "")
        # Still within the cooldown window — no new attempt yet.
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            self._mock_api(cfg) as mock_open,
        ):
            still_empty = _get_ha_base_url()
        self.assertEqual(still_empty, "")
        mock_open.assert_not_called()
        # Cooldown elapsed — the next call retries and succeeds.
        _hi._ha_base_url_retry_after = 0.0
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api(cfg):
            recovered = _get_ha_base_url()
        self.assertEqual(recovered, "http://192.168.1.10:8123")

    def test_request_built_with_correct_url_headers_and_method(self):
        """The urllib.request.Request passed to urlopen must use the real
        supervisor config URL, a Bearer-auth Authorization header built from
        the real token, and method GET — urlopen is mocked in every other
        test here, so nothing else verifies what was actually requested."""
        from nibe_ha_integration import _get_ha_base_url

        cfg = {"internal_url": "http://192.168.1.10:8123"}
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "sekrit-tok"}),
            self._mock_api(cfg) as mock_open,
        ):
            _get_ha_base_url()
        req = mock_open.call_args.args[0]
        self.assertEqual(req.full_url, "http://supervisor/core/api/config")
        self.assertEqual(req.get_header("Authorization"), "Bearer sekrit-tok")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(mock_open.call_args.kwargs.get("timeout"), 5)

    def test_returns_empty_string_when_neither_url_present(self):
        """Neither internal_url nor external_url present (both falsy) must
        fall back to '' — not None or some other placeholder."""
        from nibe_ha_integration import _get_ha_base_url

        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api({}):
            result = _get_ha_base_url()
        self.assertEqual(result, "")

    def test_only_trailing_slashes_stripped_not_internal_ones(self):
        """rstrip('/') must strip only trailing slashes, not internal path
        separators — pins the exact stripped character set."""
        from nibe_ha_integration import _get_ha_base_url

        cfg = {"internal_url": "http://192.168.1.10:8123/api//"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api(cfg):
            result = _get_ha_base_url()
        self.assertEqual(result, "http://192.168.1.10:8123/api")

    def test_success_logs_resolved_url_verbatim(self):
        from nibe_ha_integration import _get_ha_base_url

        cfg = {"internal_url": "http://192.168.1.10:8123"}
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            self._mock_api(cfg),
            self.assertLogs("nibe.mqtt", level="DEBUG") as cm,
        ):
            _get_ha_base_url()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("HA base URL resolved: 'http://192.168.1.10:8123'")
                for msg in cm.output
            )
        )

    def test_api_error_logs_warning_verbatim(self):
        from nibe_ha_integration import _get_ha_base_url

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
            self.assertLogs("nibe.mqtt", level="WARNING") as cm,
        ):
            _get_ha_base_url()
        self.assertTrue(
            any(
                msg.splitlines()[0] == "WARNING:nibe.mqtt:Could not fetch HA base URL: refused"
                for msg in cm.output
            )
        )

    def test_retry_cooldown_boundary_allows_a_fetch_at_the_exact_instant(self):
        """`now < _ha_base_url_retry_after` means the cooldown has NOT yet
        elapsed at `now == retry_after` exactly — a fetch is allowed to
        proceed at that instant. A `<=` mutant would still block it, one
        instant too long."""
        import time as _time

        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_base_url

        boundary = _time.time() + 10
        _hi._ha_base_url_retry_after = boundary
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("time.time", return_value=boundary),
            self._mock_api({"internal_url": "http://192.168.1.10:8123"}) as mock_open,
        ):
            result = _get_ha_base_url()
        self.assertEqual(result, "http://192.168.1.10:8123")
        mock_open.assert_called_once()

    @given(
        delta=st.floats(min_value=-120.0, max_value=-0.001, allow_nan=False, allow_infinity=False)
    )
    def test_retry_cooldown_blocks_the_fetch_for_any_positive_delta(self, delta):
        """For any retry_after strictly in the future (now < retry_after,
        i.e. now - retry_after == delta < 0), the fetch must be blocked —
        mirrors the same property already added for _get_ha_language,
        generalizing the single hand-picked delta example above."""
        import time as _time

        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_base_url

        _hi._ha_base_url = None
        now = _time.time()
        _hi._ha_base_url_retry_after = now - delta  # delta<0 => retry_after>now
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("time.time", return_value=now),
            patch("urllib.request.urlopen") as mock_open,
        ):
            result = _get_ha_base_url()
        self.assertEqual(result, "")
        mock_open.assert_not_called()

    @given(delta=st.floats(min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False))
    def test_retry_cooldown_allows_the_fetch_for_any_non_positive_delta(self, delta):
        """The complementary case: now >= retry_after must always allow
        the fetch to proceed."""
        import time as _time

        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_base_url

        _hi._ha_base_url = None
        now = _time.time()
        _hi._ha_base_url_retry_after = now - delta  # delta>=0 => retry_after<=now
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("time.time", return_value=now),
            self._mock_api({"internal_url": "http://192.168.1.10:8123"}) as mock_open,
        ):
            result = _get_ha_base_url()
        self.assertEqual(result, "http://192.168.1.10:8123")
        mock_open.assert_called_once()

    def test_cached_value_is_empty_string_not_none(self):
        """Without a supervisor token, the cached _ha_base_url must be the
        empty string, not None — callers rely on f'{_get_ha_base_url()}/...'
        producing a clean relative path, and a stray 'None/...' string would
        be a broken, confusing URL rather than a graceful fallback."""
        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_base_url

        with patch.dict("os.environ", {}, clear=True):
            result = _get_ha_base_url()
        self.assertEqual(result, "")
        self.assertIsInstance(result, str)
        self.assertEqual(_hi._ha_base_url, "")

    def test_missing_token_is_still_cached_forever(self):
        """Unlike a network failure, a missing SUPERVISOR_TOKEN is a
        permanent condition for the process lifetime (the environment
        doesn't change without a restart), so it should NOT be retried."""
        from nibe_ha_integration import _get_ha_base_url

        with patch.dict("os.environ", {}, clear=True):
            first = _get_ha_base_url()
        self.assertEqual(first, "")
        with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen") as mock_open:
            second = _get_ha_base_url()
        self.assertEqual(second, "")
        mock_open.assert_not_called()


# ===========================================================================
# _get_ha_language — supervisor API fetch and caching
# ===========================================================================


class TestGetHaLanguage(unittest.TestCase):
    """Tests for _get_ha_language():

    • Returns HA's configured language when present
    • Returns '' when language is absent from the supervisor response
    • Returns '' when no supervisor token
    • Returns '' and logs warning when supervisor API call fails
    • Caches the result after first successful fetch
    • Independent of _get_ha_base_url's own cache/retry state
    """

    def setUp(self):
        import nibe_ha_integration as _hi

        _hi._ha_language = None
        _hi._ha_language_retry_after = 0.0

    def tearDown(self):
        import nibe_ha_integration as _hi

        _hi._ha_language = None
        _hi._ha_language_retry_after = 0.0

    def _mock_api(self, response_dict):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(response_dict).encode()
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def test_returns_language_when_present(self):
        from nibe_ha_integration import _get_ha_language

        cfg = {"language": "nl", "internal_url": "http://192.168.1.10:8123"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api(cfg):
            result = _get_ha_language()
        self.assertEqual(result, "nl")

    def test_returns_empty_string_when_language_absent(self):
        from nibe_ha_integration import _get_ha_language

        cfg = {"internal_url": "http://192.168.1.10:8123"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api(cfg):
            result = _get_ha_language()
        self.assertEqual(result, "")

    def test_no_supervisor_token_returns_empty_string(self):
        from nibe_ha_integration import _get_ha_language

        with patch.dict("os.environ", {}, clear=True):
            result = _get_ha_language()
        self.assertEqual(result, "")

    def test_no_supervisor_token_caches_empty_string_not_none(self):
        """The no-token result must be cached as '' (which passes the
        module-level `is not None` cache guard), not left as None — a
        None result would defeat the cache and re-check os.environ on
        every subsequent call. Distinguishes this permanent-no-token case
        from the API-failure case, which deliberately stays uncached
        (None) so it can be retried after the cooldown."""
        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_language

        with patch.dict("os.environ", {}, clear=True):
            _get_ha_language()
        self.assertEqual(_hi._ha_language, "")

    def test_api_failure_returns_empty_string_and_is_retryable(self):
        """A failed fetch must not be cached forever — it's retried after
        the cooldown, unlike a permanently-missing SUPERVISOR_TOKEN."""
        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_language

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=OSError("unreachable")),
        ):
            result = _get_ha_language()
        self.assertEqual(result, "")
        self.assertIsNone(_hi._ha_language)  # not cached — only the cooldown was set
        self.assertGreater(_hi._ha_language_retry_after, 0.0)

    def test_result_cached_after_first_successful_fetch(self):
        """A second call must not re-hit the network."""
        from nibe_ha_integration import _get_ha_language

        cfg = {"language": "de"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api(cfg):
            first = _get_ha_language()
        self.assertEqual(first, "de")
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            second = _get_ha_language()
        self.assertEqual(second, "de")
        mock_open.assert_not_called()

    def test_retry_cooldown_is_a_future_timestamp_not_past(self):
        """After a failed fetch, _ha_language_retry_after must be set to a
        point in the FUTURE (now + cooldown) — a `-` in place of the `+`
        here would set it in the past, defeating the cooldown entirely and
        letting every subsequent call immediately retry the network."""
        import time as _time

        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_language

        before = _time.time()
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=OSError("unreachable")),
        ):
            _get_ha_language()
        self.assertGreater(_hi._ha_language_retry_after, before)

    def test_retry_cooldown_boundary_allows_a_fetch_at_the_exact_instant(self):
        """`now < _ha_language_retry_after` means the cooldown has NOT yet
        elapsed at `now == retry_after` exactly — the retry is allowed to
        proceed at that instant. A `<=` mutant would instead still block
        it, one instant too long."""
        import time as _time

        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_language

        boundary = _time.time() + 10
        _hi._ha_language_retry_after = boundary
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("time.time", return_value=boundary),
            self._mock_api({"language": "fr"}) as mock_open,
        ):
            result = _get_ha_language()
        self.assertEqual(result, "fr")
        mock_open.assert_called_once()

    def test_retry_cooldown_still_active_returns_empty_without_network_call(self):
        """While the cooldown from a prior failed fetch is still active
        (now < retry_after), _get_ha_language must return '' immediately
        without attempting another network call — the actually-blocked
        case, distinct from the exact-boundary-allows-it test above."""
        import time as _time

        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_language

        _hi._ha_language_retry_after = _time.time() + 60  # well within cooldown
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen") as mock_open,
        ):
            result = _get_ha_language()
        self.assertEqual(result, "")
        mock_open.assert_not_called()

    @given(
        delta=st.floats(min_value=-120.0, max_value=-0.001, allow_nan=False, allow_infinity=False)
    )
    def test_retry_cooldown_blocks_the_fetch_for_any_positive_delta(self, delta):
        """For any retry_after strictly in the future (now < retry_after,
        i.e. now - retry_after == delta < 0), the fetch must be blocked —
        generalizes the single hand-picked delta=-60 example above to the
        whole space of 'still within cooldown' offsets."""
        import time as _time

        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_language

        _hi._ha_language = None  # @given re-invokes this body per example; setUp only runs once
        now = _time.time()
        _hi._ha_language_retry_after = now - delta  # delta<0 => retry_after>now
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("time.time", return_value=now),
            patch("urllib.request.urlopen") as mock_open,
        ):
            result = _get_ha_language()
        self.assertEqual(result, "")
        mock_open.assert_not_called()

    @given(delta=st.floats(min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False))
    def test_retry_cooldown_allows_the_fetch_for_any_non_positive_delta(self, delta):
        """For any retry_after at or before now (now >= retry_after, i.e.
        now - retry_after == delta >= 0), the cooldown has elapsed and the
        fetch must proceed — the complementary case to the property above."""
        import time as _time

        import nibe_ha_integration as _hi
        from nibe_ha_integration import _get_ha_language

        _hi._ha_language = None  # @given re-invokes this body per example; setUp only runs once
        now = _time.time()
        _hi._ha_language_retry_after = now - delta  # delta>=0 => retry_after<=now
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("time.time", return_value=now),
            self._mock_api({"language": "fr"}) as mock_open,
        ):
            result = _get_ha_language()
        self.assertEqual(result, "fr")
        mock_open.assert_called_once()

    def test_independent_of_get_ha_base_url_cache(self):
        """_get_ha_language() must not be blocked or skipped just because
        _get_ha_base_url()'s own cache/retry-cooldown state is set — the two
        must fetch independently, since one's failure must not silently
        disable the other."""
        import nibe_ha_integration as _hi

        _hi._ha_base_url = "http://cached-from-earlier:8123"
        _hi._ha_base_url_retry_after = 0.0
        try:
            from nibe_ha_integration import _get_ha_language

            cfg = {"language": "sv"}
            with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}), self._mock_api(cfg):
                result = _get_ha_language()
            self.assertEqual(result, "sv")
        finally:
            _hi._ha_base_url = None
            _hi._ha_base_url_retry_after = 0.0

    def test_request_built_with_correct_url_headers_and_method(self):
        from nibe_ha_integration import _get_ha_language

        cfg = {"language": "nl"}
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "sekrit-tok"}),
            self._mock_api(cfg) as mock_open,
        ):
            _get_ha_language()
        req = mock_open.call_args.args[0]
        self.assertEqual(req.full_url, "http://supervisor/core/api/config")
        self.assertEqual(req.get_header("Authorization"), "Bearer sekrit-tok")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(mock_open.call_args.kwargs.get("timeout"), 5)

    def test_success_logs_resolved_language_verbatim(self):
        from nibe_ha_integration import _get_ha_language

        cfg = {"language": "nl"}
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            self._mock_api(cfg),
            self.assertLogs("nibe.mqtt", level="DEBUG") as cm,
        ):
            _get_ha_language()
        self.assertTrue(
            any(msg.splitlines()[0].endswith("HA language resolved: 'nl'") for msg in cm.output)
        )

    def test_api_error_logs_warning_verbatim(self):
        from nibe_ha_integration import _get_ha_language

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
            self.assertLogs("nibe.mqtt", level="WARNING") as cm,
        ):
            _get_ha_language()
        self.assertTrue(
            any(
                msg.splitlines()[0] == "WARNING:nibe.mqtt:Could not fetch HA language: refused"
                for msg in cm.output
            )
        )


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
        w._registry_map_lock = threading.Lock()
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
        w._unique_id_map["nibe_pre"] = "sensor.pre"  # pre-existing entry
        ws = MagicMock()
        ws.recv.side_effect = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
            json.dumps({"id": 1, "type": "result", "success": False, "error": {"code": "unknown"}}),
        ]
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("websocket.create_connection", return_value=ws),
        ):
            w.refresh_registry()
        # Map must not be populated from the failed response
        self.assertNotIn("nibe_100", w._unique_id_map)
        # The pre-existing entry is preserved (not cleared)
        self.assertIn("nibe_pre", w._unique_id_map)


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
        w._registry_map_lock = threading.Lock()
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
                return json.dumps({"type": "result", "id": 99, "success": True, "result": {}})
            w._stop_event.set()
            return json.dumps({"type": "pong"})

        ws.recv.side_effect = recv_side_effect
        with (
            patch.object(w, "_connect_and_subscribe", return_value=ws),
            patch.object(w, "_handle_event") as mock_event,
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
        ):
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
        w._registry_map_lock = threading.Lock()
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
        with patch.object(w, "_schedule_refresh_registry") as mock_sched:
            w._handle_event(
                {
                    "data": {
                        "action": "create",
                        # deliberately no 'entity_id' key at all
                    }
                }
            )
        mock_sched.assert_not_called()

    def test_update_no_eid_does_not_schedule_refresh(self):
        """update event with no entity_id (eid=None) — elif eid is False —
        must not call _schedule_refresh_registry (561→567 False)."""
        w = self._make_watcher()
        with patch.object(w, "_schedule_refresh_registry") as mock_sched:
            w._handle_event(
                {
                    "data": {
                        "action": "update",
                        # deliberately no 'entity_id' key
                    }
                }
            )
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
        w._registry_map_lock = threading.Lock()
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
        with patch("nibe_ha_integration.notify_ha"):
            w._on_entity_enabled("switch.nibe_100")
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
                    return {"is_dynamic": True, "display_title": "THS-10 Humidity"}
                return None  # second call: simulates concurrent removal
            return real_dict.get(key, default)

        em.all_points_by_id = MagicMock()
        em.all_points_by_id.get = MagicMock(side_effect=get_side_effect)
        pub = MagicMock()
        w = self._make_watcher(em, pub)
        with patch("nibe_ha_integration.notify_ha"):
            w._on_entity_disabled("sensor.nibe_50827")
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

    def test_publish_stats_called_with_real_values(self):
        """publisher.publish_stats() must receive the real counts/dicts from
        entity_manager — not just be called with *some* arguments. Every
        other test in this file mocks _publish_stats out entirely, so
        nothing else verifies these kwargs."""
        from nibe_ha_integration import _publish_stats

        em = _make_em()
        em.active_entities_by_id = {1: {}, 2: {}, 3: {}}
        em.all_points_by_id = {i: {} for i in range(10)}
        em.mqtt_enabled_points = {1, 2, 3, 4}
        em._stats_type_counts = {"sensor": 5, "switch": 2}
        em._stats_category_counts = {"temperature": 3}
        em._stats_writable_count = 7
        em._write_total = 100
        em._write_success = 95
        em._write_failed = 5
        pub = MagicMock()
        _publish_stats(em, pub)
        pub.publish_stats.assert_called_once_with(
            all_points_count=10,
            mqtt_enabled_count=4,
            active_count=3,
            type_counts={"sensor": 5, "switch": 2},
            category_counts={"temperature": 3},
            writable_count=7,
            write_total=100,
            write_success=95,
            write_failed=5,
        )

    def test_publish_stats_first_call_with_no_cached_key_does_not_raise(self):
        """The very first _publish_stats() call on a fresh entity_manager
        (before _last_stats_key has ever been set) must not raise
        AttributeError — getattr()'s None default is what makes a missing
        attribute compare as 'changed' rather than crash."""
        from contextlib import nullcontext

        from nibe_ha_integration import _publish_stats

        class _BareEntityManager:
            _active_entities_lock = nullcontext()
            active_entities_by_id: ClassVar[dict] = {}
            all_points_by_id: ClassVar[dict] = {}
            mqtt_enabled_points: ClassVar[set] = set()
            _stats_type_counts: ClassVar[dict] = {}
            _stats_category_counts: ClassVar[dict] = {}
            _stats_writable_count = 0
            _write_total = 0
            _write_success = 0
            _write_failed = 0
            # deliberately no _last_stats_key attribute at all

        em = _BareEntityManager()
        pub = MagicMock()
        _publish_stats(em, pub)  # must not raise AttributeError
        self.assertTrue(pub.publish_stats.called)
        self.assertEqual(em._last_stats_key, (0, 0, 0))

    def test_stats_key_changed_logs_and_updates_cached_key(self):
        """When the stats key genuinely changes between calls, the debug
        log must fire and _last_stats_key must be updated to the NEW key
        — pins both the `!=` comparison direction and that the cached key
        is actually replaced (not left stale)."""
        from nibe_ha_integration import _publish_stats

        em = _make_em()
        em.all_points_by_id = {1: {}}
        pub = MagicMock()
        _publish_stats(em, pub)  # first call establishes a cached key
        em.all_points_by_id = {1: {}, 2: {}}  # total_count changes
        with self.assertLogs("nibe.stats", level="DEBUG") as cm:
            _publish_stats(em, pub)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Stats: MQTT=0, Active=0, Total=2")
                for msg in cm.output
            )
        )
        self.assertEqual(em._last_stats_key, (0, 0, 2))

    def test_stats_key_unchanged_does_not_log(self):
        """The dedup guard must actually suppress the log (not just leave
        the cached key alone) when the stats key repeats — an `==` mutant
        in place of `!=` would invert this and log on every unchanged
        call instead of every changed one."""
        from nibe_ha_integration import _publish_stats

        em = _make_em()
        pub = MagicMock()
        _publish_stats(em, pub)
        with self.assertNoLogs("nibe.stats", level="DEBUG"):
            _publish_stats(em, pub)

    def test_stats_key_unchanged_skips_log_update(self):
        """Calling _publish_stats twice with identical state must skip the
        debug log on the second call (1167→exit False branch)."""
        from nibe_ha_integration import _publish_stats

        em = _make_em()
        pub = MagicMock()
        # First call: stats_key differs from None → debug log fires, key stored
        _publish_stats(em, pub)
        stored_key = getattr(em, "_last_stats_key", None)
        # Second call: same em state → stats_key == _last_stats_key → 1167→exit
        _publish_stats(em, pub)
        self.assertEqual(getattr(em, "_last_stats_key", None), stored_key)
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

        self.em = _make_em()
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

        handler = ManagementCommandHandler(self.em.mqtt, self.em, self.pub, self.exe)
        handler._handle_snapshot_cmd(None, None, msg)
        self.exe.shutdown(wait=True)
        import concurrent.futures

        self.exe = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def test_save_action_calls_save_snapshot(self):
        with patch.object(self.em, "save_snapshot", return_value=(True, "ok")) as mock_save:
            self._send({"action": "save", "name": "Test"})
        mock_save.assert_called_once_with("Test")

    def test_restore_action_calls_restore_snapshot(self):
        with patch.object(self.em, "restore_snapshot", return_value=(True, "ok")) as mock_restore:
            self._send({"action": "restore", "name": "Test", "mode": "merge"})
        mock_restore.assert_called_once_with("Test", "merge")

    def test_restore_defaults_to_flush(self):
        with patch.object(self.em, "restore_snapshot", return_value=(True, "ok")) as mock_restore:
            self._send({"action": "restore", "name": "Test"})
        mock_restore.assert_called_once_with("Test", "flush")

    def test_missing_name_key_defaults_to_empty_string_not_none(self):
        """cmd.get('name', '') must default to '' — not None, which would
        crash the following .strip() call with AttributeError instead of
        gracefully treating an omitted name as empty."""
        with patch.object(self.em, "save_snapshot", return_value=(True, "ok")) as mock_save:
            self._send({"action": "save"})  # no 'name' key at all
        mock_save.assert_called_once_with("")

    def test_invalid_payload_error_log_has_the_real_exception(self):
        import json as _json

        msg = MagicMock()
        msg.payload = b"not valid json {{{"
        from nibe_ha_integration import ManagementCommandHandler

        handler = ManagementCommandHandler(self.em.mqtt, self.em, self.pub, self.exe)
        with patch("nibe_ha_integration.log_commands") as mock_log:
            handler._handle_snapshot_cmd(None, None, msg)
        error_call = mock_log.error.call_args
        self.assertEqual(error_call.args[0], "snapshot_cmd: invalid payload: %s")
        self.assertIsInstance(error_call.args[1], _json.JSONDecodeError)

    def test_restore_invalid_mode_defaults_to_flush(self):
        with (
            patch.object(self.em, "restore_snapshot", return_value=(True, "ok")) as mock_restore,
            self.assertLogs("nibe.commands", level="ERROR") as cm,
        ):
            self._send({"action": "restore", "name": "Test", "mode": "invalid"})
        mock_restore.assert_called_once_with("Test", "flush")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "snapshot_cmd restore: unknown mode 'invalid' — expected 'flush' or "
                    "'merge', using flush"
                )
                for msg in cm.output
            )
        )

    def test_restore_explicit_flush_mode_does_not_log_error(self):
        """'flush' passed explicitly must be recognised as valid — a
        mutated membership check that no longer matches the real string
        'flush' would still end up calling restore_snapshot with 'flush'
        (the fallback), silently masking itself unless the absence of the
        warning log is also checked."""
        with (
            patch.object(self.em, "restore_snapshot", return_value=(True, "ok")),
            self.assertRaises(AssertionError),
            self.assertLogs("nibe.commands", level="ERROR"),
        ):
            # assertLogs raises if nothing was logged — that's the
            # expected (passing) outcome here.
            self._send({"action": "restore", "name": "Test", "mode": "flush"})

    def test_restore_explicit_merge_mode_does_not_log_error(self):
        with (
            patch.object(self.em, "restore_snapshot", return_value=(True, "ok")),
            self.assertRaises(AssertionError),
            self.assertLogs("nibe.commands", level="ERROR"),
        ):
            self._send({"action": "restore", "name": "Test", "mode": "merge"})

    def test_restore_failure_does_not_publish_stats(self):
        with (
            patch.object(self.em, "restore_snapshot", return_value=(False, "snapshot not found")),
            patch("nibe_ha_integration._publish_stats") as mock_stats,
        ):
            self._send({"action": "restore", "name": "Missing"})
        mock_stats.assert_not_called()

    def test_restore_success_publishes_stats_with_correct_arguments(self):
        """On successful restore, _publish_stats must be called with the
        real (entity_manager, publisher) pair — not a wrong argument count
        or order. The failure-path test above only checks 'not called'."""
        with (
            patch.object(self.em, "restore_snapshot", return_value=(True, "restored")),
            patch("nibe_ha_integration._publish_stats") as mock_stats,
        ):
            self._send({"action": "restore", "name": "Test"})
        mock_stats.assert_called_once_with(self.em, self.pub)

    def test_missing_action_key_treated_as_unknown(self):
        """cmd.get('action', '') — when the 'action' key is entirely
        absent (not just empty), must default to '' and fall into the
        unknown-action branch, not crash or silently succeed."""
        with (
            patch.object(self.em, "save_snapshot") as ms,
            patch.object(self.em, "restore_snapshot") as mr,
            patch.object(self.em, "delete_snapshot") as md,
            self.assertLogs("nibe.commands", level="ERROR") as cm,
        ):
            self._send({"name": "Test"})  # no 'action' key
        ms.assert_not_called()
        mr.assert_not_called()
        md.assert_not_called()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "snapshot_cmd: unknown action '' — expected 'save', 'restore', or 'delete'"
                )
                for msg in cm.output
            )
        )

    def test_delete_action_calls_delete_snapshot(self):
        with patch.object(self.em, "delete_snapshot", return_value=(True, "ok")) as mock_delete:
            self._send({"action": "delete", "name": "Test"})
        mock_delete.assert_called_once_with("Test")

    def test_unknown_action_is_ignored(self):
        with (
            patch.object(self.em, "save_snapshot") as ms,
            patch.object(self.em, "restore_snapshot") as mr,
            patch.object(self.em, "delete_snapshot") as md,
            self.assertLogs("nibe.commands", level="ERROR") as cm,
        ):
            self._send({"action": "unknown", "name": "Test"})
        ms.assert_not_called()
        mr.assert_not_called()
        md.assert_not_called()
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "snapshot_cmd: unknown action 'unknown' — expected 'save', 'restore', "
                    "or 'delete'"
                )
                for msg in cm.output
            )
        )

    def test_successful_action_logs_final_result_line(self):
        with (
            patch.object(self.em, "save_snapshot", return_value=(True, "Saved as Test")),
            self.assertLogs("nibe.commands", level="INFO") as cm,
        ):
            self._send({"action": "save", "name": "Test"})
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("snapshot_cmd save 'Test': Saved as Test")
                for msg in cm.output
            )
        )

    def test_invalid_json_payload_is_ignored(self):
        msg = MagicMock()
        msg.payload = b"not valid json"
        with (
            patch.object(self.em, "save_snapshot") as ms,
            self.assertLogs("nibe.commands", level="ERROR") as cm,
        ):
            from nibe_ha_integration import ManagementCommandHandler

            handler = ManagementCommandHandler(self.em.mqtt, self.em, self.pub, self.exe)
            handler._handle_snapshot_cmd(None, None, msg)
            self.exe.shutdown(wait=True)
        ms.assert_not_called()
        self.assertTrue(
            any(
                msg_.splitlines()[0].startswith(
                    "ERROR:nibe.commands:snapshot_cmd: invalid payload: "
                )
                for msg_ in cm.output
            )
        )

    def test_non_dict_json_payload_does_not_raise(self):
        """Valid JSON that isn't an object (e.g. a bare number, null, or a
        list) must not crash — .get() on a non-dict would otherwise raise
        AttributeError directly on the MQTT client's own thread."""
        for payload, expected_repr in (
            (b"5", "5"),
            (b"null", "None"),
            (b'["x"]', "['x']"),
            (b'"just a string"', "'just a string'"),
        ):
            msg = MagicMock()
            msg.payload = payload
            with (
                patch.object(self.em, "save_snapshot") as ms,
                self.assertLogs("nibe.commands", level="ERROR") as cm,
            ):
                from nibe_ha_integration import ManagementCommandHandler

                handler = ManagementCommandHandler(self.em.mqtt, self.em, self.pub, self.exe)
                handler._handle_snapshot_cmd(None, None, msg)  # must not raise
                self.exe.shutdown(wait=True)
                import concurrent.futures

                self.exe = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            ms.assert_not_called()
            self.assertTrue(
                any(
                    msg_.splitlines()[0].endswith(
                        f"snapshot_cmd: expected a JSON object, got {expected_repr}"
                    )
                    for msg_ in cm.output
                ),
                f"payload={payload!r}",
            )
