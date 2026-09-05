"""Integration tests: this bridge's MQTT discovery/cleanup logic against a
real MQTT broker.

Every other test file proves the publisher/EntityManager call the right
client methods with the right arguments, using a MagicMock in place of a
real broker connection. That is not the same as proving those calls produce
the wire behaviour Home Assistant's MQTT integration actually depends on —
retained messages really are retained, and really are cleared by an empty
retained payload; a real broker's retained-message delivery order across
*different* topics is not something a mock can expose, because the mock's
delivery order is whatever the test script says it is.

That gap is exactly what let the GitHub issue #23 regression through: a
point reclassified between entity types (binary_sensor -> sensor) left its
old discovery topic orphaned in Home Assistant, because the cleanup logic's
first fix depended on which of two *different* retained topics a real
broker happened to deliver first during EntityManager.scan_mqtt_discovery()
-- behaviour no mocked-client test could exercise, since the mock's
"broker" only ever delivers messages in whatever order the test itself
scripts. This suite reproduces that regression against a real mosquitto
broker as a permanent guard against it recurring, and separately proves
the more basic wire-level claims (a discovery config is valid, retained
messages persist for a late subscriber) that the rest of the suite assumes
but has never actually verified end to end.

Skipped unless NIBE_MQTT_TEST_HOST points at a disposable broker:

    ./dev/mosquitto.sh start
    NIBE_MQTT_TEST_HOST=127.0.0.1 NIBE_MQTT_TEST_PORT=1894 \\
      .venv-check/bin/python -m pytest tests/test_mqtt_broker_integration.py

Never point these at a real broker serving a real Home Assistant instance:
this suite publishes real (retained) discovery configs on it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 — restarts this suite's own disposable dev broker container, see TestBrokerRestartAgainstARealBroker
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest

_HOST = os.environ.get("NIBE_MQTT_TEST_HOST")
_PORT = int(os.environ.get("NIBE_MQTT_TEST_PORT", "1894"))

# Every unique_id this suite touches is namespaced to this process/worker.
# The broker is shared across xdist workers within one run and across two
# runs started close together, and every discovery config here is
# *retained* -- without a unique namespace, one run's leftover retained
# config for point 2002 would seed a later run's scan_mqtt_discovery() with
# stale state, corrupting exactly the behaviour under test. Same lesson as
# test_entity_manager_snapshots.py's shared-/tmp-path race (see CLAUDE.md):
# found by running this suite repeatedly, not by reasoning about it up front.
_WORKER_TAG = f"{os.environ.get('PYTEST_XDIST_WORKER', 'solo')}_{os.getpid()}"
_TEST_POINT_ID = 900000 + (hash(_WORKER_TAG) % 90000)

pytestmark = pytest.mark.skipif(
    not _HOST, reason="NIBE_MQTT_TEST_HOST is not set; see this module's docstring"
)


def _real_client() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(_HOST or "", _PORT, keepalive=10)
    client.loop_start()
    return client


def _point(pid: int, entity_type: str) -> dict:
    return {
        "variableId": pid,
        "display_title": f"Integration Test Point {pid}",
        "entity_type": entity_type,
        "entity_category": "diagnostic",
        "is_writable": False,
        "is_dynamic": False,
        "description": "",
        "metadata": {
            "unit": "",
            "shortUnit": "",
            "minValue": 0,
            "maxValue": 1,
            "modbusRegisterID": pid,
            "modbusRegisterType": "MODBUS_INPUT_REGISTER",
            "variableType": "integer",
            "variableSize": "u8",
            "isWritable": False,
            "divisor": 1,
            "decimal": 0,
            "intDefaultValue": 0,
            "stringDefaultValue": "",
            "change": 1,
        },
    }


class _Subscriber:
    """A second, independent client -- collects whatever is published to
    the given topics, the way Home Assistant's own MQTT integration would."""

    def __init__(self, topics: list[str]) -> None:
        self.messages: dict[str, str] = {}
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_message = self._on_message
        self._client.connect(_HOST or "", _PORT, keepalive=10)
        for topic in topics:
            self._client.subscribe(topic)
        self._client.loop_start()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        self.messages[message.topic] = message.payload.decode()

    def wait_for_value(self, topic: str, expected: str, timeout: float = 15.0) -> str | None:
        """Waits for a *specific* value, not just any message -- a topic
        that already holds a previous retained value would otherwise let
        wait_for() return stale data instead of actually waiting for the
        real update under test."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.messages.get(topic) == expected:
                return expected
            time.sleep(0.05)
        return self.messages.get(topic)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


class TestPublishEntityDiscoveryAgainstARealBroker:
    def test_publishes_a_valid_retained_config_a_late_subscriber_can_read(self) -> None:
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, create_entity_id, t_config, t_state

        pid = _TEST_POINT_ID
        entity_id = create_entity_id(pid)
        topic = t_config("sensor", entity_id)

        client = _real_client()
        try:
            pub = MqttDiscoveryPublisher(
                mqtt_client=client,
                device_info={"identifiers": ["nibe_integration_test"], "name": "Integration Test"},
                device_id="nibe_integration_test",
                device_name="Integration Test Device",
            )
            pub.publish_entity_discovery(_point(pid, "sensor"), {})
            time.sleep(0.2)  # let the broker apply the retained message

            late_subscriber = _Subscriber([topic])
            try:
                # Poll until the retained config arrives, then validate it.
                deadline = time.monotonic() + 15.0
                raw = late_subscriber.messages.get(topic)
                while not raw and time.monotonic() < deadline:
                    time.sleep(0.05)
                    raw = late_subscriber.messages.get(topic)
                assert raw, f"No retained config found on {topic}"
                payload = json.loads(raw)
                assert payload["state_topic"] == t_state("sensor", entity_id)
                assert payload["unique_id"] == f"nibe_{pid}"
            finally:
                late_subscriber.close()
        finally:
            client.publish(topic, "", retain=True)  # clean up after ourselves
            time.sleep(0.1)
            client.loop_stop()
            client.disconnect()


class TestEntityTypeChangeCleanupAgainstARealBroker:
    """Regression coverage for GitHub issue #23: a point reclassified
    between entity types must have its old retained discovery topic
    cleared, detected via a real broker's scan_mqtt_discovery() -- not a
    mocked client whose message delivery order is scripted by the test
    itself."""

    def test_stale_domain_is_cleared_when_scan_finds_it_before_republish(self) -> None:
        from nibe_entity_manager import EntityManager
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, create_entity_id, t_config

        pid = _TEST_POINT_ID + 1
        entity_id = create_entity_id(pid)
        old_topic = t_config("binary_sensor", entity_id)
        new_topic = t_config("sensor", entity_id)

        setup_client = _real_client()
        try:
            # Simulate a pre-reclassification install: a real retained
            # binary_sensor config already on the broker for this point,
            # published by a throwaway publisher instance (a fresh process
            # would never share publisher state across restarts either).
            old_pub = MqttDiscoveryPublisher(
                mqtt_client=setup_client,
                device_info={"identifiers": ["nibe_integration_test"], "name": "Integration Test"},
                device_id="nibe_integration_test",
                device_name="Integration Test Device",
            )
            old_pub.publish_entity_discovery(_point(pid, "binary_sensor"), {})
            time.sleep(0.2)

            observer = _Subscriber([old_topic, new_topic])
            try:
                # A fresh process's EntityManager + publisher, exactly as
                # generate_nibe_mqtt.py constructs them on restart -- no
                # in-memory history of this point's previous entity_type.
                with (
                    patch("nibe_entity_manager.EntityManager.resubscribe_all"),
                    patch("nibe_entity_manager.EntityManager._setup_history_loading"),
                    patch("nibe_entity_manager.EntityManager._setup_dynamic_map_loading"),
                ):
                    fresh_client = _real_client()
                    try:
                        fresh_pub = MqttDiscoveryPublisher(
                            mqtt_client=fresh_client,
                            device_info={
                                "identifiers": ["nibe_integration_test"],
                                "name": "Integration Test",
                            },
                            device_id="nibe_integration_test",
                            device_name="Integration Test Device",
                        )
                        em = EntityManager(
                            api_client=MagicMock(),
                            publisher=fresh_pub,
                            notify_fn=MagicMock(),
                            dismiss_fn=MagicMock(),
                            mqtt_client=fresh_client,
                        )
                        # The real behaviour under test: scan the real
                        # broker for existing retained configs (seeding
                        # fresh_pub's stale-domain tracking from whatever
                        # the broker actually returns), then publish this
                        # point under its new, reclassified entity_type.
                        em.scan_mqtt_discovery()
                        fresh_pub.publish_entity_discovery(_point(pid, "sensor"), {})

                        # Generous timeout: confirmed empirically to flake
                        # at the 5.0s default under the full test suite's
                        # parallel CPU load (passes reliably alone or in
                        # this file only) -- a timing margin issue under
                        # contention, not a logic bug.
                        assert observer.wait_for_value(old_topic, "", timeout=10.0) == "", (
                            "Stale binary_sensor config was not cleared on the real broker"
                        )
                    finally:
                        fresh_client.loop_stop()
                        fresh_client.disconnect()
            finally:
                observer.close()
        finally:
            setup_client.publish(old_topic, "", retain=True)
            setup_client.publish(new_topic, "", retain=True)
            time.sleep(0.1)
            setup_client.loop_stop()
            setup_client.disconnect()


class TestDynamicBinarySensorReclassificationAgainstARealBroker:
    """Regression coverage for the dynamic-reclassification feature added
    this session: a point classified as binary_sensor that is then polled
    with a raw value other than 0/1 flips its cached entity_type to
    "sensor" and republishes MQTT discovery under the new domain (see
    EntityManager._reclassify_binary_sensor / _process_and_publish_state in
    nibe_entity_manager.py).

    TestEntityTypeChangeCleanupAgainstARealBroker above proves the GENERAL
    stale-domain cleanup mechanism inside publish_entity_discovery() works
    against a real broker when entity_type simply changes between two
    publish_entity_discovery() calls. It does not drive the NEW trigger
    path at all -- this test does: a real EntityManager.active_entities_by_id
    entry classified as binary_sensor, fed a live poll (via the same
    _update_entity_state() the normal polling loop calls) whose bulk_data
    raw_value is a non-boolean int, must itself detect the mismatch,
    reclassify, and republish -- exercising _reclassify_binary_sensor's own
    warn-once/mutate-in-place/republish sequence, not just the cleanup
    mechanism it happens to depend on."""

    def test_a_live_poll_with_a_non_boolean_value_reclassifies_and_republishes(self) -> None:
        from nibe_entity_manager import EntityManager
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, create_entity_id, t_config, t_state

        pid = _TEST_POINT_ID + 30
        entity_id = create_entity_id(pid)
        old_topic = t_config("binary_sensor", entity_id)
        new_topic = t_config("sensor", entity_id)
        state_topic = t_state("sensor", entity_id)

        setup_client = _real_client()
        try:
            # Publish the pre-reclassification state: a real retained
            # binary_sensor discovery config on the broker, exactly as if
            # this point had been running correctly (as a real boolean, or
            # not yet observed with an offending value) since the bridge
            # last started.
            old_pub = MqttDiscoveryPublisher(
                mqtt_client=setup_client,
                device_info={"identifiers": ["nibe_integration_test"], "name": "Integration Test"},
                device_id="nibe_integration_test",
                device_name="Integration Test Device",
            )
            binary_point = _point(pid, "binary_sensor")
            entity_info = old_pub.publish_entity_discovery(binary_point, {})
            assert entity_info is not None
            time.sleep(0.2)

            observer = _Subscriber([old_topic, new_topic, state_topic])
            try:
                with (
                    patch("nibe_entity_manager.EntityManager.resubscribe_all"),
                    patch("nibe_entity_manager.EntityManager._setup_history_loading"),
                    patch("nibe_entity_manager.EntityManager._setup_dynamic_map_loading"),
                ):
                    fresh_client = _real_client()
                    try:
                        fresh_pub = MqttDiscoveryPublisher(
                            mqtt_client=fresh_client,
                            device_info={
                                "identifiers": ["nibe_integration_test"],
                                "name": "Integration Test",
                            },
                            device_id="nibe_integration_test",
                            device_name="Integration Test Device",
                        )
                        em = EntityManager(
                            api_client=MagicMock(),
                            publisher=fresh_pub,
                            notify_fn=MagicMock(),
                            dismiss_fn=MagicMock(),
                            mqtt_client=fresh_client,
                        )
                        # Seed fresh_pub's stale-domain tracking from the
                        # real broker, exactly like the general-mechanism
                        # test above -- otherwise publish_entity_discovery's
                        # cleanup has no retained old_topic to know about,
                        # since fresh_pub itself never published it.
                        em.scan_mqtt_discovery()

                        # entity_info carries this point's real
                        # binary_sensor topics/point_data, exactly what
                        # active_entities_by_id would hold after a real
                        # startup discovery scan. bulk_data supplies the
                        # live-poll value under test: 30, not 0/1 -- the
                        # actual NEW trigger condition
                        # (`raw_value not in (0, 1)`), not a directly-called
                        # entity_type swap.
                        em.active_entities_by_id[pid] = entity_info
                        em.bulk_data[pid] = {
                            "is_ok": True,
                            "raw_value": 30,
                            "string_value": "30",
                            "metadata": binary_point["metadata"],
                        }

                        # The real trigger path: the same method the normal
                        # polling loop calls per active entity.
                        em._update_entity_state(entity_info)

                        assert observer.wait_for_value(old_topic, "", timeout=10.0) == "", (
                            "Stale binary_sensor config was not cleared after "
                            "a live poll triggered dynamic reclassification"
                        )
                        # wait_for_value's exact-match contract needs a
                        # concrete expected payload; the real config's exact
                        # JSON isn't known ahead of time, so poll for
                        # *any* non-empty payload on new_topic instead of
                        # asserting equality against a fabricated one.
                        deadline = time.monotonic() + 10.0
                        while not observer.messages.get(new_topic) and time.monotonic() < deadline:
                            time.sleep(0.05)
                        new_raw = observer.messages.get(new_topic)
                        assert new_raw, "No new sensor discovery config was published/retained"
                        new_payload = json.loads(new_raw)
                        assert new_payload["state_topic"] == state_topic
                        assert new_payload["unique_id"] == f"nibe_{pid}"

                        assert observer.wait_for_value(state_topic, "30", timeout=10.0) == "30", (
                            "State topic did not carry the raw value after reclassification"
                        )

                        # entity_info is mutated in place (see
                        # _reclassify_binary_sensor's docstring) -- confirm
                        # the caller's own reference actually picked up the
                        # new entity_type/state_topic, not just the broker.
                        assert entity_info["entity_type"] == "sensor"
                        assert entity_info["state_topic"] == state_topic
                    finally:
                        fresh_client.loop_stop()
                        fresh_client.disconnect()
            finally:
                observer.close()
        finally:
            setup_client.publish(old_topic, "", retain=True)
            setup_client.publish(new_topic, "", retain=True)
            setup_client.publish(state_topic, "", retain=True)
            time.sleep(0.1)
            setup_client.loop_stop()
            setup_client.disconnect()


class TestCorruptedRetainedConfigAgainstARealBroker:
    """A retained discovery config already on the broker at startup can be
    corrupted (truncated by a killed process mid-publish, hand-edited,
    written by an incompatible older version). scan_mqtt_discovery() must
    log and skip that one topic, not crash the whole startup scan -- and
    must still pick up every other, valid retained config alongside it."""

    def test_malformed_json_on_one_topic_does_not_abort_the_scan(self) -> None:
        from nibe_entity_manager import EntityManager
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, create_entity_id, t_config

        bad_pid = _TEST_POINT_ID + 10
        good_pid = _TEST_POINT_ID + 11
        bad_topic = t_config("sensor", create_entity_id(bad_pid))
        good_topic = t_config("sensor", create_entity_id(good_pid))

        setup_client = _real_client()
        try:
            # Not valid JSON at all -- simulates a process killed mid-write
            # or a hand-edited retained message.
            setup_client.publish(bad_topic, "{not valid json", retain=True)
            good_pub = MqttDiscoveryPublisher(
                mqtt_client=setup_client,
                device_info={"identifiers": ["nibe_integration_test"], "name": "Integration Test"},
                device_id="nibe_integration_test",
                device_name="Integration Test Device",
            )
            good_pub.publish_entity_discovery(_point(good_pid, "sensor"), {})
            time.sleep(0.2)

            with (
                patch("nibe_entity_manager.EntityManager.resubscribe_all"),
                patch("nibe_entity_manager.EntityManager._setup_history_loading"),
                patch("nibe_entity_manager.EntityManager._setup_dynamic_map_loading"),
            ):
                fresh_client = _real_client()
                try:
                    fresh_pub = MqttDiscoveryPublisher(
                        mqtt_client=fresh_client,
                        device_info={
                            "identifiers": ["nibe_integration_test"],
                            "name": "Integration Test",
                        },
                        device_id="nibe_integration_test",
                        device_name="Integration Test Device",
                    )
                    em = EntityManager(
                        api_client=MagicMock(),
                        publisher=fresh_pub,
                        notify_fn=MagicMock(),
                        dismiss_fn=MagicMock(),
                        mqtt_client=fresh_client,
                    )
                    discovered = em.scan_mqtt_discovery()
                    assert good_pid in discovered, (
                        "A valid retained config was lost because a sibling "
                        "topic's config was corrupted"
                    )
                    assert bad_pid not in discovered
                finally:
                    fresh_client.loop_stop()
                    fresh_client.disconnect()
        finally:
            setup_client.publish(bad_topic, "", retain=True)
            setup_client.publish(good_topic, "", retain=True)
            time.sleep(0.1)
            setup_client.loop_stop()
            setup_client.disconnect()


def _make_reconnecting_client(avail_topic: str) -> tuple[mqtt.Client, threading.Event]:
    """A client configured the same way generate_nibe_mqtt.py configures the
    bridge's real one: an LWT that fires "offline" on any ungraceful loss of
    connection, and paho's own automatic-reconnect machinery enabled with a
    short backoff so tests don't wait out the production 1-30s range."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.reconnect_delay_set(min_delay=1, max_delay=2)
    client.will_set(avail_topic, "offline", retain=True)
    connected = threading.Event()

    def on_connect(
        _client: Any, _userdata: Any, _flags: Any, _reason_code: Any, _properties: Any
    ) -> None:
        connected.set()

    client.on_connect = on_connect
    client.connect(_HOST or "", _PORT, keepalive=2)
    client.loop_start()
    connected.wait(timeout=15)
    return client, connected


class TestUncleanDisconnectAgainstARealBroker:
    """Real-broker misbehaviour coverage, not just the happy path above: a
    severed connection (a crash, not a clean shutdown) must (1) actually
    trigger the broker's real LWT mechanism, and (2) actually cause the
    broker to drop this client's subscriptions on reconnect (paho's default
    clean-session behaviour) -- which is exactly the vulnerability
    resubscribe_all() exists to close. Neither of those is something a
    mocked client can expose: a mock has no concept of a broker deciding a
    connection is dead, and no concept of a broker discarding subscription
    state that was never really there to begin with."""

    def test_lwt_fires_and_resubscribe_all_recovers_a_lost_subscription(self) -> None:
        from nibe_entity_manager import EntityManager

        avail_topic = f"nibe/browser/integration_test/available/{_TEST_POINT_ID}"
        cmd_topic = f"nibe/browser/integration_test/cmd/{_TEST_POINT_ID}"

        with (
            patch("nibe_entity_manager.EntityManager.resubscribe_all"),
            patch("nibe_entity_manager.EntityManager._setup_history_loading"),
            patch("nibe_entity_manager.EntityManager._setup_dynamic_map_loading"),
        ):
            bridge_client, _connected = _make_reconnecting_client(avail_topic)
        try:
            em = EntityManager(
                api_client=MagicMock(),
                publisher=MagicMock(),
                notify_fn=MagicMock(),
                dismiss_fn=MagicMock(),
                mqtt_client=bridge_client,
            )
            em._mgmt_avail_topic = avail_topic

            received: list[bytes] = []

            def handler(_client: Any, _userdata: Any, message: Any) -> None:
                received.append(message.payload)

            bridge_client.subscribe(cmd_topic, qos=1)
            bridge_client.message_callback_add(cmd_topic, handler)
            em.register_mgmt_subscription(cmd_topic, handler, qos=1)

            ha_client = _real_client()
            avail_observer = _Subscriber([avail_topic])
            try:
                # Sanity: the subscription works before anything goes wrong.
                ha_client.publish(cmd_topic, "before", retain=False)
                deadline = time.monotonic() + 15.0
                while not received and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert received == [b"before"], "Initial subscription never delivered"

                # Simulate a crash: sever the socket directly, not
                # disconnect() -- the broker, not this process, must be the
                # one to notice the loss and fire the will. The network loop
                # thread (from loop_start() above) is deliberately left
                # running -- paho's automatic reconnect requires that same
                # thread to notice the broken socket and drive the
                # reconnect itself; stopping and manually restarting the
                # loop around the socket close breaks that detection path
                # (confirmed empirically: doing so left the client
                # permanently unreconnected).
                reconnected = threading.Event()

                def _on_reconnect(*_args: Any) -> None:
                    reconnected.set()

                bridge_client.on_connect = _on_reconnect

                sock = bridge_client.socket()
                assert sock is not None
                sock.close()

                assert (
                    avail_observer.wait_for_value(avail_topic, "offline", timeout=15.0) == "offline"
                ), "Real broker did not fire the LWT on an unclean disconnect"

                assert reconnected.wait(timeout=15.0), "Client never reconnected"
                time.sleep(0.3)  # let the broker finish applying the new session

                # The real vulnerability: clean-session reconnect drops the
                # broker-side subscription. Without resubscribe_all(), this
                # message must NOT arrive.
                received.clear()
                ha_client.publish(cmd_topic, "lost", retain=False)
                time.sleep(1.0)
                assert received == [], (
                    "Subscription unexpectedly survived reconnect on its own -- "
                    "if paho's behaviour changed, resubscribe_all() may no "
                    "longer be necessary, but this test's premise would be wrong"
                )

                # The real fix: resubscribe_all() must restore it.
                em.resubscribe_all()
                # subscribe() only sends the SUBSCRIBE packet -- it does not
                # wait for the broker's SUBACK, so publishing immediately
                # after can race the broker still applying it. Confirmed
                # empirically: this test was flaky (2 of 3 runs) without
                # this pause.
                time.sleep(0.3)
                ha_client.publish(cmd_topic, "recovered", retain=False)
                deadline = time.monotonic() + 15.0
                while not received and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert received == [b"recovered"], (
                    "resubscribe_all() did not restore the lost subscription on the real broker"
                )

                # republish_availability() must also bring the entity back
                # online after the LWT marked it offline.
                em.republish_availability()
                # Generous timeout: confirmed empirically to flake at 3.0s
                # under the full test suite's parallel CPU load (this test
                # passes reliably alone or in this file only) -- a timing
                # margin issue under contention, not a logic bug.
                assert (
                    avail_observer.wait_for_value(avail_topic, "online", timeout=10.0) == "online"
                )
            finally:
                avail_observer.close()
                ha_client.publish(cmd_topic, "", retain=True)
                ha_client.publish(avail_topic, "", retain=True)
                time.sleep(0.1)
                ha_client.loop_stop()
                ha_client.disconnect()
        finally:
            bridge_client.loop_stop()
            bridge_client.disconnect()


class TestBrokerRestartAgainstARealBroker:
    """A step further than a severed socket: the broker process itself goes
    away and comes back, the way a Mosquitto add-on restart or host reboot
    would. Only runs against this suite's own disposable dev broker
    (dev/mosquitto.sh) -- restarting is destructive to whatever is
    connected, so this never runs against an arbitrary NIBE_MQTT_TEST_HOST
    without an explicit second opt-in, on top of the module-level one."""

    @pytest.mark.skipif(
        not os.environ.get("NIBE_MQTT_TEST_ALLOW_BROKER_RESTART"),
        reason="NIBE_MQTT_TEST_ALLOW_BROKER_RESTART is not set -- this test "
        "restarts the broker container, see this class's docstring",
    )
    @pytest.mark.skipif(not shutil.which("docker"), reason="docker not available")
    def test_client_reconnects_after_the_broker_container_restarts(self) -> None:
        container = os.environ.get("NIBE_MQTT_TEST_CONTAINER", "nibe-dev-mosquitto")
        avail_topic = f"nibe/browser/integration_test/restart_available/{_TEST_POINT_ID}"

        client, _connected = _make_reconnecting_client(avail_topic)
        try:
            reconnect_count = [0]

            def on_connect(*_args: Any) -> None:
                reconnect_count[0] += 1

            client.on_connect = on_connect

            subprocess.run(  # nosec B603, B607 — fixed args, this suite's own disposable container
                ["docker", "restart", container],
                check=True,
                capture_output=True,
                timeout=30,
            )

            deadline = time.monotonic() + 20.0
            while reconnect_count[0] < 1 and time.monotonic() < deadline:
                time.sleep(0.1)
            assert reconnect_count[0] >= 1, (
                f"Client never reconnected after 'docker restart {container}'"
            )

            # The broker process restarting is a fresh instance with no
            # memory of this client's prior subscriptions -- confirm the
            # connection is actually usable again, not just "connected".
            probe_topic = f"nibe/browser/integration_test/restart_probe/{_TEST_POINT_ID}"
            received: list[bytes] = []
            client.subscribe(probe_topic, qos=1)
            client.message_callback_add(probe_topic, lambda _c, _u, m: received.append(m.payload))
            prober = _real_client()
            try:
                prober.publish(probe_topic, "alive", retain=False)
                deadline = time.monotonic() + 15.0
                while not received and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert received == [b"alive"], "Connection unusable after broker restart"
            finally:
                prober.publish(probe_topic, "", retain=True)
                time.sleep(0.1)
                prober.loop_stop()
                prober.disconnect()
        finally:
            client.publish(avail_topic, "", retain=True)
            time.sleep(0.1)
            client.loop_stop()
            client.disconnect()


class TestOutboundQueueBackpressureAgainstARealBroker:
    """generate_nibe_mqtt.py caps the outbound queue at 1000 messages
    (max_queued_messages_set) specifically "to prevent unbounded memory
    growth under backpressure". That cap has never been proven to actually
    behave as a graceful degradation rather than a crash or a silent hang
    -- paho's real behaviour once the cap is hit (confirmed empirically
    while building this test: publish() returns MQTT_ERR_QUEUE_SIZE and
    drops the message, it does not raise or block) is exactly the kind of
    real client-library behaviour a mock can't expose, since a mocked
    publish() only ever returns whatever a test tells it to."""

    def test_publish_past_the_cap_is_dropped_gracefully_not_a_crash(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.max_queued_messages_set(5)
        # connect_async(), not the blocking connect(): a synchronous connect()
        # races its own internal CONNACK processing against these immediately
        # -following publish() calls -- confirmed empirically to be genuinely
        # flaky (the client can end up "connected" before or after the
        # publishes depending on scheduling, non-deterministically changing
        # which MQTT_ERR_* code comes back). connect_async() guarantees the
        # client is genuinely not yet connected when publish() is called,
        # which is what actually exercises "queued while disconnected" rather
        # than racing a real connection attempt.
        client.connect_async(_HOST or "", _PORT, keepalive=10)
        try:
            topic = f"nibe/browser/integration_test/queue_overflow/{_TEST_POINT_ID}"
            results = [client.publish(f"{topic}/{i}", f"msg{i}", qos=1) for i in range(10)]
            rcs = [r.rc for r in results]
            assert rcs[:5] == [mqtt.MQTT_ERR_NO_CONN] * 5, (
                f"First 5 (at the cap) should queue while genuinely disconnected, got: {rcs[:5]}"
            )
            assert all(rc == mqtt.MQTT_ERR_QUEUE_SIZE for rc in rcs[5:]), (
                f"Publishes past the cap should be dropped with MQTT_ERR_QUEUE_SIZE, got: {rcs}"
            )

            # Confirm the process survives past the cap and, once connected,
            # actually delivers the messages that *did* make it into the
            # queue -- proving this is graceful degradation (some messages
            # lost under sustained disconnection) rather than the whole
            # client becoming unusable once the cap is first hit.
            observer = _Subscriber([f"{topic}/+"])
            try:
                # The subscription itself is async (subscribe() doesn't wait
                # for the broker's SUBACK) -- without this pause, loop_start()
                # below can flush the queued publishes before the broker has
                # actually applied the subscription, and the observer misses
                # them even though nothing is actually wrong. Confirmed
                # empirically: this was the test's own bug, not a real
                # product issue, on the first attempt.
                time.sleep(0.3)
                client.loop_start()
                deadline = time.monotonic() + 15.0
                while len(observer.messages) < 5 and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert len(observer.messages) == 5, (
                    f"Expected exactly the 5 queued-before-the-cap messages to "
                    f"arrive, got {len(observer.messages)}: {sorted(observer.messages)}"
                )
            finally:
                observer.close()
        finally:
            client.loop_stop()
            client.disconnect()


class TestDiscoveryPublishQueueFullAgainstARealBroker:
    """TestOutboundQueueBackpressureAgainstARealBroker above proves paho's
    raw publish() degrades gracefully once max_queued_messages_set's cap is
    hit. It does not prove publish_entity_discovery() itself -- the actual
    call site that checks `result.rc != 0` -- reacts correctly to that real
    MQTT_ERR_QUEUE_SIZE return: this closes that gap against a real capped,
    disconnected client rather than a mocked result.rc."""

    def test_discovery_publish_past_the_cap_returns_none_not_a_crash(self) -> None:
        from nibe_mqtt_publisher import MqttDiscoveryPublisher

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.max_queued_messages_set(1)
        client.connect_async(_HOST or "", _PORT, keepalive=10)
        try:
            pub = MqttDiscoveryPublisher(
                mqtt_client=client,
                device_info={"identifiers": ["nibe_integration_test"], "name": "Integration Test"},
                device_id="nibe_integration_test",
                device_name="Integration Test Device",
            )
            # First publish fills the 1-message cap while genuinely
            # disconnected (mirrors the queue-overflow test's own
            # connect_async() pattern); every publish after it must be
            # dropped with MQTT_ERR_QUEUE_SIZE on the real client.
            filler_result = client.publish("nibe/browser/integration_test/filler", "x", qos=1)
            assert filler_result.rc == mqtt.MQTT_ERR_NO_CONN

            result = pub.publish_entity_discovery(_point(_TEST_POINT_ID + 20, "sensor"), {})
            assert result is None, (
                "publish_entity_discovery() should return None when the real "
                "MQTT client's outbound queue is genuinely full, not raise "
                "or silently report success"
            )
        finally:
            client.loop_stop()
            client.disconnect()


@contextmanager
def _size_limited_broker(max_bytes: int, port: int = 1899):
    """A throwaway mosquitto container configured with a real
    max_packet_size cap -- proves what actually happens to a discovery
    publish oversized enough to exceed a broker-imposed packet limit (a
    real, if unusual, broker deployment constraint), rather than assuming
    it behaves like the client-side queue-full case above. Same
    disposable-container pattern as _password_protected_broker."""
    scratch = tempfile.mkdtemp(dir=os.path.dirname(__file__) + "/..")
    container = "nibe-test-sizelimit-broker"
    try:
        with open(os.path.join(scratch, "mosquitto.conf"), "w") as f:
            f.write(f"listener {port}\nallow_anonymous true\npersistence false\n")
            f.write(f"max_packet_size {max_bytes}\n")
        subprocess.run(  # nosec B603, B607 — fixed args, disposable test container
            ["docker", "rm", "-f", container], capture_output=True, timeout=10, check=False
        )
        subprocess.run(  # nosec B603, B607 — fixed args, disposable test container
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-p",
                f"127.0.0.1:{port}:{port}",
                "-v",
                f"{scratch}/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro",
                "eclipse-mosquitto:2",
                "mosquitto",
                "-c",
                "/mosquitto/config/mosquitto.conf",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            probe = subprocess.run(  # nosec B603, B607 — fixed args, disposable test container
                ["docker", "exec", container, "sh", "-c", f"nc -z localhost {port}"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.2)
        yield port
    finally:
        subprocess.run(  # nosec B603, B607 — fixed args, disposable test container
            ["docker", "rm", "-f", container], capture_output=True, timeout=10, check=False
        )
        shutil.rmtree(scratch, ignore_errors=True)


class TestOversizedPacketAgainstARealBroker:
    """A real max_packet_size on the broker (a deliberately conservative
    deployment, or one hit by a point with an unusually long title/device
    metadata) rejects an oversized publish outright. Confirmed empirically
    while writing this test: mosquitto silently drops the offending publish
    and closes the connection rather than sending any MQTT-level error back
    to a plain MQTT 3.1.1 client (there is no NAK for this in that
    protocol version) -- so the observable effect on the wire is a
    disconnect, not a non-zero result.rc from the immediate publish() call
    itself, which only reports local hand-off to the client's own socket
    buffer."""

    def test_oversized_discovery_publish_disconnects_rather_than_silently_vanishing(self) -> None:
        from nibe_mqtt_publisher import MqttDiscoveryPublisher

        with _size_limited_broker(max_bytes=200) as port:
            disconnected = threading.Event()
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

            def on_disconnect(
                _client: Any, _userdata: Any, _flags: Any, _reason_code: Any, _properties: Any
            ) -> None:
                disconnected.set()

            client.on_disconnect = on_disconnect
            client.connect("127.0.0.1", port, keepalive=10)
            client.loop_start()
            try:
                pub = MqttDiscoveryPublisher(
                    mqtt_client=client,
                    device_info={
                        "identifiers": ["nibe_integration_test"],
                        "name": "Integration Test",
                    },
                    device_id="nibe_integration_test",
                    device_name="Integration Test Device",
                )
                # A title long enough that the resulting discovery JSON
                # (device block, availability topics, etc. included)
                # comfortably exceeds the broker's 200-byte cap.
                point = _point(_TEST_POINT_ID + 21, "sensor")
                point["display_title"] = "X" * 2000
                pub.publish_entity_discovery(point, {})

                assert disconnected.wait(timeout=10.0), (
                    "Broker never disconnected the client after an oversized "
                    "publish -- max_packet_size may not be taking effect"
                )
            finally:
                client.loop_stop()
                client.disconnect()


@contextmanager
def _password_protected_broker(username: str, password: str, port: int = 1898):
    """A throwaway, disposable mosquitto container requiring real
    username/password auth -- distinct from the shared anonymous dev
    broker every other test in this file uses, which can't exercise a
    real auth rejection at all. Not shared with dev/mosquitto.sh's
    container so this suite can't ever interfere with a developer's own
    dev broker or vice versa."""
    scratch = tempfile.mkdtemp(dir=os.path.dirname(__file__) + "/..")
    container = "nibe-test-auth-broker"
    try:
        with open(os.path.join(scratch, "passwd.conf"), "w") as f:
            f.write("")
        with open(os.path.join(scratch, "mosquitto.conf"), "w") as f:
            f.write(f"listener {port}\nallow_anonymous false\n")
            f.write("password_file /mosquitto/config/passwd.conf\npersistence false\n")
        subprocess.run(  # nosec B603, B607 — fixed args, disposable test container
            ["docker", "rm", "-f", container], capture_output=True, timeout=10, check=False
        )
        subprocess.run(  # nosec B603, B607 — fixed args, disposable test container
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-p",
                f"127.0.0.1:{port}:{port}",
                "-v",
                f"{scratch}/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro",
                "-v",
                f"{scratch}/passwd.conf:/mosquitto/config/passwd.conf",
                "eclipse-mosquitto:2",
                "sh",
                "-c",
                (
                    f"mosquitto_passwd -b /mosquitto/config/passwd.conf {username} {password} "
                    "&& mosquitto -c /mosquitto/config/mosquitto.conf"
                ),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        # Wait for the broker to actually accept connections rather than a
        # fixed sleep -- mosquitto_passwd running first inside the
        # container adds a variable startup delay ahead of mosquitto itself.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            probe = subprocess.run(  # nosec B603, B607 — fixed args, disposable test container
                ["docker", "exec", container, "sh", "-c", f"nc -z localhost {port}"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.2)
        yield port
    finally:
        subprocess.run(  # nosec B603, B607 — fixed args, disposable test container
            ["docker", "rm", "-f", container], capture_output=True, timeout=10, check=False
        )
        shutil.rmtree(scratch, ignore_errors=True)


class TestBrokerAuthRejectionAgainstARealBroker:
    """generate_nibe_mqtt.py's on_connect treats reason codes {4, 5}
    (_FATAL_RC) as unrecoverable auth failures and stops retrying. Proven
    here against mosquitto's real CONNACK rejection for a genuinely wrong
    password, not an assumption about what reason code a mocked on_connect
    callback would have been handed."""

    @pytest.mark.skipif(not shutil.which("docker"), reason="docker not available")
    def test_wrong_credentials_produce_the_reason_code_production_code_expects(self) -> None:
        with _password_protected_broker("realuser", "realpass", port=1898) as port:
            result: dict = {}
            connected = threading.Event()

            def on_connect(_c: Any, _u: Any, _f: Any, reason_code: Any, _p: Any) -> None:
                result["rc"] = (
                    reason_code.value if hasattr(reason_code, "value") else int(reason_code)
                )
                connected.set()

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.username_pw_set("realuser", "wrongpassword")
            client.on_connect = on_connect
            client.connect("127.0.0.1", port, keepalive=10)
            client.loop_start()
            try:
                assert connected.wait(timeout=10.0), "Broker never responded to the connect attempt"
                # _FATAL_RC = {134, 135} in generate_nibe_mqtt.py -- this is the
                # exact set production code checks reason_code.value against.
                # paho's VERSION2 on_connect always normalizes to MQTT5-style
                # ReasonCode values (128+ range), even for a plain MQTT 3.1.1
                # wire connection -- confirmed empirically against a real broker.
                assert result.get("rc") in (134, 135), (
                    f"Expected a bad-credentials reason code in {{134, 135}} "
                    f"(what production code's _FATAL_RC checks), got {result.get('rc')}"
                )
            finally:
                client.loop_stop()
                client.disconnect()
